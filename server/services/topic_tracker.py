import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import anthropic

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
PROMPT_CACHE_TTL = os.getenv("ANTHROPIC_PROMPT_CACHE_TTL", "5m")
_PROMPT_CACHE_CONTROL = {"type": "ephemeral", "ttl": PROMPT_CACHE_TTL}


def _cached_text_block(text: str) -> dict[str, object]:
    return {
        "type": "text",
        "text": text,
        "cache_control": _PROMPT_CACHE_CONTROL,
    }

FIRST_SUMMARY_MIN_SEGMENTS = 3    # fire once early to capture the passage announcement
MIN_SEGMENTS_BEFORE_SUMMARY = 8   # subsequent summaries require more content

# Update frequently for the first FAST_INTERVAL_LIMIT_SECS of the sermon to
# capture mode transitions quickly, then relax to avoid redundant LLM calls.
FAST_INTERVAL_SECS = 60           # first 10 minutes: update every minute
FAST_INTERVAL_LIMIT_SECS = 600    # 10 minutes
SLOW_INTERVAL_SECS = 180          # after 10 minutes: update every 3 minutes


@dataclass
class SermonContext:
    """Structured snapshot of the current sermon state, used as enrichment prompt context."""
    summary: str                          # 2-3 sentence theological summary
    current_mode: str = "exposition"      # current settled sermon mode
    key_themes: list[str] = field(default_factory=list)  # ["fellowship", "walking in light"]
    illustration_subject: str | None = None  # set only when mode == "illustration"
    sermon_arc: str = ""                  # where we are in the sermon arc (e.g. "opening", "climax")
    rhetorical_goal: str = ""             # what the preacher is trying to achieve right now

    def to_context_str(self) -> str:
        """Format for injection into the enrichment LLM prompt."""
        if self.illustration_subject:
            header = f"[ILLUSTRATION IN PROGRESS] {self.illustration_subject}"
            themes = f"Key themes: {', '.join(self.key_themes)}." if self.key_themes else ""
            arc_goal = ""
            if self.sermon_arc:
                arc_goal += f" Arc: {self.sermon_arc}."
            if self.rhetorical_goal:
                arc_goal += f" Goal: {self.rhetorical_goal}."
            return f"{header}\n{themes}{arc_goal}".strip()
        themes = f" Key themes: {', '.join(self.key_themes)}." if self.key_themes else ""
        arc_goal = ""
        if self.sermon_arc:
            arc_goal += f" Arc: {self.sermon_arc}."
        if self.rhetorical_goal:
            arc_goal += f" Goal: {self.rhetorical_goal}."
        return f"{self.summary}{themes}{arc_goal}".strip()


class TopicTracker:
    """Maintains a rolling structured summary of sermon content.

    Updated on an adaptive schedule: every 60 seconds for the first 10 minutes
    of the sermon (to capture mode transitions quickly), then every 3 minutes.
    An eager first summary fires after only 3 segments to capture the passage
    announcement before the first interval would kick in.

    The tracker accepts a current_mode hint on each add_segment call so the
    LLM can frame the summary with awareness of whether we are in exposition,
    illustration, etc. The summary is injected into each enrichment prompt as
    [SERMON CONTEXT], providing the model with the theological framing needed
    to correctly classify vocabulary and avoid false verse detections.
    """

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
        # Seed with sermon topic so context is useful from the very first call
        self._context = SermonContext(
            summary=sermon_topic.strip(),
            current_mode="exposition",
            sermon_arc="",
            rhetorical_goal="",
        )
        self._session_start: float = time.monotonic()
        self._last_summary_time: float = 0.0
        self._update_task: asyncio.Task | None = None
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._on_observability_event = on_observability_event
        self._observability_seq = 0

    def add_segment(self, spanish_text: str, mode: str = "exposition") -> None:
        """Record a final transcript segment and schedule a summary refresh if due."""
        self._segments.append(spanish_text)
        self._current_mode = mode
        self._maybe_schedule_update()

    def set_active_passage(self, reference: str, canonical_english: str) -> None:
        """Receive an active passage update from LLMEnrichmentService.

        Injects the current scripture reference into the sermon context so
        subsequent enrichment prompts know what passage is being expounded,
        even before the next TopicTracker summary fires.
        """
        self._active_passage_ref = reference
        self._active_passage_en = canonical_english
        logger.debug(
            "[topic] Active passage updated for church %s: %s",
            self._church_id, reference,
        )

    def get_context(self) -> str:
        """Return the current context string for injection into the enrichment prompt."""
        ctx = self._context.to_context_str()
        if getattr(self, "_active_passage_ref", None):
            ctx = (
                f"Active scripture: {self._active_passage_ref} — {self._active_passage_en}\n{ctx}"
            ).strip()
        return ctx

    def get_context_obj(self) -> SermonContext:
        """Return the full structured context object."""
        return self._context

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
                self._church_id,
                stage,
                exc,
            )

    def _interval(self) -> int:
        """Return the appropriate update interval based on elapsed session time."""
        elapsed = time.monotonic() - self._session_start
        return FAST_INTERVAL_SECS if elapsed < FAST_INTERVAL_LIMIT_SECS else SLOW_INTERVAL_SECS

    def _maybe_schedule_update(self) -> None:
        now = time.monotonic()
        n = len(self._segments)
        first_run = (self._last_summary_time == 0.0 and n >= FIRST_SUMMARY_MIN_SEGMENTS)
        subsequent = (
            n >= MIN_SEGMENTS_BEFORE_SUMMARY
            and (now - self._last_summary_time) >= self._interval()
        )

        if first_run or subsequent:
            if not self._update_task or self._update_task.done():
                self._last_summary_time = now
                self._update_task = asyncio.create_task(self._update_summary())

    async def _update_summary(self) -> None:
        # Use the last ~80 segments (~5-8 minutes of speech)
        recent_text = " ".join(self._segments[-80:])
        topic_hint = f' The sermon topic is: "{self._sermon_topic}".' if self._sermon_topic else ""
        mode_hint = f" The current sermon mode appears to be: {self._current_mode}."

        response = None
        call_id = self._next_observability_call_id("summary")
        try:
            prompt_prefix = (
                f"Analyze this sermon transcript and return a JSON object with these fields:\n"
                f"- summary: 2-3 sentences on theological themes and current direction\n"
                f"- key_themes: array of 2-4 short theme strings\n"
                f"- illustration_subject: if the pastor is currently telling a personal "
                f"story or analogy, describe it in one sentence; otherwise null\n"
                f"- sermon_arc: where the sermon is in its arc — one of: "
                f'"opening", "development", "climax", "application", "closing", "altar_call"\n'
                f"- rhetorical_goal: one sentence describing what the preacher is trying "
                f"to accomplish right now (e.g. 'establishing biblical authority for the main claim', "
                f"'moving congregation toward repentance', 'illustrating grace with a personal story')\n\n"
                f"JSON schema: "
                f'{{ "summary": "string", "key_themes": ["string"], '
                f'"illustration_subject": "string | null", '
                f'"sermon_arc": "string", "rhetorical_goal": "string" }}'
            )
            transcript_block = f"Transcript:{topic_hint}{mode_hint}\n{recent_text}"
            system_prompt = (
                "You summarize live Spanish sermon transcripts for a simultaneous interpreter. "
                "Be brief and precise. Return ONLY valid JSON â€” no prose, no markdown fences."
            )
            await self._emit_observability_event(
                stage="summary.prompt",
                trace_kind="llm_prompt",
                summary="rolling sermon summary prompt",
                call_id=call_id,
                data={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 300,
                    "system": system_prompt,
                    "system_truncated": False,
                    "user": f"{prompt_prefix}\n\n{transcript_block}",
                    "user_truncated": False,
                    "segment_count": len(self._segments),
                    "current_mode": self._current_mode,
                },
            )
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=300,
                temperature=0,
                system=[
                    _cached_text_block(
                        "You summarize live Spanish sermon transcripts for a simultaneous interpreter. "
                        "Be brief and precise. Return ONLY valid JSON — no prose, no markdown fences."
                    )
                ],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            _cached_text_block(prompt_prefix),
                            {"type": "text", "text": transcript_block},
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
            raw = response.content[0].text.strip()
            raw_before_strip = raw
            # Strip markdown fences if model adds them despite instructions
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

            parsed = json.loads(raw)
            await self._emit_observability_event(
                stage="summary.response",
                trace_kind="llm_response",
                summary="rolling sermon summary response parsed",
                call_id=call_id,
                data={
                    "raw_response": raw_before_strip,
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
            new_context = SermonContext(
                summary=parsed.get("summary", self._context.summary),
                current_mode=self._current_mode,
                key_themes=parsed.get("key_themes", []),
                illustration_subject=parsed.get("illustration_subject") or None,
                sermon_arc=parsed.get("sermon_arc", ""),
                rhetorical_goal=parsed.get("rhetorical_goal", ""),
            )
            self._context = new_context
            await self._emit_observability_event(
                stage="summary.applied",
                trace_kind="decision",
                summary="rolling sermon summary applied",
                call_id=call_id,
                data={
                    "context": {
                        "summary": new_context.summary,
                        "current_mode": new_context.current_mode,
                        "key_themes": new_context.key_themes,
                        "illustration_subject": new_context.illustration_subject,
                        "sermon_arc": new_context.sermon_arc,
                        "rhetorical_goal": new_context.rhetorical_goal,
                    },
                },
            )
            logger.info(
                "[topic] Context updated for church %s (mode=%s): %s",
                self._church_id, self._current_mode, new_context.to_context_str()[:120],
            )
        except (json.JSONDecodeError, KeyError):
            # Graceful fallback: store the raw text as a plain summary
            raw_text = response.content[0].text.strip() if response is not None else ""
            await self._emit_observability_event(
                stage="summary.response",
                trace_kind="llm_response",
                summary="rolling sermon summary parse failed",
                call_id=call_id,
                data={
                    "raw_response": raw_text,
                    "raw_response_truncated": False,
                    "parsed_json": None,
                },
            )
            if raw_text:
                self._context = SermonContext(
                    summary=raw_text,
                    current_mode=self._current_mode,
                )
                await self._emit_observability_event(
                    stage="summary.applied",
                    trace_kind="decision",
                    summary="rolling sermon summary applied from fallback text",
                    call_id=call_id,
                    data={
                        "context": {
                            "summary": self._context.summary,
                            "current_mode": self._context.current_mode,
                            "key_themes": self._context.key_themes,
                            "illustration_subject": self._context.illustration_subject,
                            "sermon_arc": self._context.sermon_arc,
                            "rhetorical_goal": self._context.rhetorical_goal,
                        },
                    },
                )
            logger.warning(
                "[topic] Could not parse structured context for church %s — using plain text",
                self._church_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._emit_observability_event(
                stage="summary.error",
                trace_kind="error",
                summary="rolling sermon summary update failed",
                call_id=call_id,
                data={"error": str(e)},
            )
            logger.warning("[topic] Context update failed for church %s: %s", self._church_id, e)

    async def stop(self) -> None:
        if self._update_task and not self._update_task.done():
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
