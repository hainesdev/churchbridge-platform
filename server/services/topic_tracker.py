"""Semantic-memory layer for live sermon context.

The :class:`TopicTracker` owns a structured memory object describing the
current sermon (active passage, sermon arc, rhetorical goal, primary
themes, illustration state, short/long summary, evidence) and refreshes
that memory off the latency-critical caption path.

The tracker exposes three surfaces to the rest of the pipeline:

- :meth:`get_context` — backward-compatible single-string sermon context
  used by older prompt assembly paths.
- :meth:`get_memory` — the canonical structured :class:`TopicTrackerMemory`.
- :meth:`get_prompt_blocks_text` — labeled prompt blocks
  (``[ACTIVE PASSAGE]``, ``[SERMON ARC]``, etc.) for downstream prompt
  assembly that wants to consume hard and soft context separately.

Refreshes are driven by two sources:

- a periodic fallback schedule (early-fast / late-slow) that prevents
  topic state from going stale during long, semantically similar stretches;
- explicit :meth:`request_refresh` calls from
  :class:`LLMEnrichmentService`, which sees discourse shifts first and can
  signal the tracker to recompute on real rhetorical movement.

The scheduler dedupes repeated identical signals, applies separate
cooldowns for periodic / soft / strong refreshes, never runs more than
one refresh task concurrently, and stores the strongest pending request
so it can rerun once after the in-flight task settles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Awaitable, Callable

import anthropic

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model + cache configuration (Phase 5: dedicated topic-tracker model knobs)
# ---------------------------------------------------------------------------

# Live caption enrichment uses a fast/cheap model; topic tracking has more
# latency tolerance and benefits from a stronger summarizer. Default to the
# same model as before so existing behavior is preserved when the env var is
# unset, but allow operators to point this at a stronger model.
ANTHROPIC_MODEL = os.getenv(
    "TOPIC_TRACKER_MODEL", "claude-haiku-4-5-20251001"
)
TOPIC_TRACKER_MAX_TOKENS = int(os.getenv("TOPIC_TRACKER_MAX_TOKENS", "600"))
PROMPT_CACHE_TTL = os.getenv(
    "TOPIC_TRACKER_PROMPT_CACHE_TTL",
    os.getenv("ANTHROPIC_PROMPT_CACHE_TTL", "5m"),
)
_PROMPT_CACHE_CONTROL = {"type": "ephemeral", "ttl": PROMPT_CACHE_TTL}


def _cached_text_block(text: str) -> dict[str, object]:
    return {
        "type": "text",
        "text": text,
        "cache_control": _PROMPT_CACHE_CONTROL,
    }


# ---------------------------------------------------------------------------
# Periodic refresh schedule (Phase 0/3)
# ---------------------------------------------------------------------------

FIRST_SUMMARY_MIN_SEGMENTS = 3
MIN_SEGMENTS_BEFORE_SUMMARY = 8

FAST_INTERVAL_SECS = int(os.getenv("TOPIC_TRACKER_PERIODIC_FAST_SECS", "60"))
FAST_INTERVAL_LIMIT_SECS = int(os.getenv("TOPIC_TRACKER_FAST_LIMIT_SECS", "600"))
SLOW_INTERVAL_SECS = int(os.getenv("TOPIC_TRACKER_PERIODIC_SLOW_SECS", "180"))

# Cooldowns for signal-driven refreshes. Soft signals only fire after a
# non-trivial gap; strong signals can fire sooner but still respect a small
# minimum window so we don't issue back-to-back refreshes for repeated
# evidence of the same shift.
MIN_REFRESH_GAP_SECS = float(os.getenv("TOPIC_TRACKER_MIN_REFRESH_GAP_SECS", "30"))
STRONG_REFRESH_GAP_SECS = float(os.getenv("TOPIC_TRACKER_STRONG_REFRESH_GAP_SECS", "10"))
# Urgent reasons can preempt the strong-refresh cooldown when the change is
# semantically definitive (a new chapter announcement, for example).
_URGENT_REFRESH_REASONS = frozenset({"passage_change"})

# Window inside which an identical (reason, strength, active_passage)
# signal is treated as a duplicate and dropped.
_SIGNAL_DEDUPE_WINDOW_SECS = float(os.getenv("TOPIC_TRACKER_SIGNAL_DEDUPE_SECS", "8"))

_VALID_STRENGTHS = frozenset({"none", "soft", "strong"})
_VALID_REFRESH_REASONS = frozenset({
    "passage_change",
    "mode_shift",
    "theme_shift",
    "illustration_started",
    "illustration_ended",
    "application_started",
    "exhortation_started",
    "altar_call_started",
    "closing_shift",
    "periodic",
    "first_summary",
    "manual",
})

# Bounded windows used in the layered prompt assembly (Phase 4).
_PROMPT_RECENT_SEGMENTS = 32
_PROMPT_RECENT_MODES = 6
_PROMPT_RECENT_REFRESH_REASONS = 4
_EVIDENCE_SEGMENT_BUFFER = 80


# ---------------------------------------------------------------------------
# Structured memory dataclasses (Phase 1)
# ---------------------------------------------------------------------------

@dataclass
class ActivePassageState:
    """Hard context: the explicit passage the preacher is expounding."""

    reference: str = ""
    canonical_english: str = ""
    confidence: str = ""              # "explicit" | "quoted" | ""
    source: str = ""                  # "verse_detection" | "summary_inference" | ""
    updated_at_ts: int = 0


@dataclass
class SermonStateMemory:
    """Semi-hard context: rhetorical framing of the current movement."""

    current_mode: str = "exposition"
    sermon_arc: str = ""
    rhetorical_goal: str = ""
    confidence: float = 0.0
    updated_at_ts: int = 0


@dataclass
class ThemeStateMemory:
    """Soft context: theological vocabulary continuity."""

    primary_themes: list[str] = field(default_factory=list)
    supporting_themes: list[str] = field(default_factory=list)
    theme_shift: bool = False
    updated_at_ts: int = 0


@dataclass
class IllustrationStateMemory:
    """Semi-hard context: whether the preacher is in narrative illustration."""

    active: bool = False
    subject: str | None = None
    started_at_ts: int | None = None
    updated_at_ts: int = 0


@dataclass
class SummaryStateMemory:
    """Human/operator facing summary plus fallback prompt context."""

    short_summary: str = ""
    long_summary: str = ""
    last_refresh_ts: int = 0
    refresh_reason: str = ""


@dataclass
class EvidenceMemory:
    """Bounded debugging/observability snapshots, not required downstream."""

    recent_segments: list[str] = field(default_factory=list)
    recent_mode_history: list[str] = field(default_factory=list)
    recent_trigger_reasons: list[str] = field(default_factory=list)


@dataclass
class TopicTrackerMemory:
    """Canonical structured sermon memory.

    Hard fields (``active_passage``, ``illustration_state``) are trusted by
    downstream consumers; soft fields (``theme_state``, ``summary_state``)
    are advisory.
    """

    active_passage: ActivePassageState = field(default_factory=ActivePassageState)
    sermon_state: SermonStateMemory = field(default_factory=SermonStateMemory)
    theme_state: ThemeStateMemory = field(default_factory=ThemeStateMemory)
    illustration_state: IllustrationStateMemory = field(default_factory=IllustrationStateMemory)
    summary_state: SummaryStateMemory = field(default_factory=SummaryStateMemory)
    evidence: EvidenceMemory = field(default_factory=EvidenceMemory)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# Backward-compatible thin view used by older prompt-assembly paths
# (LLMEnrichmentService still reads ``topic_tracker.get_context()`` while
# we migrate downstream consumers in Phase 6).
@dataclass
class SermonContext:
    """Backward-compat single-string view of the structured memory."""

    summary: str
    current_mode: str = "exposition"
    key_themes: list[str] = field(default_factory=list)
    illustration_subject: str | None = None
    sermon_arc: str = ""
    rhetorical_goal: str = ""

    def to_context_str(self) -> str:
        if self.illustration_subject:
            header = f"[ILLUSTRATION IN PROGRESS] {self.illustration_subject}"
            themes = (
                f"Key themes: {', '.join(self.key_themes)}." if self.key_themes else ""
            )
            arc_goal = ""
            if self.sermon_arc:
                arc_goal += f" Arc: {self.sermon_arc}."
            if self.rhetorical_goal:
                arc_goal += f" Goal: {self.rhetorical_goal}."
            return f"{header}\n{themes}{arc_goal}".strip()
        themes = (
            f" Key themes: {', '.join(self.key_themes)}." if self.key_themes else ""
        )
        arc_goal = ""
        if self.sermon_arc:
            arc_goal += f" Arc: {self.sermon_arc}."
        if self.rhetorical_goal:
            arc_goal += f" Goal: {self.rhetorical_goal}."
        return f"{self.summary}{themes}{arc_goal}".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _coerce_str_list(value, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            cleaned = item.strip()
            if cleaned:
                out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def _coerce_str(value, fallback: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    return fallback


# ---------------------------------------------------------------------------
# Prompt content (Phase 4)
# ---------------------------------------------------------------------------

_TOPIC_SYSTEM_PROMPT = (
    "You maintain a structured semantic-memory snapshot for a live Spanish "
    "sermon translation system. Read the prior memory, recent segments, mode "
    "history, and refresh trigger, and return ONLY a JSON object describing "
    "the current sermon state. Be brief, conservative, and stable. Preserve "
    "prior fields unless recent evidence clearly justifies changing them. "
    "Never hallucinate passage references. Mark illustration state only when "
    "narrative evidence is strong. Prefer continuity over novelty. Return ONLY "
    "valid JSON — no prose, no markdown fences."
)

_TOPIC_PROMPT_INSTRUCTIONS = (
    "Return ONLY valid JSON with these fields:\n"
    "- active_passage_override: object {reference, canonical_english} OR null. "
    "Only set when transcript evidence clearly contradicts the prior active "
    "passage; otherwise null. Never invent a citation.\n"
    "- sermon_arc: one of "
    "\"opening\"|\"development\"|\"climax\"|\"application\"|\"closing\"|\"altar_call\".\n"
    "- rhetorical_goal: one sentence describing what the preacher is trying "
    "to accomplish right now.\n"
    "- primary_themes: 1-4 short theme strings ranked by salience.\n"
    "- supporting_themes: 0-4 secondary theme strings.\n"
    "- illustration_state: object {active, subject}. active=true ONLY when the "
    "speaker is clearly in a narrative analogy or personal story; otherwise "
    "active=false and subject=null.\n"
    "- short_summary: 1-2 sentence theological summary suitable for prompt "
    "injection and operator display.\n"
    "- long_summary: 3-5 sentence reviewer-facing summary covering the whole "
    "sermon arc so far.\n"
    "- confidence: float 0..1 representing how confident you are in this "
    "snapshot relative to the recent transcript.\n"
    "- refresh_reason_echo: echo back the [REFRESH TRIGGER] reason or "
    "\"periodic\" when no specific reason was given.\n"
    "\nJSON schema:\n"
    "{ \"active_passage_override\": {\"reference\": string, \"canonical_english\": string} | null, "
    "\"sermon_arc\": string, "
    "\"rhetorical_goal\": string, "
    "\"primary_themes\": [string], "
    "\"supporting_themes\": [string], "
    "\"illustration_state\": {\"active\": boolean, \"subject\": string | null}, "
    "\"short_summary\": string, "
    "\"long_summary\": string, "
    "\"confidence\": number, "
    "\"refresh_reason_echo\": string }"
)


# ---------------------------------------------------------------------------
# TopicTracker
# ---------------------------------------------------------------------------

class TopicTracker:
    """Maintains a structured semantic memory of sermon content."""

    def __init__(
        self,
        church_id: str,
        sermon_topic: str = "",
        on_observability_event: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self._church_id = church_id
        self._sermon_topic = sermon_topic.strip()
        self._segments: list[str] = []
        self._current_mode: str = "exposition"
        self._mode_history: list[str] = []
        self._refresh_reason_history: list[str] = []
        self._memory = TopicTrackerMemory(
            summary_state=SummaryStateMemory(short_summary=self._sermon_topic),
        )
        self._session_start = time.monotonic()
        self._last_refresh_monotonic: float = 0.0
        self._update_task: asyncio.Task | None = None
        # Strongest pending refresh request held for re-run after the
        # in-flight task settles. Compared by strength precedence.
        self._pending_request: dict | None = None
        # Last accepted (reason, strength, active_passage_ref) tuple +
        # timestamp used to dedupe identical signals.
        self._last_accepted_signal: tuple[str, str, str] = ("", "", "")
        self._last_accepted_signal_ts: float = 0.0
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._on_observability_event = on_observability_event
        self._observability_seq = 0
        # Phase 0 metrics surface — surfaced via :meth:`get_metrics`.
        self.metrics: dict[str, int] = {
            "refresh_count": 0,
            "soft_refresh_count": 0,
            "strong_refresh_count": 0,
            "periodic_refresh_count": 0,
            "manual_refresh_count": 0,
            "refresh_suppressed_cooldown": 0,
            "refresh_suppressed_inflight": 0,
            "refresh_suppressed_dedupe": 0,
            "refresh_suppressed_invalid": 0,
            "summary_parse_failure_count": 0,
            "signal_received_total": 0,
            "refresh_latency_total_ms": 0,
            "refresh_latency_count": 0,
            "rerun_after_inflight_count": 0,
        }
        self._refresh_reason_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Inputs from the live pipeline
    # ------------------------------------------------------------------

    def add_segment(self, spanish_text: str, mode: str = "exposition") -> None:
        """Record a final transcript segment.

        The segment is appended to the bounded evidence window and may
        trigger the periodic-fallback schedule when no enrichment-driven
        refresh signal has fired recently.
        """

        self._segments.append(spanish_text)
        self._current_mode = mode
        self._mode_history.append(mode)
        # Keep the history bounded; this drives prompt evidence and
        # diagnostics output, both of which are intentionally short.
        if len(self._mode_history) > 64:
            self._mode_history = self._mode_history[-64:]
        self._maybe_schedule_periodic_update()

    def set_active_passage(self, reference: str, canonical_english: str) -> None:
        """Receive an active passage update from :class:`LLMEnrichmentService`.

        Hard-context updates skip the LLM round-trip so subsequent
        prompts know the current passage immediately, even before the
        next memory refresh fires.
        """

        ref = (reference or "").strip()
        eng = (canonical_english or "").strip()
        if not ref:
            return
        previous_ref = self._memory.active_passage.reference
        self._memory.active_passage = ActivePassageState(
            reference=ref,
            canonical_english=eng,
            confidence="explicit",
            source="verse_detection",
            updated_at_ts=_now_ms(),
        )
        logger.debug(
            "[topic] Active passage updated for church %s: %s (was %s)",
            self._church_id, ref, previous_ref or "none",
        )

    def request_refresh(
        self,
        *,
        reason: str,
        strength: str,
        ts: int | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Ask the tracker to consider a refresh based on a downstream signal.

        Parameters
        ----------
        reason:
            Normalized reason (see ``_VALID_REFRESH_REASONS``) describing
            *why* the caller thinks topic state may need to recompute.
        strength:
            ``"none"`` | ``"soft"`` | ``"strong"``.  ``none`` short-circuits
            to no-op. ``soft`` is gated by a longer cooldown.  ``strong``
            uses a shorter cooldown and may preempt for an allowlisted
            urgent reason such as ``passage_change``.
        ts:
            Optional caller-supplied wall-clock ms. Currently used only
            for downstream dedupe traces.
        metadata:
            Optional auxiliary context surfaced to observability.

        Returns
        -------
        str
            Outcome label. One of ``"scheduled"``, ``"queued_pending"``,
            ``"deduped"``, ``"cooldown"``, ``"in_flight"``, ``"ignored"``,
            ``"invalid"``.
        """

        self.metrics["signal_received_total"] += 1
        outcome = self._handle_refresh_request(
            reason=reason, strength=strength, ts=ts, metadata=metadata,
        )
        if outcome not in {"ignored", "invalid"}:
            logger.info(
                "[topic] refresh signal church=%s reason=%s strength=%s outcome=%s",
                self._church_id, reason, strength, outcome,
            )
        return outcome

    # ------------------------------------------------------------------
    # Public read surfaces
    # ------------------------------------------------------------------

    def get_context(self) -> str:
        """Backward-compat: flattened single-string sermon context.

        Older prompt-assembly paths still read this. Phase 6 migrates the
        primary structural prompt to :meth:`get_prompt_blocks_text`.
        """

        view = self._sermon_context_view()
        ctx = view.to_context_str()
        if self._memory.active_passage.reference:
            ctx = (
                f"Active scripture: {self._memory.active_passage.reference} — "
                f"{self._memory.active_passage.canonical_english}\n{ctx}"
            ).strip()
        return ctx

    def get_context_obj(self) -> SermonContext:
        """Backward-compat: structured one-shot view for legacy callers."""

        return self._sermon_context_view()

    def get_memory(self) -> TopicTrackerMemory:
        """Return the canonical structured sermon memory."""

        return self._memory

    def get_metrics(self) -> dict[str, object]:
        """Snapshot the metrics surface for diagnostics / logging."""

        return {
            **self.metrics,
            "refresh_reason_counts": dict(self._refresh_reason_counts),
            "active_passage_reference": self._memory.active_passage.reference,
            "current_mode": self._current_mode,
        }

    def get_prompt_blocks_text(self) -> str:
        """Return labeled prompt blocks for downstream prompt assembly.

        Each block is separated by a blank line, e.g.::

            [ACTIVE PASSAGE]
            John 3:16 — For God so loved...

            [SERMON MODE]
            exposition

            [SERMON ARC]
            development

            [PRIMARY THEMES]
            light, fellowship, confession

            [SERMON SUMMARY]
            The preacher is expounding 1 John 1...

        This shape is structured enough for the downstream prompt to
        consume hard and soft fields independently, while still being
        cheap to inject into a single prompt-cached prefix.
        """

        return "\n\n".join(self._build_prompt_block_parts())

    # ------------------------------------------------------------------
    # Internal: view conversions
    # ------------------------------------------------------------------

    def _sermon_context_view(self) -> SermonContext:
        summary = self._memory.summary_state.short_summary or self._sermon_topic
        illustration = (
            self._memory.illustration_state.subject
            if self._memory.illustration_state.active
            else None
        )
        return SermonContext(
            summary=summary,
            current_mode=self._current_mode,
            key_themes=list(self._memory.theme_state.primary_themes),
            illustration_subject=illustration,
            sermon_arc=self._memory.sermon_state.sermon_arc,
            rhetorical_goal=self._memory.sermon_state.rhetorical_goal,
        )

    def _build_prompt_block_parts(self) -> list[str]:
        parts: list[str] = []
        ap = self._memory.active_passage
        if ap.reference:
            text = f"{ap.reference}"
            if ap.canonical_english:
                text = f"{ap.reference} — {ap.canonical_english}"
            parts.append(f"[ACTIVE PASSAGE]\n{text}")

        if self._current_mode:
            parts.append(f"[SERMON MODE]\n{self._current_mode}")

        ss = self._memory.sermon_state
        if ss.sermon_arc:
            parts.append(f"[SERMON ARC]\n{ss.sermon_arc}")
        if ss.rhetorical_goal:
            parts.append(f"[RHETORICAL GOAL]\n{ss.rhetorical_goal}")

        themes = self._memory.theme_state.primary_themes
        if themes:
            parts.append(f"[PRIMARY THEMES]\n{', '.join(themes)}")

        ill = self._memory.illustration_state
        if ill.active:
            subject = ill.subject or "narrative illustration"
            parts.append(f"[ILLUSTRATION STATE]\nactive — {subject}")

        summary = self._memory.summary_state.short_summary
        if summary:
            parts.append(f"[SERMON SUMMARY]\n{summary}")

        return parts

    # ------------------------------------------------------------------
    # Scheduling (Phase 3)
    # ------------------------------------------------------------------

    def _interval(self) -> int:
        elapsed = time.monotonic() - self._session_start
        return (
            FAST_INTERVAL_SECS
            if elapsed < FAST_INTERVAL_LIMIT_SECS
            else SLOW_INTERVAL_SECS
        )

    def _maybe_schedule_periodic_update(self) -> None:
        now = time.monotonic()
        n = len(self._segments)
        first_run = (
            self._last_refresh_monotonic == 0.0
            and n >= FIRST_SUMMARY_MIN_SEGMENTS
        )
        subsequent = (
            n >= MIN_SEGMENTS_BEFORE_SUMMARY
            and (now - self._last_refresh_monotonic) >= self._interval()
        )
        if not (first_run or subsequent):
            return

        if self._update_task and not self._update_task.done():
            # Periodic clock tick during an in-flight refresh: keep the
            # current task running and remember a periodic rerun if
            # nothing stronger has been queued yet.
            if (
                self._pending_request is None
                or self._pending_request.get("strength") == "soft"
            ):
                self._pending_request = {
                    "strength": "soft",
                    "reason": "periodic",
                    "metadata": {},
                }
            return

        reason = "first_summary" if first_run else "periodic"
        self._schedule_refresh(reason=reason, strength="periodic", metadata={})

    def _handle_refresh_request(
        self,
        *,
        reason: str,
        strength: str,
        ts: int | None,
        metadata: dict | None,
    ) -> str:
        if strength not in _VALID_STRENGTHS or reason not in _VALID_REFRESH_REASONS:
            self.metrics["refresh_suppressed_invalid"] += 1
            return "invalid"
        if strength == "none":
            return "ignored"

        # If we lack enough material to summarise yet, only a strong urgent
        # signal forces an early summary; otherwise wait for the periodic path.
        if (
            len(self._segments) < FIRST_SUMMARY_MIN_SEGMENTS
            and not (strength == "strong" and reason in _URGENT_REFRESH_REASONS)
        ):
            self.metrics["refresh_suppressed_invalid"] += 1
            return "ignored"

        now = time.monotonic()
        active_ref = self._memory.active_passage.reference
        signal_key = (reason, strength, active_ref)
        if (
            self._last_accepted_signal == signal_key
            and (now - self._last_accepted_signal_ts) < _SIGNAL_DEDUPE_WINDOW_SECS
        ):
            self.metrics["refresh_suppressed_dedupe"] += 1
            return "deduped"

        is_urgent = strength == "strong" and reason in _URGENT_REFRESH_REASONS
        cooldown = (
            0.0
            if is_urgent
            else (
                STRONG_REFRESH_GAP_SECS
                if strength == "strong"
                else MIN_REFRESH_GAP_SECS
            )
        )
        if (
            self._last_refresh_monotonic > 0.0
            and (now - self._last_refresh_monotonic) < cooldown
        ):
            self.metrics["refresh_suppressed_cooldown"] += 1
            self._last_accepted_signal = signal_key
            self._last_accepted_signal_ts = now
            return "cooldown"

        if self._update_task and not self._update_task.done():
            # Park the request — it will rerun once the in-flight task
            # finishes, but only if nothing stronger arrives meanwhile.
            self._update_pending_request(reason=reason, strength=strength, metadata=metadata)
            self.metrics["refresh_suppressed_inflight"] += 1
            return "queued_pending"

        self._last_accepted_signal = signal_key
        self._last_accepted_signal_ts = now
        self._schedule_refresh(reason=reason, strength=strength, metadata=metadata or {})
        return "scheduled"

    def _strength_rank(self, strength: str) -> int:
        return {"strong": 3, "soft": 2, "periodic": 1, "none": 0}.get(strength, 0)

    def _update_pending_request(
        self,
        *,
        reason: str,
        strength: str,
        metadata: dict | None,
    ) -> None:
        new_rank = self._strength_rank(strength)
        existing = self._pending_request
        if existing is None or new_rank > self._strength_rank(existing.get("strength", "")):
            self._pending_request = {
                "strength": strength,
                "reason": reason,
                "metadata": dict(metadata or {}),
            }

    def _schedule_refresh(
        self,
        *,
        reason: str,
        strength: str,
        metadata: dict,
    ) -> None:
        # Update timing + counters BEFORE creating the task so callers
        # observe a stable post-schedule state even if the task races to
        # completion in unit tests.
        self._last_refresh_monotonic = time.monotonic()
        self.metrics["refresh_count"] += 1
        if strength == "strong":
            self.metrics["strong_refresh_count"] += 1
        elif strength == "soft":
            self.metrics["soft_refresh_count"] += 1
        elif strength == "periodic":
            self.metrics["periodic_refresh_count"] += 1
        elif strength == "manual":
            self.metrics["manual_refresh_count"] += 1
        self._refresh_reason_counts[reason] = (
            self._refresh_reason_counts.get(reason, 0) + 1
        )
        self._update_task = asyncio.create_task(
            self._run_refresh(reason=reason, strength=strength, metadata=metadata)
        )

    # ------------------------------------------------------------------
    # Refresh execution (Phase 4 + 5)
    # ------------------------------------------------------------------

    async def _run_refresh(
        self,
        *,
        reason: str,
        strength: str,
        metadata: dict,
    ) -> None:
        start = time.monotonic()
        try:
            await self._update_memory(reason=reason, strength=strength, metadata=metadata)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[topic] refresh task crashed for church %s reason=%s: %s",
                self._church_id, reason, exc,
            )
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self.metrics["refresh_latency_total_ms"] += elapsed_ms
            self.metrics["refresh_latency_count"] += 1
            self._refresh_reason_history.append(reason)
            if len(self._refresh_reason_history) > 32:
                self._refresh_reason_history = self._refresh_reason_history[-32:]
            await self._consume_pending_request()

    async def _consume_pending_request(self) -> None:
        pending = self._pending_request
        self._pending_request = None
        if not pending:
            return
        # Apply current cooldowns the same way fresh requests do; this
        # ensures we don't churn after a long-running refresh.
        outcome = self._handle_refresh_request(
            reason=pending.get("reason", "manual"),
            strength=pending.get("strength", "soft"),
            ts=None,
            metadata=pending.get("metadata"),
        )
        if outcome == "scheduled":
            self.metrics["rerun_after_inflight_count"] += 1

    async def _update_memory(
        self,
        *,
        reason: str,
        strength: str,
        metadata: dict,
    ) -> None:
        recent_segments = self._segments[-_EVIDENCE_SEGMENT_BUFFER:]
        recent_window = self._segments[-_PROMPT_RECENT_SEGMENTS:]
        recent_modes = self._mode_history[-_PROMPT_RECENT_MODES:]
        recent_reasons = self._refresh_reason_history[-_PROMPT_RECENT_REFRESH_REASONS:]
        elapsed_minutes = int((time.monotonic() - self._session_start) / 60)

        prior_memory = {
            "active_passage": asdict(self._memory.active_passage),
            "sermon_state": asdict(self._memory.sermon_state),
            "theme_state": asdict(self._memory.theme_state),
            "illustration_state": asdict(self._memory.illustration_state),
            "summary_state": asdict(self._memory.summary_state),
        }

        prompt_prefix = _TOPIC_PROMPT_INSTRUCTIONS
        layered_blocks = self._compose_layered_blocks(
            prior_memory=prior_memory,
            recent_window=recent_window,
            recent_modes=recent_modes,
            recent_reasons=recent_reasons,
            elapsed_minutes=elapsed_minutes,
            reason=reason,
            strength=strength,
        )
        layered_text = "\n\n".join(layered_blocks)

        call_id = self._next_observability_call_id("summary")
        await self._emit_observability_event(
            stage="summary.prompt",
            trace_kind="llm_prompt",
            summary="topic memory refresh prompt",
            call_id=call_id,
            data={
                "model": ANTHROPIC_MODEL,
                "max_tokens": TOPIC_TRACKER_MAX_TOKENS,
                "system": _TOPIC_SYSTEM_PROMPT,
                "system_truncated": False,
                "user": f"{prompt_prefix}\n\n{layered_text}",
                "user_truncated": False,
                "segment_count": len(self._segments),
                "current_mode": self._current_mode,
                "refresh_reason": reason,
                "refresh_strength": strength,
                "metadata": metadata or {},
            },
        )

        response = None
        raw_text = ""
        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=TOPIC_TRACKER_MAX_TOKENS,
                temperature=0,
                system=[_cached_text_block(_TOPIC_SYSTEM_PROMPT)],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            _cached_text_block(prompt_prefix),
                            {"type": "text", "text": layered_text},
                        ],
                    }
                ],
            )
            usage = getattr(response, "usage", None)
            logger.info(
                "[topic] Usage for church %s: input=%d output=%d cache_write=%d cache_read=%d",
                self._church_id,
                int(getattr(usage, "input_tokens", 0) or 0),
                int(getattr(usage, "output_tokens", 0) or 0),
                int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            )
            raw_text = response.content[0].text.strip()
            cleaned = raw_text
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.DOTALL).strip()
            parsed = json.loads(cleaned)
        except asyncio.CancelledError:
            raise
        except (json.JSONDecodeError, KeyError) as parse_exc:
            self.metrics["summary_parse_failure_count"] += 1
            await self._emit_observability_event(
                stage="summary.response",
                trace_kind="llm_response",
                summary="topic memory refresh parse failed",
                call_id=call_id,
                data={
                    "raw_response": raw_text,
                    "raw_response_truncated": False,
                    "parsed_json": None,
                    "error": str(parse_exc),
                },
            )
            if raw_text:
                # Soft fallback: keep the textual summary so older
                # consumers still see updated context, but leave
                # structured fields untouched.
                self._memory.summary_state.short_summary = raw_text[:400]
                self._memory.summary_state.last_refresh_ts = _now_ms()
                self._memory.summary_state.refresh_reason = reason
                await self._emit_memory_applied(reason=reason, strength=strength, fallback=True)
            logger.warning(
                "[topic] Could not parse structured memory for church %s — using fallback text",
                self._church_id,
            )
            return
        except Exception as exc:
            await self._emit_observability_event(
                stage="summary.error",
                trace_kind="error",
                summary="topic memory refresh failed",
                call_id=call_id,
                data={"error": str(exc)},
            )
            logger.warning(
                "[topic] memory refresh failed for church %s: %s", self._church_id, exc,
            )
            return

        usage = getattr(response, "usage", None)
        await self._emit_observability_event(
            stage="summary.response",
            trace_kind="llm_response",
            summary="topic memory refresh response parsed",
            call_id=call_id,
            data={
                "raw_response": raw_text,
                "raw_response_truncated": False,
                "parsed_json": parsed,
                "usage": {
                    "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                    "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                    "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
                    "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
                },
            },
        )

        self._apply_parsed_memory(parsed=parsed, reason=reason, recent_segments=recent_segments)
        await self._emit_memory_applied(reason=reason, strength=strength, fallback=False)

        logger.info(
            "[topic] Memory updated for church %s (mode=%s, reason=%s): %s",
            self._church_id,
            self._current_mode,
            reason,
            self._memory.summary_state.short_summary[:120],
        )

    def _apply_parsed_memory(
        self,
        *,
        parsed: dict,
        reason: str,
        recent_segments: list[str],
    ) -> None:
        now_ms = _now_ms()

        # active_passage: only override if the model explicitly identifies
        # a new passage *and* we don't already trust an explicit citation
        # (verse_detection wins). This preserves the "preserve continuity"
        # rule from the prompt.
        override = parsed.get("active_passage_override")
        if (
            isinstance(override, dict)
            and self._memory.active_passage.source != "verse_detection"
        ):
            ref = _coerce_str(override.get("reference"))
            eng = _coerce_str(override.get("canonical_english"))
            if ref:
                self._memory.active_passage = ActivePassageState(
                    reference=ref,
                    canonical_english=eng,
                    confidence="inferred",
                    source="summary_inference",
                    updated_at_ts=now_ms,
                )

        sermon_arc = _coerce_str(parsed.get("sermon_arc"), self._memory.sermon_state.sermon_arc)
        rhetorical_goal = _coerce_str(
            parsed.get("rhetorical_goal"), self._memory.sermon_state.rhetorical_goal,
        )
        confidence_raw = parsed.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else 0.0
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        self._memory.sermon_state = SermonStateMemory(
            current_mode=self._current_mode,
            sermon_arc=sermon_arc,
            rhetorical_goal=rhetorical_goal,
            confidence=confidence,
            updated_at_ts=now_ms,
        )

        primary_themes = _coerce_str_list(parsed.get("primary_themes"), limit=4)
        supporting_themes = _coerce_str_list(parsed.get("supporting_themes"), limit=4)
        prior_primary = self._memory.theme_state.primary_themes
        theme_shift = bool(prior_primary) and set(primary_themes) != set(prior_primary)
        self._memory.theme_state = ThemeStateMemory(
            primary_themes=primary_themes,
            supporting_themes=supporting_themes,
            theme_shift=theme_shift,
            updated_at_ts=now_ms,
        )

        illustration_block = parsed.get("illustration_state")
        if isinstance(illustration_block, dict):
            active = bool(illustration_block.get("active"))
            subject = illustration_block.get("subject")
            subject = _coerce_str(subject) if isinstance(subject, str) else None
            existing = self._memory.illustration_state
            started_at = existing.started_at_ts if existing.active else None
            if active and not existing.active:
                started_at = now_ms
            self._memory.illustration_state = IllustrationStateMemory(
                active=active,
                subject=subject if active else None,
                started_at_ts=started_at if active else None,
                updated_at_ts=now_ms,
            )

        short_summary = _coerce_str(
            parsed.get("short_summary"), self._memory.summary_state.short_summary,
        )
        long_summary = _coerce_str(
            parsed.get("long_summary"), self._memory.summary_state.long_summary,
        )
        self._memory.summary_state = SummaryStateMemory(
            short_summary=short_summary,
            long_summary=long_summary,
            last_refresh_ts=now_ms,
            refresh_reason=reason,
        )

        # Evidence is intentionally bounded — it is for diagnostics and
        # post-service review, not for downstream prompt injection.
        self._memory.evidence = EvidenceMemory(
            recent_segments=list(recent_segments[-12:]),
            recent_mode_history=list(self._mode_history[-_PROMPT_RECENT_MODES:]),
            recent_trigger_reasons=list(self._refresh_reason_history[-_PROMPT_RECENT_REFRESH_REASONS:] + [reason])[-_PROMPT_RECENT_REFRESH_REASONS:],
        )

    def _compose_layered_blocks(
        self,
        *,
        prior_memory: dict,
        recent_window: list[str],
        recent_modes: list[str],
        recent_reasons: list[str],
        elapsed_minutes: int,
        reason: str,
        strength: str,
    ) -> list[str]:
        blocks: list[str] = []

        if self._sermon_topic:
            blocks.append(f"[SERMON TOPIC]\n{self._sermon_topic}")

        # Layer 1: prior structured memory.
        prior_lines: list[str] = []
        ap = prior_memory["active_passage"]
        if ap.get("reference"):
            prior_lines.append(
                f"active_passage: {ap['reference']} — {ap.get('canonical_english', '')}"
            )
            if ap.get("source"):
                prior_lines.append(f"active_passage_source: {ap['source']}")
        ss = prior_memory["sermon_state"]
        if ss.get("sermon_arc"):
            prior_lines.append(f"sermon_arc: {ss['sermon_arc']}")
        if ss.get("rhetorical_goal"):
            prior_lines.append(f"rhetorical_goal: {ss['rhetorical_goal']}")
        ts = prior_memory["theme_state"]
        if ts.get("primary_themes"):
            prior_lines.append(f"primary_themes: {', '.join(ts['primary_themes'])}")
        if ts.get("supporting_themes"):
            prior_lines.append(f"supporting_themes: {', '.join(ts['supporting_themes'])}")
        ill = prior_memory["illustration_state"]
        if ill.get("active"):
            prior_lines.append(f"illustration_active: true ({ill.get('subject') or 'unspecified'})")
        else:
            prior_lines.append("illustration_active: false")
        sm = prior_memory["summary_state"]
        if sm.get("short_summary"):
            prior_lines.append(f"prior_short_summary: {sm['short_summary']}")
        if sm.get("long_summary"):
            prior_lines.append(f"prior_long_summary: {sm['long_summary']}")
        if prior_lines:
            blocks.append("[PRIOR MEMORY]\n" + "\n".join(prior_lines))

        # Layer 2: recent local window.
        if recent_window:
            blocks.append(
                "[RECENT SEGMENTS — most recent last]\n"
                + "\n".join(f"- {seg}" for seg in recent_window)
            )
        if recent_modes:
            blocks.append(
                "[RECENT MODE TRAJECTORY — most recent last]\n"
                + " → ".join(recent_modes)
            )

        # Layer 3: anchor events.
        if recent_reasons:
            blocks.append(
                "[RECENT REFRESH REASONS — most recent last]\n"
                + ", ".join(recent_reasons)
            )

        # Layer 4: metadata.
        meta_lines = [
            f"segment_count: {len(self._segments)}",
            f"elapsed_minutes_bucket: {elapsed_minutes}",
            f"current_mode: {self._current_mode}",
        ]
        blocks.append("[METADATA]\n" + "\n".join(meta_lines))

        blocks.append(
            "[REFRESH TRIGGER]\n"
            f"reason: {reason}\n"
            f"strength: {strength}"
        )
        return blocks

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def _next_observability_call_id(self, stage: str) -> str:
        self._observability_seq += 1
        return f"{stage}:{self._observability_seq}"

    async def _emit_observability_event(
        self,
        *,
        stage: str,
        trace_kind: str,
        summary: str,
        data: dict | None = None,
        call_id: str | None = None,
    ) -> None:
        if not self._on_observability_event:
            return
        payload: dict[str, object] = {
            "trace_stage": stage,
            "trace_kind": trace_kind,
            "summary": summary,
        }
        if data:
            payload["data"] = data
        if call_id:
            payload["call_id"] = call_id
        try:
            await self._on_observability_event(payload)
        except Exception as exc:
            logger.debug(
                "[topic] observability emit failed for church %s stage=%s: %s",
                self._church_id, stage, exc,
            )

    async def _emit_memory_applied(
        self,
        *,
        reason: str,
        strength: str,
        fallback: bool,
    ) -> None:
        await self._emit_observability_event(
            stage="summary.applied",
            trace_kind="decision",
            summary=(
                "topic memory refresh applied (fallback)"
                if fallback
                else "topic memory refresh applied"
            ),
            data={
                "memory": self._memory.to_dict(),
                "reason": reason,
                "strength": strength,
                "fallback": fallback,
                "metrics": {
                    "refresh_count": self.metrics["refresh_count"],
                    "soft_refresh_count": self.metrics["soft_refresh_count"],
                    "strong_refresh_count": self.metrics["strong_refresh_count"],
                    "periodic_refresh_count": self.metrics["periodic_refresh_count"],
                    "refresh_suppressed_cooldown": self.metrics["refresh_suppressed_cooldown"],
                    "refresh_suppressed_inflight": self.metrics["refresh_suppressed_inflight"],
                    "refresh_suppressed_dedupe": self.metrics["refresh_suppressed_dedupe"],
                    "summary_parse_failure_count": self.metrics["summary_parse_failure_count"],
                },
            },
        )
        # Phase 7: dedicated topic_state event so diagnostics can render a
        # live semantic-memory card without parsing the summary trace.
        await self._emit_observability_event(
            stage="topic_state",
            trace_kind="state",
            summary="topic semantic memory snapshot",
            data={
                "memory": self._memory.to_dict(),
                "reason": reason,
                "strength": strength,
                "fallback": fallback,
            },
        )

    async def stop(self) -> None:
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
