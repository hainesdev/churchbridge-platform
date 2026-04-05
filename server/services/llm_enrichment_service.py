import asyncio
import json
import logging
import os
import re
from collections import deque
from dataclasses import dataclass
from typing import Callable, Awaitable, TYPE_CHECKING

import anthropic

from server.db.verses import save_verse_detection, save_verse_suggestions

if TYPE_CHECKING:
    from server.services.topic_tracker import TopicTracker
    from server.services.sermon_state_tracker import SermonStateTracker

logger = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
MAX_ENRICHMENT_TOKENS = 900
_VALID_SERMON_MODES = frozenset({
    "scripture", "exposition", "illustration", "application", "exhortation", "procedural"
})
VERSE_GAP_THRESHOLD_S = float(os.getenv("VERSE_GAP_THRESHOLD_S", "45"))


@dataclass
class VerseScratchEntry:
    book: str
    chapter: int
    verse_start: int
    verse_end: int | None
    confidence: str           # "explicit" | "quoted"
    reference: str
    canonical_english: str
    spanish_text: str
    audio_start: float        # Deepgram stream seconds (sermon-relative, reconnect-normalized)
    audio_end: float
    ts: int                   # wall-clock ms for client broadcast keying


_SYSTEM_PROMPT_BASE = """\
You are a bilingual (Spanish/English) theological assistant helping a live church sermon translation system.

{glossary_block}

Your job is to analyze one sentence from a live Spanish sermon and return a JSON object with the fields below.

RULES:
1. Output ONLY valid JSON. No prose, no markdown fences, no code blocks.

2. improved_translation: provide a better English rendering than the Google Translate output if needed.
   Preserve the preaching register — declarative, present tense, active voice where natural.
   If Google's translation is already excellent, return it unchanged.
   Common Spanish Pentecostal sermon interjections ("Santo", "Aleluya", "Gloria", "Amén") that appear
   mid-sentence and disrupt grammatical flow should be silently removed from the translation — they are
   STT artifacts of the preaching register, not content words.
   When [PREVIOUS SENTENCE DISCOURSE] shows thought_complete: false, check whether the current sentence
   completes that prior thought. If so, unify them naturally in improved_translation rather than
   treating the current sentence in isolation.
   When [PREVIOUS SENTENCE DISCOURSE] shows discourse_tag: "rhetorical_question", the current sentence
   is likely the answer. Keep improved_translation crisp and direct — it answers the previous question.
   When [PREVIOUS SENTENCE DISCOURSE] shows introduces_quote: true, the current sentence is quoted
   scripture. Use formal, present-tense, verbatim-cadence register in the translation.

3. discourse_tag: classify the rhetorical function of this sentence. Choose exactly one:
   - "statement":          a declarative theological claim or exposition ("God is light")
   - "rhetorical_question": a question the preacher asks but answers themselves ("Who is he?")
   - "answer_to_question":  a direct answer to the preacher's own question ("Jesus Christ.")
   - "quote_introduction":  the preacher is about to quote scripture ("Juan dice...", "dice aquí...",
                             "la Biblia dice...", "como dice en...", "versículo dice...", "leemos que...")
   - "scripture_quote":     the preacher is reciting or reading Bible text verbatim
   - "transition":          moving between topics, passages, or sermon sections
   - "exhortation_appeal":  direct appeal to the congregation ("Come to him", "Do not be afraid")

4. introduces_quote: true if this sentence contains a quote introduction marker such as:
   "Juan dice", "Pedro dice", "Pablo dice", "dice aquí", "dice la Biblia", "la Biblia dice",
   "como dice en", "versículo dice", "leemos que", "escrito está", "está escrito".
   false otherwise.

5. thought_complete: true if this sentence expresses a complete thought on its own.
   false if it ends mid-clause — a preposition, subordinating conjunction, or relative pronoun
   with no resolution (e.g. "Porque anoche se acuerdan que él", "Si nosotros decimos que").

6. verse_detected: detect a Bible reference if the speaker explicitly announces or clearly quotes one
   in any of these forms:
   - Chapter + verse:   "Juan 3:16", "Romanos 8, versículo 28", "Apocalipsis capítulo 1 versículo 5"
   - Chapter-only:      "primera de Juan, capítulo 1", "Salmos capítulo 22", "vamos a Mateo capítulo cinco"
   - Ordinal book ref:  "primera de Juan dice...", "en segunda de Corintios capítulo 5"
   Spanish ordinal book names →
     primera/segunda/tercera de Juan = 1/2/3 John
     primera/segunda de Pedro = 1/2 Peter
     primera/segunda de Corintios = 1/2 Corinthians
     primera/segunda de Tesalonicenses = 1/2 Thessalonians
     primera/segunda de Timoteo = 1/2 Timothy
     primera/segunda de Samuel = 1/2 Samuel
     primera/segunda de Reyes = 1/2 Kings
     Apocalipsis = Revelation; Efesios = Ephesians; Filipenses = Philippians
     Gálatas = Galatians; Colosenses = Colossians; Filemón = Philemon
   For chapter-only citations: set verse_start=1, verse_end=null, reference="1 John 1", confidence="explicit".
   For clearly quoted verse text: confidence="quoted".
   IMPORTANT: Do NOT infer a verse citation from standalone theological vocabulary words
   ("Pentecostés", "bautismo", "adoración", "gracia") even if they reference a biblical event or holiday.
   A valid citation requires an explicit book + chapter/verse announcement, or a clear quotation of
   specific verse text. The word "Pentecostés" alone is never a citation of Acts 2.
   Never hallucinate — if uncertain return null.

7. verse_suggestions: based on the theological theme of this sentence and sermon context, suggest 1–3
   related Bible verses the congregation would find meaningful. Use NIV text for canonical_english.
   NEVER suggest a verse already listed in [ALREADY SUGGESTED] or the current [ACTIVE PASSAGE].
   Prefer thematic cross-references over the same book being expounded. Vary suggestions across sentences.
   Return [] if the sentence is procedural or non-theological.

8. sermon_mode: classify this sentence into exactly one of these modes:
   - "scripture":    pastor is directly reading or reciting Bible text verbatim
   - "exposition":   explaining, commenting on, or unpacking a biblical passage
   - "illustration": personal story, anecdote, analogy, or parable — even if theological
                     vocabulary is present. Use this whenever the speaker shifts to past-tense personal narrative.
   - "application":  applying the text to the congregation's situation or behavior
   - "exhortation":  emotional appeal, motivational call, altar invitation
   - "procedural":   logistics, worship direction, prayer cues (e.g. "please stand", "let us pray")

9. Use English book names in all references (e.g. "John", "Romans", "Revelation").
10. Infer chapter/verse from quoted text only when highly confident.

JSON schema (return exactly this shape):
{
  "improved_translation": "string",
  "discourse_tag": "statement" | "rhetorical_question" | "answer_to_question" | "quote_introduction" | "scripture_quote" | "transition" | "exhortation_appeal",
  "introduces_quote": true | false,
  "thought_complete": true | false,
  "sermon_mode": "scripture" | "exposition" | "illustration" | "application" | "exhortation" | "procedural",
  "verse_detected": {
    "book": "string",
    "chapter": integer,
    "verse_start": integer,
    "verse_end": integer | null,
    "spanish_text": "string",
    "canonical_english": "string",
    "reference": "string",
    "confidence": "explicit" | "quoted"
  } | null,
  "verse_suggestions": [
    {
      "reference": "string",
      "canonical_english": "string",
      "relevance_note": "string"
    }
  ]
}\
"""


def _build_system_prompt(church_terms: dict[str, str]) -> str:
    if church_terms:
        lines = "\n".join(f"  {es} → {en}" for es, en in church_terms.items())
        glossary_block = f"THEOLOGICAL GLOSSARY — always use these exact translations:\n{lines}"
    else:
        glossary_block = ""
    return _SYSTEM_PROMPT_BASE.replace("{glossary_block}", glossary_block)


def _build_user_message(
    spanish: str,
    google_english: str,
    topic_context: str,
    sentence_history: list[tuple[str, str]],
    active_passage: dict | None,
    shown_suggestions: set[str],
    current_mode_label: str = "",
    prev_discourse: dict | None = None,
) -> str:
    parts: list[str] = []

    if topic_context:
        parts.append(f"[SERMON CONTEXT]\n{topic_context}")

    if current_mode_label:
        parts.append(f"[CURRENT MODE]\n{current_mode_label}")

    if active_passage:
        parts.append(
            f"[ACTIVE PASSAGE]\n"
            f"The preacher is currently expounding: "
            f"{active_passage['reference']} — {active_passage['canonical_english']}"
        )

    if shown_suggestions:
        parts.append(f"[ALREADY SUGGESTED]\n{', '.join(sorted(shown_suggestions))}")

    if sentence_history:
        lines = []
        for sp, en in sentence_history:
            lines.append(f"  ES: {sp}")
            lines.append(f"  EN: {en}")
        parts.append("[RECENT SENTENCES — most recent last]\n" + "\n".join(lines))

    if prev_discourse:
        tag = prev_discourse.get("discourse_tag", "statement")
        introduces = prev_discourse.get("introduces_quote", False)
        complete = prev_discourse.get("thought_complete", True)
        parts.append(
            f"[PREVIOUS SENTENCE DISCOURSE]\n"
            f"discourse_tag: {tag}\n"
            f"introduces_quote: {str(introduces).lower()}\n"
            f"thought_complete: {str(complete).lower()}"
        )

    parts.append(f"[SOURCE — Spanish original]\n{spanish}")
    parts.append(f"[GOOGLE TRANSLATION — may need improvement]\n{google_english}")
    return "\n\n".join(parts)


class LLMEnrichmentService:
    """Post-translation enrichment via Claude structured output.

    Called once per committed sentence (accurate track only).
    Always runs as a fire-and-forget background task — never blocks Google translation.

    For each sentence it:
    - Improves the Google translation where possible → fires on_translation_update
    - Detects explicit or quoted Bible verse references → accumulated in scratch pad,
      flushed as consolidated ranges → fires on_verse_detected
    - Suggests 1–3 related verses based on theme → fires on_verse_suggestion
    - Signals enrichment settled → fires on_enrichment_settled (used to suppress
      stale Google dual-pass corrections)
    """

    def __init__(
        self,
        church_id: str,
        church_terms: dict[str, str],
        topic_tracker: "TopicTracker",
        on_translation_update: Callable[[int, str], Awaitable[None]],
        on_verse_detected: Callable[[int, dict], Awaitable[None]],
        on_verse_range_update: Callable[[int, dict], Awaitable[None]],
        on_verse_suggestion: Callable[[int, list[dict]], Awaitable[None]],
        on_enrichment_settled: Callable[[int], Awaitable[None]],
        session_id: int,
        state_tracker: "SermonStateTracker | None" = None,
    ):
        self._church_id = church_id
        self._topic_tracker = topic_tracker
        self._state_tracker = state_tracker
        self._on_translation_update = on_translation_update
        self._on_verse_detected = on_verse_detected
        self._on_verse_range_update = on_verse_range_update
        self._on_verse_suggestion = on_verse_suggestion
        self._on_enrichment_settled = on_enrichment_settled
        self._session_id = session_id
        self._system_prompt = _build_system_prompt(church_terms)
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._tasks: list[asyncio.Task] = []
        # Serializes post-completion state mutations so concurrent tasks that finish
        # out of order (due to variable API latency) don't corrupt sentence history,
        # active passage, shown suggestions, or sermon mode signals.
        self._mutation_lock = asyncio.Lock()

        # Rolling sentence context (last 3 sentences, Spanish + best English)
        self._sentence_history: deque[tuple[str, str]] = deque(maxlen=3)
        # Most recent explicit verse citation — injected into every subsequent prompt
        self._active_passage: dict | None = None
        # All references suggested this session — prevents repetition
        self._shown_suggestions: set[str] = set()
        # Discourse output of the previous enriched sentence — injected as forward context
        self._prev_discourse: dict | None = None

        # Verse scratch pad — accumulates detections for temporal range consolidation
        self._verse_scratch: list[VerseScratchEntry] = []

    def enrich(
        self,
        spanish: str,
        google_english: str,
        ts: int,
        audio_start: float = 0.0,
        audio_end: float = 0.0,
    ) -> asyncio.Task:
        """Schedule enrichment as a fire-and-forget task. Does not block."""
        task = asyncio.create_task(
            self._run_enrichment(spanish, google_english, ts, audio_start, audio_end)
        )
        self._tasks = [t for t in self._tasks if not t.done()]
        self._tasks.append(task)
        return task

    async def _run_enrichment(
        self,
        spanish: str,
        google_english: str,
        ts: int,
        audio_start: float,
        audio_end: float,
    ) -> None:
        topic_context = self._topic_tracker.get_context()
        # Snapshot mutable state before the await so concurrent tasks don't interfere
        history = list(self._sentence_history)
        active_passage = self._active_passage
        shown = set(self._shown_suggestions)
        prev_discourse = self._prev_discourse
        current_mode_label = (
            self._state_tracker.get_context_label() if self._state_tracker else ""
        )

        logger.debug(
            "[enrichment:%s] Enriching ts=%d at audio=%.1f–%.1fs | "
            "history=%d prior sentence(s) | active_passage=%s | shown=%d suggestion(s) | mode=%s",
            self._church_id, ts, audio_start, audio_end,
            len(history),
            active_passage["reference"] if active_passage else "none",
            len(shown),
            current_mode_label or "unknown",
        )

        user_msg = _build_user_message(
            spanish, google_english, topic_context, history, active_passage, shown,
            current_mode_label, prev_discourse,
        )

        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=MAX_ENRICHMENT_TOKENS,
                temperature=0,
                system=self._system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[enrichment:%s] Claude call failed for ts=%d: %s", self._church_id, ts, e)
            return

        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[enrichment:%s] Could not parse JSON for ts=%d: %.120s", self._church_id, ts, raw)
            return

        if not isinstance(result, dict):
            logger.warning("[enrichment:%s] Expected JSON object for ts=%d, got %s", self._church_id, ts, type(result).__name__)
            return

        # Acquire the mutation lock before touching any shared state.
        # Concurrent enrichment tasks complete in arbitrary order due to variable
        # API latency; the lock ensures sentence history, active passage, shown
        # suggestions, and mode signals are always updated in arrival order.
        async with self._mutation_lock:
            # --- Discourse tagging ---
            discourse_tag = result.get("discourse_tag", "statement")
            introduces_quote = bool(result.get("introduces_quote", False))
            thought_complete = bool(result.get("thought_complete", True))
            logger.info(
                "[enrichment:%s] Discourse ts=%d: tag=%s introduces_quote=%s complete=%s",
                self._church_id, ts, discourse_tag, introduces_quote, thought_complete,
            )
            # Store for injection into the next sentence's prompt
            self._prev_discourse = {
                "discourse_tag": discourse_tag,
                "introduces_quote": introduces_quote,
                "thought_complete": thought_complete,
            }

            # --- Sermon mode ---
            sermon_mode = result.get("sermon_mode", "exposition")
            if sermon_mode not in _VALID_SERMON_MODES:
                sermon_mode = "exposition"
            logger.info("[enrichment:%s] Sermon mode ts=%d: %s", self._church_id, ts, sermon_mode)
            if self._state_tracker:
                await self._state_tracker.add_signal(sermon_mode, ts)

            # --- Translation improvement ---
            improved = result.get("improved_translation", "").strip()
            if improved and improved != google_english:
                logger.info(
                    "[enrichment:%s] Translation improved ts=%d:\n  google: %s\n     llm: %s",
                    self._church_id, ts, google_english[:80], improved[:80],
                )
                try:
                    await self._on_translation_update(ts, improved)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_translation_update failed: %s", self._church_id, e)
            else:
                logger.info("[enrichment:%s] Translation accepted ts=%d — no change", self._church_id, ts)

            # Signal settled in both cases so the correction guard fires correctly
            try:
                await self._on_enrichment_settled(ts)
            except Exception as e:
                logger.warning("[enrichment:%s] on_enrichment_settled failed: %s", self._church_id, e)

            # Append to sentence history using the best available translation
            best_english = improved if (improved and improved != google_english) else google_english
            self._sentence_history.append((spanish, best_english))

            # --- Verse detection ---
            verse = result.get("verse_detected")
            if verse and isinstance(verse, dict):
                if _is_valid_verse(verse):
                    logger.info(
                        "[enrichment:%s] Verse detected ts=%d: %s (%s) at audio=%.1f–%.1fs",
                        self._church_id, ts, verse["reference"], verse["confidence"],
                        audio_start, audio_end,
                    )
                    # Advance active passage on explicit citations (chapter announcements or full refs)
                    if verse.get("confidence") == "explicit":
                        prev = self._active_passage
                        if (prev is None
                                or prev["book"] != verse["book"]
                                or prev["chapter"] != verse["chapter"]):
                            self._active_passage = verse
                            logger.info(
                                "[enrichment:%s] Active passage → %s (was: %s)",
                                self._church_id, verse["reference"],
                                prev["reference"] if prev else "none",
                            )
                            self._topic_tracker.set_active_passage(
                                verse["reference"], verse["canonical_english"]
                            )
                    await self._handle_verse_detection(verse, audio_start, audio_end, ts)
                else:
                    logger.debug(
                        "[enrichment:%s] Verse shape failed validation ts=%d: %s",
                        self._church_id, ts, _describe_invalid_verse(verse),
                    )
                    logger.info("[enrichment:%s] No verse detected ts=%d", self._church_id, ts)
            else:
                logger.info("[enrichment:%s] No verse detected ts=%d", self._church_id, ts)

            # --- Verse suggestions ---
            suggestions = result.get("verse_suggestions", [])
            if isinstance(suggestions, list):
                # Gate: suppress suggestions during narrative/exhortation/procedural modes
                if self._state_tracker and not self._should_suggest():
                    logger.info(
                        "[enrichment:%s] Suggestions suppressed ts=%d — mode=%s",
                        self._church_id, ts, self._state_tracker.settled_mode,
                    )
                    suggestions = []

                active_ref = (self._active_passage or {}).get("reference")
                valid = []
                suppressed = []
                for s in suggestions:
                    if not _is_valid_suggestion(s):
                        continue
                    if s["reference"] in self._shown_suggestions:
                        suppressed.append((s["reference"], "already shown"))
                    elif s["reference"] == active_ref:
                        suppressed.append((s["reference"], "active passage"))
                    else:
                        valid.append(s)

                if suppressed:
                    logger.debug(
                        "[enrichment:%s] Suggestions suppressed ts=%d: %s",
                        self._church_id, ts,
                        ", ".join(f"{ref} ({reason})" for ref, reason in suppressed),
                    )

                if valid:
                    for s in valid:
                        self._shown_suggestions.add(s["reference"])
                    logger.info(
                        "[enrichment:%s] Verse suggestions ts=%d: %s",
                        self._church_id, ts,
                        [(s["reference"], s["relevance_note"][:50]) for s in valid],
                    )
                    try:
                        await self._on_verse_suggestion(ts, valid)
                        await save_verse_suggestions(self._session_id, ts, valid)
                    except Exception as e:
                        logger.warning("[enrichment:%s] on_verse_suggestion failed: %s", self._church_id, e)
                else:
                    logger.info("[enrichment:%s] No verse suggestions ts=%d", self._church_id, ts)

    def _should_suggest(self) -> bool:
        """Return True when the current sermon mode warrants verse suggestions."""
        if not self._state_tracker:
            return True
        return self._state_tracker.settled_mode not in (
            "illustration", "exhortation", "procedural"
        )

    # --- Verse scratch pad ---

    async def _handle_verse_detection(
        self, verse: dict, audio_start: float, audio_end: float, ts: int
    ) -> None:
        """Add a detection to the scratch pad; flush if the passage has changed.

        Quoted detections are skipped during illustration mode — story vocabulary
        frequently produces false positives. Explicit chapter announcements are
        always accepted regardless of mode.
        """
        if (self._state_tracker
                and self._state_tracker.is_narrative()
                and verse.get("confidence") == "quoted"):
            logger.info(
                "[enrichment:%s] Verse detection skipped ts=%d — quoted during illustration",
                self._church_id, ts,
            )
            return

        entry = VerseScratchEntry(
            book=verse["book"],
            chapter=verse["chapter"],
            verse_start=verse["verse_start"],
            verse_end=verse.get("verse_end"),
            confidence=verse["confidence"],
            reference=verse["reference"],
            canonical_english=verse["canonical_english"],
            spanish_text=verse["spanish_text"],
            audio_start=audio_start,
            audio_end=audio_end,
            ts=ts,
        )

        if not self._verse_scratch:
            # First detection for this passage — emit immediately so the client
            # knows what's being read without waiting for the passage to change.
            logger.info(
                "[enrichment:%s] Scratch pad started: %s — emitting tentative verse_detected",
                self._church_id, entry.reference,
            )
            self._verse_scratch.append(entry)
            tentative = _consolidate_scratch(self._verse_scratch)
            try:
                await self._on_verse_detected(ts, tentative)
                await save_verse_detection(self._session_id, ts, tentative)
            except Exception as e:
                logger.warning("[enrichment:%s] on_verse_detected (tentative) failed: %s", self._church_id, e)
            return

        last = self._verse_scratch[-1]
        same_chapter = (last.book == entry.book and last.chapter == entry.chapter)
        # Use Deepgram audio timeline for gap — unaffected by server processing lag
        audio_gap = entry.audio_start - last.audio_end
        within_gap = audio_gap <= VERSE_GAP_THRESHOLD_S

        if same_chapter and within_gap:
            # Still in the same passage — extend the range and push an update to the client.
            self._verse_scratch.append(entry)
            running_range = _scratch_range_str(self._verse_scratch)
            logger.info(
                "[enrichment:%s] Scratch pad extended: %s → %s (gap=%.1fs, %d entry(s))",
                self._church_id, entry.reference, running_range,
                audio_gap, len(self._verse_scratch),
            )
            updated = _consolidate_scratch(self._verse_scratch)
            try:
                await self._on_verse_range_update(self._verse_scratch[0].ts, updated)
            except Exception as e:
                logger.warning("[enrichment:%s] on_verse_range_update failed: %s", self._church_id, e)
        else:
            # Passage changed or gap too large — emit the accumulated range and start fresh
            reason = (
                f"gap {audio_gap:.1f}s > {VERSE_GAP_THRESHOLD_S:.0f}s"
                if same_chapter
                else f"passage change ({last.book} {last.chapter} → {entry.book} {entry.chapter})"
            )
            logger.info(
                "[enrichment:%s] Scratch pad flushing (%s)",
                self._church_id, reason,
            )
            await self._flush_scratch("passage change" if not same_chapter else f"gap {audio_gap:.1f}s")
            self._verse_scratch = [entry]
            logger.debug(
                "[enrichment:%s] Scratch pad restarted: %s",
                self._church_id, entry.reference,
            )

    async def _flush_scratch(self, reason: str = "unknown") -> None:
        """Emit the final consolidated verse range when the passage ends.

        The initial verse_detected was already fired when the first entry arrived.
        This flush sends a verse_range_update with the fully consolidated range so
        the client can replace the tentative entry with the accurate span.
        """
        if not self._verse_scratch:
            return
        merged = _consolidate_scratch(self._verse_scratch)
        self._verse_scratch = []
        ts = merged["ts"]
        logger.info(
            "[enrichment:%s] Verse range finalised: %s | reason=%s | "
            "audio=%.1f–%.1fs | %d detection(s) | confidence=%s",
            self._church_id, merged["reference"], reason,
            merged["audio_start"], merged["audio_end"],
            merged["_count"], merged["confidence"],
        )
        try:
            await self._on_verse_range_update(ts, merged)
        except Exception as e:
            logger.warning("[enrichment:%s] on_verse_range_update (flush) failed: %s", self._church_id, e)

    async def close(self) -> None:
        # Flush any pending verse range before shutting down
        await self._flush_scratch("session close")
        for task in self._tasks:
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _consolidate_scratch(entries: list[VerseScratchEntry]) -> dict:
    """Merge same-chapter scratch entries into a single verse range dict."""
    starts = [e.verse_start for e in entries if isinstance(e.verse_start, int)]
    ends   = [e.verse_end or e.verse_start for e in entries if isinstance(e.verse_start, int)]
    v_start = min(starts) if starts else entries[0].verse_start
    v_end_raw = max(ends) if ends else None
    v_end = v_end_raw if (v_end_raw and v_end_raw != v_start) else None

    base = entries[0]
    ref = f"{base.book} {base.chapter}:{v_start}"
    if v_end:
        ref += f"\u2013{v_end}"  # en-dash range: "1 John 1:5–7"

    return {
        "book": base.book,
        "chapter": base.chapter,
        "verse_start": v_start,
        "verse_end": v_end,
        "reference": ref,
        "canonical_english": base.canonical_english,
        "spanish_text": entries[-1].spanish_text,  # most recently quoted text
        "confidence": "explicit" if any(e.confidence == "explicit" for e in entries) else "quoted",
        "ts": base.ts,
        "audio_start": base.audio_start,
        "audio_end": entries[-1].audio_end,
        "_count": len(entries),
    }


def _scratch_range_str(entries: list[VerseScratchEntry]) -> str:
    """Return a human-readable range for the current scratch pad, e.g. '1 John 1:1–5'."""
    if not entries:
        return "(empty)"
    starts = [e.verse_start for e in entries if isinstance(e.verse_start, int)]
    ends   = [e.verse_end or e.verse_start for e in entries if isinstance(e.verse_start, int)]
    base = entries[0]
    lo = min(starts) if starts else base.verse_start
    hi = max(ends) if ends else lo
    ref = f"{base.book} {base.chapter}:{lo}"
    if hi != lo:
        ref += f"\u2013{hi}"
    return ref


def _describe_invalid_verse(v: dict) -> str:
    """Return a short description of why a verse dict failed validation."""
    problems = []
    if not (isinstance(v.get("book"), str) and v.get("book")):
        problems.append(f"book={v.get('book')!r}")
    if not isinstance(v.get("chapter"), int):
        problems.append(f"chapter={v.get('chapter')!r}")
    if not isinstance(v.get("verse_start"), int):
        problems.append(f"verse_start={v.get('verse_start')!r}")
    if not (isinstance(v.get("reference"), str) and v.get("reference")):
        problems.append(f"reference={v.get('reference')!r}")
    if not (isinstance(v.get("canonical_english"), str) and v.get("canonical_english")):
        problems.append(f"canonical_english missing")
    if not isinstance(v.get("spanish_text"), str):
        problems.append(f"spanish_text missing")
    if v.get("confidence") not in ("explicit", "quoted"):
        problems.append(f"confidence={v.get('confidence')!r}")
    return "; ".join(problems) if problems else "unknown"


def _is_valid_verse(v: dict) -> bool:
    return (
        isinstance(v.get("book"), str) and v["book"]
        and isinstance(v.get("chapter"), int)
        and isinstance(v.get("verse_start"), int)
        and isinstance(v.get("reference"), str) and v["reference"]
        and isinstance(v.get("canonical_english"), str) and v["canonical_english"]
        and isinstance(v.get("spanish_text"), str)
        and v.get("confidence") in ("explicit", "quoted")
    )


def _is_valid_suggestion(s: dict) -> bool:
    return (
        isinstance(s.get("reference"), str) and s["reference"]
        and isinstance(s.get("canonical_english"), str) and s["canonical_english"]
        and isinstance(s.get("relevance_note"), str)
    )
