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
MAX_ENRICHMENT_TOKENS = 1100
DEFERRED_RELEASE_S = 6.0    # seconds to wait before releasing a suppressed translation if no merge
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

11. continuation_required: true if the speaker's thought clearly requires more text to complete —
    stronger and more forward-looking than thought_complete.
    true: sentence ends mid-argument, introduces a list without completing it, ends with a
    conjunction or subject pronoun that sets up a predicate not yet delivered
    (e.g. "Porque anoche se acuerdan que él", "Si nosotros decimos que", "Y la razón es").
    false: sentence is a complete unit, even if brief.

12. merge_with_previous: true if this sentence should be combined with the immediately
    preceding sentence into a single display caption. Any one criterion is sufficient:
    - discourse_tag is "answer_to_question" AND previous was "rhetorical_question"
    - discourse_tag is "scripture_quote" AND previous was "quote_introduction"
    - previous [PREVIOUS SENTENCE DISCOURSE] had continuation_required: true AND
      this sentence continues or resolves that incomplete thought
    - previous [PREVIOUS SENTENCE DISCOURSE] had source_quality: "fragmented" AND
      this sentence grammatically continues it (open clause, dangling copula, incomplete list)
    - this sentence's source_quality is "fragmented" AND it attaches to the previous
    - this sentence grammatically completes a dangling predicate from the previous sentence,
      regardless of whether the previous call set continuation_required: true —
      misclassification on the prior call is possible; judge structural continuation
      from the Spanish text independently.
      Examples: previous "Tiene peso, tiene significado, es" → current completes the predicate
                previous "¿Cuántos de ustedes, los hermanos que" → current closes the question
    - previous [PREVIOUS SENTENCE DISCOURSE] had display_ready: false AND this sentence
      clearly resolves or continues the prior thought
    All other cases → false.
    When true, you MUST write improved_translation as a fluent English rendering of the COMPLETE
    merged unit — the [PREVIOUS SENTENCE — PENDING MERGE] text PLUS the current sentence,
    treated as a single utterance. Do not translate only the current sentence.
    Example: if previous was "Tiene peso, tiene significado, es" and current is "el amor de Dios",
    improved_translation must render "It has weight, it has meaning — it is the love of God."
    The previous sentence's translation was suppressed awaiting this merge.

13. paragraph_break: true if this sentence opens a new major section, topic shift, or
    rhetorical phase. Triggers a visual separator on the display.
    true: shift from exposition to illustration, new scripture passage announced,
    transition from teaching to altar call, return from illustration to main point.
    false: continues the current rhetorical thread.

14. source_quality: assess the apparent quality of the input Spanish text:
    "clean":      normal sermon speech, no obvious STT artifacts
    "noisy":      contains apparent repetitions, garbled tokens, or incomplete phonemes
    "fragmented": clearly an incomplete utterance, a mid-word cut, or structurally broken

15. translation_register: the rendering register appropriate for this sentence:
    "scripture":   verbatim Bible text — formal, present tense, liturgical cadence
    "expository":  teaching or explanation — clear, accessible, present tense
    "narrative":   personal story or illustration — past tense, conversational
    "exhortation": direct appeal to the congregation — imperative, energetic

16. display_ready: the authoritative emission control signal for this sentence.
    Set to false when ANY of the following apply:
    - thought_complete is false
    - continuation_required is true
    - source_quality is "fragmented"
    - discourse_tag is "quote_introduction" (the quoted content has not arrived yet)
    Set to true only when ALL of the above are absent.
    When false, the translation is suppressed until a merge arrives or a fallback timeout fires.
    Be accurate — this drives whether the caption appears on screen.

JSON schema (return exactly this shape):
{
  "improved_translation": "string",
  "discourse_tag": "statement" | "rhetorical_question" | "answer_to_question" | "quote_introduction" | "scripture_quote" | "transition" | "exhortation_appeal",
  "introduces_quote": true | false,
  "thought_complete": true | false,
  "continuation_required": true | false,
  "merge_with_previous": true | false,
  "paragraph_break": true | false,
  "source_quality": "clean" | "noisy" | "fragmented",
  "translation_register": "scripture" | "expository" | "narrative" | "exhortation",
  "sermon_mode": "scripture" | "exposition" | "illustration" | "application" | "exhortation" | "procedural",
  "display_ready": true | false,
  "verse_detected": {
    "book": "string",
    "chapter": integer,
    "verse_start": integer,
    "verse_end": integer | null,
    "spanish_text": "string",
    "canonical_english": "string",
    "reference": "string",
    "confidence": "explicit" | "quoted"
  } | null
}\
"""


_VERSE_SUGGESTIONS_SYSTEM = """\
You are a biblical reference assistant for a live Spanish sermon translation system.
Given a sentence from a sermon, suggest 1-3 specifically relevant Bible verses for the congregation.

STRICT RULES:
- Return ONLY valid JSON: {"suggestions": [...]}. No prose, no markdown fences, no code blocks.
- Return {"suggestions": []} for: procedural, transitional, logistical, or rhetorical filler sentences.
- Return {"suggestions": []} for sentences with source_quality "fragmented" or "noisy".
- Return {"suggestions": []} when the theological content is generic or no cross-reference adds real value.
- NEVER suggest a verse in [ALREADY SUGGESTED] or matching [ACTIVE PASSAGE].
- Prefer thematic cross-references over repeating the same book being expounded.
- Each suggestion must be a SPECIFIC verse or short range, not a chapter or book.
- Only suggest when you are confident the verse meaningfully illuminates THIS sentence.
- Use NIV text for canonical_english.

JSON schema: {"suggestions": [{"reference": "string", "canonical_english": "string", "relevance_note": "string"}]}
"""


# ---------------------------------------------------------------------------
# Translation normalization — applied post-LLM to improve natural English phrasing.
# These are domain-specific substitutions that the LLM frequently gets wrong in a
# live sermon context (e.g. "transmit" is technically correct for "transmitir" but
# sounds robotic; "share" is the natural preaching-register equivalent).
# ---------------------------------------------------------------------------
_TRANSLATION_NORMALIZATION: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\btransmit\b', re.IGNORECASE), 'share'),
    (re.compile(r'\btransmits\b', re.IGNORECASE), 'shares'),
    (re.compile(r'\btransmitting\b', re.IGNORECASE), 'sharing'),
    (re.compile(r'\btransmitted\b', re.IGNORECASE), 'shared'),
    (re.compile(r'\btransmission\b', re.IGNORECASE), 'sharing'),
]


def _normalize_translation(text: str) -> str:
    """Apply domain normalization to an English translation for natural sermon register."""
    for pattern, replacement in _TRANSLATION_NORMALIZATION:
        # Preserve original case for sentence-initial words
        def _replace(m: re.Match, repl: str = replacement) -> str:
            orig = m.group(0)
            if orig[0].isupper():
                return repl[0].upper() + repl[1:]
            return repl
        text = pattern.sub(_replace, text)
    return text


def _translation_deviation_score(google: str, improved: str) -> float:
    """Word-level Jaccard similarity between two translations.

    Returns 0.0 (completely different) to 1.0 (identical).
    Used to detect when LLM reconstruction diverges too far from the Google
    baseline for noisy source text, flagging a reconstruction risk.
    """
    g_words = set(google.lower().split())
    i_words = set(improved.lower().split())
    if not g_words and not i_words:
        return 1.0
    union = g_words | i_words
    intersection = g_words & i_words
    return len(intersection) / len(union)


# Threshold below which an improved translation is considered to diverge
# significantly from the Google baseline when source_quality is "noisy".
_RECONSTRUCTION_RISK_THRESHOLD = 0.35


def _build_system_prompt(church_terms: dict[str, str]) -> str:
    if church_terms:
        lines = "\n".join(f"  {es} → {en}" for es, en in church_terms.items())
        glossary_block = (
            "THEOLOGICAL GLOSSARY — prefer these translations, adapting grammatical form "
            "and number to match the context (e.g. singular/plural, noun/adjective):\n" + lines
        )
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
        continuation = prev_discourse.get("continuation_required", False)
        quality = prev_discourse.get("source_quality", "clean")
        ready = prev_discourse.get("display_ready", True)
        parts.append(
            f"[PREVIOUS SENTENCE DISCOURSE]\n"
            f"discourse_tag: {tag}\n"
            f"introduces_quote: {str(introduces).lower()}\n"
            f"thought_complete: {str(complete).lower()}\n"
            f"continuation_required: {str(continuation).lower()}\n"
            f"source_quality: {quality}\n"
            f"display_ready: {str(ready).lower()}"
        )
        # When the previous sentence was held (not display_ready), inject its text
        # prominently so the model can write a correct merged improved_translation.
        if not ready and sentence_history:
            prev_sp, prev_en = sentence_history[-1]
            parts.append(
                f"[PREVIOUS SENTENCE — PENDING MERGE]\n"
                f"  ES: {prev_sp}\n"
                f"  EN: {prev_en}\n"
                f"If merge_with_previous is true, improved_translation MUST cover both "
                f"this previous sentence AND the current sentence as one complete unit."
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
        on_buffer_hold: Callable[[str, float], Awaitable[None]] | None = None,
        on_caption_merge: Callable[[int, int, str, str], Awaitable[None]] | None = None,
        on_segment_metadata: Callable[[int, dict], Awaitable[None]] | None = None,
        session_id: int = 0,
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
        self._on_buffer_hold = on_buffer_hold
        self._on_caption_merge = on_caption_merge
        self._on_segment_metadata = on_segment_metadata
        self._session_id = session_id
        self._system_prompt = _build_system_prompt(church_terms)
        self._client = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._tasks: list[asyncio.Task] = []
        # Serializes post-completion state mutations so concurrent tasks that finish
        # out of order (due to variable API latency) don't corrupt sentence history,
        # active passage, shown suggestions, or sermon mode signals.
        self._mutation_lock = asyncio.Lock()

        # Rolling sentence context (last 5 sentences, Spanish + best English)
        self._sentence_history: deque[tuple[str, str]] = deque(maxlen=5)
        # Most recent explicit verse citation — injected into every subsequent prompt
        self._active_passage: dict | None = None
        # All references suggested this session — prevents repetition
        self._shown_suggestions: set[str] = set()
        # Discourse output of the previous enriched sentence — injected as forward context
        self._prev_discourse: dict | None = None
        # Timestamp of the previously enriched sentence — used for caption_merge targeting
        self._prev_sentence_ts: int | None = None
        # Deferred translation updates: ts → (english, asyncio.Task)
        # When display_ready is false, translation_update is held pending merge or timeout.
        self._deferred_updates: dict[int, tuple[str, asyncio.Task]] = {}
        # Chain-aware merge. Anchored to the EARLIEST visible segment so the caption
        # stays at a stable screen position as fragments accumulate.
        # {
        #   "head_ts": int,   # oldest visible segment (ts_keep in every merge)
        #   "tail_ts": int,   # most recently absorbed fragment (used to detect chain extension)
        #   "spanish": str,   # full accumulated chain Spanish
        #   "length": int,
        # }
        self._merge_chain_head: dict | None = None

        # Verse scratch pad — accumulates detections for temporal range consolidation
        self._verse_scratch: list[VerseScratchEntry] = []

        # Metrics counters — accessible for logging/monitoring
        self.metrics: dict[str, int] = {
            "noisy_input_detected": 0,
            "reconstruction_risk": 0,
            "structural_flush_block": 0,
            "forced_release": 0,
            "verse_suggestion_triggered": 0,
            "verse_suggestion_gated": 0,
            "parse_retry_success": 0,
            "parse_failed": 0,
            "merge_chain_max_length": 0,
        }

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

        result = None
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Retry: extract first {...} JSON block from the response
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    result = json.loads(m.group(0))
                    async with self._mutation_lock:
                        self.metrics["parse_retry_success"] += 1
                    logger.info(
                        "[enrichment:%s] JSON extracted via retry for ts=%d", self._church_id, ts
                    )
                except json.JSONDecodeError:
                    pass
            if result is None:
                async with self._mutation_lock:
                    self.metrics["parse_failed"] += 1
                logger.warning(
                    "[enrichment:%s] Could not parse JSON for ts=%d: %.120s",
                    self._church_id, ts, raw,
                )
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
            continuation_required = bool(result.get("continuation_required", False))
            merge_with_previous = bool(result.get("merge_with_previous", False))
            paragraph_break = bool(result.get("paragraph_break", False))
            source_quality = result.get("source_quality", "clean")
            if source_quality not in ("clean", "noisy", "fragmented"):
                source_quality = "clean"
            translation_register = result.get("translation_register", "expository")
            if translation_register not in ("scripture", "expository", "narrative", "exhortation"):
                translation_register = "expository"

            # display_ready: always computed deterministically from the structural signals.
            # The LLM's field is read but can only make it MORE restrictive (false), never relax it.
            # This prevents cases where the LLM returns display_ready=true despite
            # continuation_required=true or source_quality="fragmented".
            display_ready = (
                thought_complete
                and not continuation_required
                and source_quality != "fragmented"
                and discourse_tag != "quote_introduction"
            )
            display_ready_from_llm = result.get("display_ready")
            if isinstance(display_ready_from_llm, bool) and not display_ready_from_llm:
                # LLM says not ready even though heuristic says ready — trust the LLM restriction
                display_ready = False

            logger.info(
                "[enrichment:%s] Discourse ts=%d: tag=%s introduces_quote=%s "
                "complete=%s continuation=%s merge=%s break=%s quality=%s "
                "register=%s display_ready=%s",
                self._church_id, ts, discourse_tag, introduces_quote,
                thought_complete, continuation_required, merge_with_previous,
                paragraph_break, source_quality, translation_register, display_ready,
            )
            # Store for injection into the next sentence's prompt
            self._prev_discourse = {
                "discourse_tag": discourse_tag,
                "introduces_quote": introduces_quote,
                "thought_complete": thought_complete,
                "continuation_required": continuation_required,
                "source_quality": source_quality,
                "display_ready": display_ready,
            }

            # Feedback hold: if this sentence was incomplete, ask the buffer to
            # hold the next sentence longer so its continuation can accumulate.
            # continuation_required takes precedence (stronger signal); fall back
            # to thought_complete. This is a forward correction — it can't un-flush
            # the current sentence but prevents the same pattern on the next boundary.
            if (continuation_required or not thought_complete) and self._on_buffer_hold:
                hold_secs = 4.5 if continuation_required else 3.5
                reason = "continuation_required" if continuation_required else "incomplete_thought"
                try:
                    await self._on_buffer_hold(reason, hold_secs)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_buffer_hold failed: %s", self._church_id, e)

            # Hold longer when audio is fragmented — the speaker may have been cut mid-word.
            if source_quality == "fragmented" and self._on_buffer_hold:
                try:
                    await self._on_buffer_hold("fragmented_audio", 3.0)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_buffer_hold (fragmented) failed: %s", self._church_id, e)

            # Also hold for quote introductions detected by the LLM (catches cases
            # the synchronous regex in _on_sentence may have missed).
            if introduces_quote and self._on_buffer_hold:
                try:
                    await self._on_buffer_hold("quote_introduction_llm", 4.0)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_buffer_hold (quote) failed: %s", self._church_id, e)

            # --- Sermon mode ---
            sermon_mode = result.get("sermon_mode", "exposition")
            if sermon_mode not in _VALID_SERMON_MODES:
                sermon_mode = "exposition"
            logger.info("[enrichment:%s] Sermon mode ts=%d: %s", self._church_id, ts, sermon_mode)
            if self._state_tracker:
                await self._state_tracker.add_signal(sermon_mode, ts)

            # Track noisy input
            if source_quality == "noisy":
                self.metrics["noisy_input_detected"] += 1

            # --- Translation improvement ---
            improved = result.get("improved_translation", "").strip()
            # Apply domain normalization for natural sermon English ("transmit" → "share", etc.)
            if improved:
                improved = _normalize_translation(improved)

            # Reconstruction risk guard: if source is noisy and the LLM's translation
            # diverges significantly from the Google baseline, the LLM may have hallucinated
            # meaning. In that case, fall back to the Google translation to be conservative.
            reconstruction_risk = False
            if source_quality == "noisy" and improved and improved != google_english:
                deviation = _translation_deviation_score(google_english, improved)
                if deviation < _RECONSTRUCTION_RISK_THRESHOLD:
                    reconstruction_risk = True
                    self.metrics["reconstruction_risk"] += 1
                    logger.warning(
                        "[enrichment:%s] reconstruction_risk=true ts=%d "
                        "deviation_score=%.2f\n  google: %s\n     llm: %s",
                        self._church_id, ts, deviation,
                        google_english[:80], improved[:80],
                    )
                    # Fall back to Google to avoid emitting hallucinated content.
                    # The noisy flag will also suppress this sentence until a merge.
                    improved = google_english

            best_english = improved if (improved and improved != google_english) else google_english
            # Apply normalization to the final best_english so the domain map is
            # always applied regardless of whether the LLM improved the translation.
            best_english = _normalize_translation(best_english)

            if display_ready:
                # Sentence is finalised — emit translation update immediately.
                if improved and improved != google_english:
                    logger.info(
                        "[enrichment:%s] decision=immediate_translation_update ts=%d:\n"
                        "  google: %s\n     llm: %s",
                        self._church_id, ts, google_english[:80], improved[:80],
                    )
                    try:
                        await self._on_translation_update(ts, improved)
                    except Exception as e:
                        logger.warning("[enrichment:%s] on_translation_update failed: %s", self._church_id, e)
                else:
                    logger.info(
                        "[enrichment:%s] decision=immediate_translation_update ts=%d — no change",
                        self._church_id, ts,
                    )
            else:
                # Sentence is not display_ready — suppress translation update and defer.
                # The deferred release fires after DEFERRED_RELEASE_S if no merge arrives.
                defer_task = asyncio.create_task(
                    self._deferred_translation_release(ts, best_english, google_english)
                )
                self._deferred_updates[ts] = (best_english, defer_task)
                logger.info(
                    "[enrichment:%s] decision=suppressed_translation_update ts=%d "
                    "(display_ready=false continuation=%s quality=%s)",
                    self._church_id, ts, continuation_required, source_quality,
                )

            # Signal enrichment settled immediately in all cases so Google correction guard fires.
            try:
                await self._on_enrichment_settled(ts)
            except Exception as e:
                logger.warning("[enrichment:%s] on_enrichment_settled failed: %s", self._church_id, e)

            # Append to sentence history using the best available translation
            self._sentence_history.append((spanish, best_english))

            # --- Caption merge (head-anchored chain) ---
            # The chain is always anchored to the EARLIEST visible segment (head_ts = ts_keep).
            # Every subsequent fragment is absorbed INTO the head so the caption stays at a
            # stable screen position as the chain grows.
            #
            # on_caption_merge(absorb_ts, keep_ts, ...) → ts_absorb=absorb_ts, ts_keep=keep_ts
            prev_ts = self._prev_sentence_ts
            if merge_with_previous and prev_ts is not None and self._on_caption_merge:
                hist = list(self._sentence_history)

                chain = self._merge_chain_head
                if chain is not None and prev_ts == chain["tail_ts"]:
                    # Extending an active chain — absorb current ts into the head anchor.
                    chain_spanish = chain["spanish"] + " " + spanish
                    chain_len = chain["length"] + 1
                    head_ts = chain["head_ts"]

                    # Cancel deferred update for the fragment being absorbed (current ts).
                    if ts in self._deferred_updates:
                        _, dt = self._deferred_updates.pop(ts)
                        dt.cancel()
                        logger.info(
                            "[enrichment:%s] decision=merge_cancelled_deferred_update absorbed=%d",
                            self._church_id, ts,
                        )

                    self._merge_chain_head = {
                        "head_ts": head_ts,
                        "tail_ts": ts,
                        "spanish": chain_spanish,
                        "length": chain_len,
                    }
                    if chain_len > self.metrics["merge_chain_max_length"]:
                        self.metrics["merge_chain_max_length"] = chain_len
                    logger.info(
                        "[enrichment:%s] decision=merge_applied ts=%d absorbed_by_head=%d "
                        "chain_len=%d",
                        self._church_id, ts, head_ts, chain_len,
                    )
                    try:
                        await self._on_caption_merge(ts, head_ts, chain_spanish, best_english)
                    except Exception as e:
                        logger.warning("[enrichment:%s] on_caption_merge failed: %s", self._church_id, e)

                else:
                    # Starting a new chain — prev_ts becomes the head anchor; current ts absorbed.
                    prev_spanish = hist[-2][0] if len(hist) >= 2 else spanish
                    chain_spanish = prev_spanish + " " + spanish

                    # Cancel deferreds for both the head (prev_ts) and the absorbed sentence (ts).
                    for cancel_ts in (prev_ts, ts):
                        if cancel_ts in self._deferred_updates:
                            _, dt = self._deferred_updates.pop(cancel_ts)
                            dt.cancel()
                            logger.info(
                                "[enrichment:%s] decision=merge_cancelled_deferred_update "
                                "cancelled=%d",
                                self._church_id, cancel_ts,
                            )

                    self._merge_chain_head = {
                        "head_ts": prev_ts,
                        "tail_ts": ts,
                        "spanish": chain_spanish,
                        "length": 2,
                    }
                    logger.info(
                        "[enrichment:%s] decision=merge_applied ts=%d absorbed_by_head=%d "
                        "chain_len=2",
                        self._church_id, ts, prev_ts,
                    )
                    try:
                        await self._on_caption_merge(ts, prev_ts, chain_spanish, best_english)
                    except Exception as e:
                        logger.warning("[enrichment:%s] on_caption_merge failed: %s", self._church_id, e)

            else:
                # No merge — reset chain.
                if self._merge_chain_head is not None:
                    logger.debug(
                        "[enrichment:%s] Chain closed at ts=%d (display_ready=%s, merge=%s)",
                        self._church_id, ts, display_ready, merge_with_previous,
                    )
                self._merge_chain_head = None

            # --- Segment metadata ---
            # pending_completion signals that this segment may be updated or merged.
            # The client can dim/italicise it until a translation_update or caption_merge arrives.
            pending_completion = not display_ready
            if self._on_segment_metadata:
                metadata = {
                    "translation_register": translation_register,
                    "paragraph_break": paragraph_break,
                    "source_quality": source_quality,
                    "pending_completion": pending_completion,
                }
                try:
                    await self._on_segment_metadata(ts, metadata)
                except Exception as e:
                    logger.warning("[enrichment:%s] on_segment_metadata failed: %s", self._church_id, e)

            # Advance prev_sentence_ts for the next enrichment's merge targeting
            self._prev_sentence_ts = ts

            # --- Verse suggestions (scheduled outside mutation lock) ---
            # Capture context snapshots now while under the lock.
            _vs_active = self._active_passage
            _vs_shown = set(self._shown_suggestions)

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

        # --- Verse suggestions (separate async call, outside mutation lock) ---
        # Runs as a fire-and-forget task so it cannot delay structural decisions.
        # Uses only the context snapshots captured inside the lock above.
        _suggest_eligible = (
            self._should_suggest()
            and display_ready
            and self._should_generate_verse_suggestions(
                spanish, source_quality, discourse_tag, _vs_active
            )
        )
        async with self._mutation_lock:
            if _suggest_eligible:
                self.metrics["verse_suggestion_triggered"] += 1
            else:
                self.metrics["verse_suggestion_gated"] += 1
        if _suggest_eligible:
            suggest_task = asyncio.create_task(
                self._run_verse_suggestions(
                    spanish, google_english, ts,
                    topic_context, _vs_active, _vs_shown,
                )
            )
            self._tasks = [t for t in self._tasks if not t.done()]
            self._tasks.append(suggest_task)

    def _should_suggest(self) -> bool:
        """Return True when the current sermon mode warrants verse suggestions."""
        if not self._state_tracker:
            return True
        return self._state_tracker.settled_mode not in (
            "illustration", "exhortation", "procedural"
        )

    def _should_generate_verse_suggestions(
        self,
        spanish: str,
        source_quality: str,
        discourse_tag: str,
        active_passage: dict | None,
    ) -> bool:
        """Additional content-level gate for verse suggestions.

        Only generates suggestions when there is meaningful, clean theological
        content to cross-reference. Prevents over-triggering on noise, short
        fragments, and casual filler sentences.

        Gates:
        - Noisy or fragmented source: skip (system prompt also says this, but
          avoid the API call entirely).
        - Short fragments (< 7 words): skip unless an active passage is in progress.
        - Procedural or transitional discourse: skip.
        - Casual exhortation filler without a supporting active passage: skip.
        """
        if source_quality in ("noisy", "fragmented"):
            return False
        word_count = len(spanish.split())
        if word_count < 7 and active_passage is None:
            return False
        if discourse_tag in ("transition", "quote_introduction"):
            return False
        # Exhortation-only filler without an active passage context is too generic
        if discourse_tag == "exhortation_appeal" and active_passage is None and word_count < 12:
            return False
        return True

    async def _deferred_translation_release(
        self, ts: int, english: str, google_english: str
    ) -> None:
        """Fallback: release a suppressed translation after DEFERRED_RELEASE_S if no merge arrived.

        Called when display_ready was false. If caption_merge fires first, this task
        is cancelled. If no merge arrives within the timeout, the best available
        translation is emitted so the caption is not permanently blank.
        """
        try:
            await asyncio.sleep(DEFERRED_RELEASE_S)
            if ts in self._deferred_updates:
                del self._deferred_updates[ts]
                logger.info(
                    "[enrichment:%s] decision=deferred_translation_released ts=%d "
                    "(no merge in %.1fs)",
                    self._church_id, ts, DEFERRED_RELEASE_S,
                )
                if english and english != google_english:
                    try:
                        await self._on_translation_update(ts, english)
                    except Exception as e:
                        logger.warning(
                            "[enrichment:%s] deferred translation_update failed ts=%d: %s",
                            self._church_id, ts, e,
                        )
        except asyncio.CancelledError:
            pass

    async def _run_verse_suggestions(
        self,
        spanish: str,
        google_english: str,
        ts: int,
        topic_context: str,
        active_passage: dict | None,
        shown: set[str],
    ) -> None:
        """Separate lightweight async call for verse suggestions.

        Runs after (and independent of) the main enrichment call so it cannot
        compete with structural decisions for prompt attention or cause delays.
        """
        parts: list[str] = []
        if topic_context:
            parts.append(f"[SERMON CONTEXT]\n{topic_context}")
        if active_passage:
            parts.append(
                f"[ACTIVE PASSAGE]\n"
                f"{active_passage['reference']} — {active_passage['canonical_english']}"
            )
        if shown:
            parts.append(f"[ALREADY SUGGESTED]\n{', '.join(sorted(shown))}")
        parts.append(f"[SOURCE]\n{spanish}")
        parts.append(f"[TRANSLATION]\n{google_english}")
        user_msg = "\n\n".join(parts)

        try:
            response = await self._client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=400,
                temperature=0,
                system=_VERSE_SUGGESTIONS_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = response.content[0].text.strip()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "[enrichment:%s] Verse suggestions call failed ts=%d: %s",
                self._church_id, ts, e,
            )
            return

        suggestions = _extract_suggestions(raw, self._church_id, ts)
        if suggestions is None:
            return

        async with self._mutation_lock:
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
                    logger.warning(
                        "[enrichment:%s] on_verse_suggestion failed ts=%d: %s",
                        self._church_id, ts, e,
                    )
            else:
                logger.info("[enrichment:%s] No verse suggestions ts=%d", self._church_id, ts)

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
        # Cancel deferred translation updates
        for _, (_, defer_task) in list(self._deferred_updates.items()):
            defer_task.cancel()
        self._deferred_updates.clear()
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


def _extract_suggestions(raw: str, church_id: str, ts: int) -> list[dict] | None:
    """Parse the verse suggestions response.

    Expects {"suggestions": [...]} but falls back to a bare array for robustness.
    Strips markdown fences, then attempts JSON extraction via regex if direct parse fails.
    Returns None on unrecoverable parse failure (caller should skip the call).
    Returns an empty list if the model returned a valid empty response.
    """
    # Strip markdown fences
    text = raw
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

    # Attempt 1: direct parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "suggestions" in parsed:
            result = parsed["suggestions"]
            if isinstance(result, list):
                return result
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Attempt 2: extract first JSON object containing "suggestions"
    m = re.search(r'\{[^{}]*"suggestions"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(0))
            result = parsed.get("suggestions", [])
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # Attempt 3: extract bare JSON array
    m = re.search(r'\[.*?\]', text, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    logger.warning(
        "[enrichment:%s] Verse suggestions parse failed ts=%d: %.80s",
        church_id, ts, raw,
    )
    return None
