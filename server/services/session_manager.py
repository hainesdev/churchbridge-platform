import asyncio
import logging
import os
import re
import time
from fastapi import WebSocket

from server.db.bible_text import get_passage, get_passage_by_reference
from server.db.glossary import get_glossary
from server.db.church_terms import load_church_terms
from server.db.modes import save_mode_transition
from server.db.sessions import (
    create_service_session,
    close_service_session,
    append_segment,
)
from server.services.audio_utils import resample_float32_to_pcm16, base64_to_float32_bytes
from server.services.google_speech_session import GoogleSpeechSession
from server.services.google_translate_service import GoogleTranslateService
from server.services.llm_enrichment_service import LLMEnrichmentService, _format_deferred_release_text
from server.services.sentence_buffer import SentenceBuffer, _is_incomplete
from server.services.stt import STTConfig
from server.services.sermon_state_tracker import SermonStateTracker
from server.services.topic_tracker import TopicTracker
from server.services.broadcaster import Broadcaster
from server.services.session_recorder import SessionRecorder, CaptureResult

logger = logging.getLogger(__name__)

# Hold the dock-to-feed handoff briefly so the interpreted area can prefer
# enriched English when it lands soon after the Google sentence.
PREFERRED_COMMIT_DELAY_S = 0.85
SHORT_FRAGMENT_COMMIT_DELAY_S = 1.5
TERMINAL_INCOMPLETE_COMMIT_DELAY_S = 0.35

# Splits an STT final at internal sentence boundaries — e.g.
# "yo soy un cristiano. Pentecostés viene Juan y dice," becomes two parts.
# Lookbehind: must follow [.!?]
# Lookahead: must precede an uppercase letter or opening punctuation (¿ ¡ ")
# This avoids splitting on verse numbers ("Juan 3:16") and abbreviations ("cap.").
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÜÑ¿¡"])')

# Minimum word count for a split fragment to stand alone. Parts shorter than
# this are merged back into the preceding fragment — keeps Q&A pairs together:
# "¿Quién es él? Jesucristo." stays as one entry rather than splitting off the answer.
_MIN_SPLIT_WORDS = 5

# --- STT noise cleanup (applied before segmentation; raw text is still broadcast) ---

# Multi-character filler sounds: "AAA", "Mmm", "Uh", "Um", "Eh", "Este", "Eeh"
_STT_FILLER = re.compile(r'\b(?:A{2,}|M{2,}|Uh+|Um+|Eh+|Eeh+|Mmm+|Este+)\b', re.IGNORECASE)

# Same single character repeated (stutter): "A a Cristo" → "a Cristo"
# Matches when the SAME character appears 2+ times (case-insensitive).
# Replacement keeps the LAST instance so "A a" → "a" (the article/preposition,
# not the filler uppercase "A"). Avoids removing valid Spanish sequences like
# "y a la fe" (different single-char words in sequence).
_STT_SINGLE_REPEAT = re.compile(r'\b(\w)(?:\s+\1)+\b', re.IGNORECASE)

# Repeated function word (stutter): "que que" → "que", "el el" → "el"
# Uses an explicit allowlist of Spanish function words — prepositions, articles,
# conjunctions, and relative pronouns — rather than a character-count heuristic.
# This protects deliberate emphasis repetition of lexical words ("muy muy",
# "bien bien", "más más") which are common in Pentecostal preaching register.
_STT_WORD_REPEAT = re.compile(
    r'\b(que|el|la|los|las|un|una|de|en|con|por|para|a|al|del|'
    r'y|o|ni|si|pero|como|cuando|donde|quien|cual|ya)\s+\1\b',
    re.IGNORECASE,
)

# "Santo" as STT noise: preaching-register exclamation misclassified as sentence start.
# Patterns: "Santo tú...", "Santo él...", "Santo ella..." etc.
# PRESERVE: "Espíritu Santo", "Padre Santo", "Dios Santo" — noun-adjective order is fine
# because we only strip when "Santo" PRECEDES a personal pronoun.
_STT_SANTO_PRONOUN = re.compile(
    r'\bSanto\b\s+(?=(?:tú|él|ella|yo|usted|nosotros|nosotras|ellos|ellas|'
    r'me|te|se|lo|la|le|les|nos)\b)',
    re.IGNORECASE,
)
# "Santo" at the very start of a fragment when followed by content that is
# clearly not a predicate of "Santo" (i.e. it's a noise prefix).
# NOT stripped when followed by theological noun phrases like "Espíritu", "Padre", etc.
_STT_SANTO_INITIAL = re.compile(
    r'^Santo\s+(?!(?:Espíritu|Padre|Hijo|Tomás|Domingo|de\s+los|de\s+las|es\b|'
    r'y\s+justo|y\s+poderoso|señor|dios))',
    re.IGNORECASE,
)

# Pentecostés context normalization.
# "Pentecostés" (the biblical feast) vs "Pentecostales" (the people/movement).
# Two detection strategies — either is sufficient to trigger the rewrite:

# Strategy 1: direct possessive/copula prefix ("los Pentecostés", "somos Pentecostés")
_PENTECOSTES_PEOPLE = re.compile(
    r'\b(los|las|somos|éramos|eran|son|como|otros|iglesia|pueblo|movimiento|'
    r'hablan|hablar|decimos|dicen)\s+(Pentecostés)\b',
    re.IGNORECASE,
)

# Strategy 2: discourse context — if the sentence contains narrative/conversational
# markers alongside "Pentecostés", the preacher is almost certainly referring to
# Pentecostal people or culture, not the feast day.
# Matches first-person speech, reported speech, present-day location markers, etc.
_PENTECOSTES_RE = re.compile(r'\bPentecostés\b', re.IGNORECASE)
_PENTECOSTES_DISCOURSE = re.compile(
    r'\b(?:dice|digo|decimos|dicen|dijo|dijeron|'
    r'viene|vengo|venimos|vienen|'
    r'anoche|hoy|aquí|ahora|nosotros|'
    r'somos|éramos|eran|son|'
    r'hablan|hablar|hablamos|llamamos|llaman|se\s+llaman|'
    r'yo\s+soy|como\s+nosotros|entre\s+nosotros)\b',
    re.IGNORECASE,
)


def _normalize_pentecostes(text: str) -> str:
    """Rewrite or remove 'Pentecostés' based on structural context.

    Three strategies applied in order:
    1. Direct possessive/copula prefix → rewrite to 'Pentecostales'
    2. Discourse context markers → rewrite to 'Pentecostales'
    3. Remaining isolated 'Pentecostés' with no structural anchor → remove as STT noise
       (e.g. "Pentecostés comunión unos con otros" → "comunión unos con otros")

    Whitelist (never removed):
    - Preceded by a preposition/article: "de Pentecostés", "en Pentecostés", "el día de Pentecostés"
    - Followed by a copula/verb making it the grammatical subject: "Pentecostés fue cuando..."
    """
    # Strategy 1: direct prefix match
    text = _PENTECOSTES_PEOPLE.sub(lambda m: m.group(1) + ' Pentecostales', text)
    # Strategy 2: discourse context — if ANY discourse marker co-occurs with Pentecostés
    if _PENTECOSTES_RE.search(text) and _PENTECOSTES_DISCOURSE.search(text):
        text = _PENTECOSTES_RE.sub('Pentecostales', text)
    # Strategy 3: remove remaining isolated noise instances
    if _PENTECOSTES_RE.search(text):
        def _remove_noise(m: re.Match) -> str:
            start = m.start()
            before = text[max(0, start - 10):start]
            after = text[m.end():].lstrip()
            # Protected: preceded by preposition/article
            if re.search(r'\b(?:de|en|el|la|los|las|del|durante|desde)\s*$', before, re.IGNORECASE):
                return m.group(0)
            # Protected: followed by copula or verb making Pentecostés the subject
            if re.match(r'\b(?:es\b|era\b|fue\b|son\b|eran\b|fueron\b|será\b|ha\b|han\b|'
                        r'significa|representa|se\b|celebra|ocurrió)', after, re.IGNORECASE):
                return m.group(0)
            # Noise — remove
            return ''
        text = _PENTECOSTES_RE.sub(_remove_noise, text)
    return text


def _clean_stt(text: str) -> str:
    """Normalize STT output before segmentation, translation, and buffering.

    Applied in order, each pass targeted:
    1. Remove multi-char filler sounds (AAA, Uh, Mmm, Este...).
    2. Collapse repeated short function words ("que que" → "que").
    3. Collapse same-character stutters ("a a Cristo" → "a Cristo").
    4. Remove "Santo" when used as sentence-initial noise before a pronoun.
    5. Context-aware Pentecostés normalization/removal.
    6. Normalize internal whitespace.

    The original raw text is still broadcast as stt_final so the operator stream
    is unmodified; only the pipeline-facing text is cleaned.
    """
    text = _STT_FILLER.sub('', text)
    text = _STT_WORD_REPEAT.sub(r'\1', text)
    # Keep the LAST instance of a stuttered single character so "A a Cristo" → "a Cristo"
    # (the article/preposition "a", not the filler "A").
    text = _STT_SINGLE_REPEAT.sub(lambda m: m.group(0).split()[-1], text)
    # Strip "Santo" when it appears as STT noise before a personal pronoun
    # ("Santo tú transmites" → "tú transmites") or as a bare sentence-initial exclamation.
    # Must come before Pentecostés normalization so whitespace is clean.
    text = _STT_SANTO_PRONOUN.sub('', text)
    text = _STT_SANTO_INITIAL.sub('', text)
    text = _normalize_pentecostes(text)
    return ' '.join(text.split())


def _language_family(code: str) -> str:
    normalized = str(code or "").strip().lower()
    if normalized.startswith("es"):
        return "es"
    if normalized.startswith("en"):
        return "en"
    return ""


def _translation_mode(stt_context: dict | None) -> str:
    stt_context = dict(stt_context or {})
    primary_family = _language_family(
        stt_context.get("stt_primary_language", "") or stt_context.get("detected_language", "")
    )
    if primary_family == "en":
        return "english"
    if primary_family == "es":
        return "spanish"

    detected = stt_context.get("stt_detected_languages") or stt_context.get("detected_languages") or []
    for code in detected:
        family = _language_family(code)
        if family == "en":
            return "english"
        if family == "es":
            return "spanish"
    return "unknown"


# --- Discourse-based buffer hold detection ---
# Applied synchronously in _on_sentence (after flush, before the next fragment
# arrives) so there is no race with LLM enrichment timing.

# Quote introductions: the next sentence is almost certainly scripture text.
_QUOTE_INTRO = re.compile(
    r'\b(?:'
    r'(?:Juan|Pedro|Pablo|Jesús|Dios|David|Moisés|el\s+Señor|la\s+Biblia|'
    r'la\s+Palabra|el\s+versículo|el\s+apóstol)\s+dic[ei]'
    r'|dice\s+(?:aquí|ahí|la\s+Biblia|la\s+Palabra)'
    r'|como\s+dice\s+en'
    r'|leemos\s+que'
    r'|está\s+escrito'
    r'|escrito\s+está'
    r'|la\s+Biblia\s+dice'
    r'|la\s+Palabra\s+dice'
    r')',
    re.IGNORECASE,
)


def _split_segments(text: str) -> list[str]:
    """Split an STT final at internal sentence boundaries, then merge back
    any trailing fragment that is too short to stand alone.

    This keeps rhetorical Q&A pairs together — "¿Quién es él? Jesucristo." is
    one sentence for LLM and buffer purposes, while a longer follow-on sentence
    like "Y no hay tinieblas en él." correctly splits off as its own entry.
    """
    parts = _SENTENCE_SPLIT.split(text)
    if len(parts) == 1:
        return parts
    merged: list[str] = [parts[0]]
    for part in parts[1:]:
        # A part that opens its own question (¿) is a distinct interrogative — never
        # merge it back even if it is short, to avoid nonsensical question chains.
        if len(part.split()) < _MIN_SPLIT_WORDS and not part.lstrip().startswith('¿'):
            # Short answer or fragment — attach to the preceding sentence.
            merged[-1] = merged[-1] + ' ' + part
        else:
            merged.append(part)
    return merged


def _preferred_commit_delay_s(text: str, *, terminal_incomplete: bool) -> float:
    """Keep clearly complete captions snappy while giving tiny bridge fragments
    a bit more time for merge repair to cancel the pending commit."""
    if terminal_incomplete:
        return TERMINAL_INCOMPLETE_COMMIT_DELAY_S
    word_count = len(text.split())
    if word_count <= 3:
        return SHORT_FRAGMENT_COMMIT_DELAY_S
    return PREFERRED_COMMIT_DELAY_S


class ServiceSession:
    """One active session per church_id. Owns the STT session, sentence
    buffer, translation, enrichment, topic tracking, and the admin WebSocket."""

    def __init__(self, church_id: str, ws: WebSocket, broadcaster: Broadcaster):
        self._church_id = church_id
        self._ws = ws
        self._broadcaster = broadcaster
        self._sample_rate = 48000
        self._db_session_id: int | None = None
        self._stt_session = None
        self._sentence_buffer: SentenceBuffer | None = None
        self._translation: GoogleTranslateService | None = None
        self._enrichment: LLMEnrichmentService | None = None
        self._topic_tracker: TopicTracker | None = None
        self._state_tracker: SermonStateTracker | None = None
        # Maps sentence ts → timing/flush metadata so enrichment can distinguish
        # normal committed captions from session-end truncated tails.
        self._pending_audio_timing: dict[int, dict[str, float | bool | str]] = {}
        # ts values for which LLM enrichment has completed — used to suppress
        # stale Google dual-pass corrections that arrive after the LLM has settled.
        self._enrichment_settled: set[int] = set()
        # Session-level STT noise removal counter (Pentecostés, Santo, etc.)
        self._stt_noise_removed_count: int = 0
        self._source_scripture_version: str = "rvr1960"
        self._display_scripture_version: str = "kjv"
        self._recorder: SessionRecorder | None = None
        self._pending_feed_commits: dict[int, dict] = {}
        self._committed_segment_ids: set[int] = set()
        self._persisted_segment_ids: set[int] = set()
        self._segment_text_cache: dict[int, dict] = {}
        self._segment_stt_cache: dict[int, dict] = {}
        self._segment_metadata_cache: dict[int, dict] = {}
        self._pending_segment_metadata: dict[int, dict] = {}
        self._pending_detected_verses: dict[int, dict] = {}
        self._pending_suggested_verses: dict[int, list[dict]] = {}
        self._last_segment_id: int = 0
        self._stt_config: STTConfig = STTConfig()

    def _ensure_segment_stt_cache(self) -> dict[int, dict]:
        cache = getattr(self, "_segment_stt_cache", None)
        if cache is None:
            cache = {}
            self._segment_stt_cache = cache
        return cache

    async def start(
        self,
        sample_rate: int,
        sermon_topic: str = "",
        source_scripture_version: str = "rvr1960",
        display_scripture_version: str = "kjv",
        stt_config: STTConfig | None = None,
    ):
        self._sample_rate = sample_rate
        self._source_scripture_version = source_scripture_version or "rvr1960"
        self._display_scripture_version = display_scripture_version or "kjv"
        self._stt_config = stt_config or STTConfig()
        self._db_session_id = await create_service_session(self._church_id)

        if os.getenv("SESSION_CAPTURE_ENABLED"):
            self._recorder = SessionRecorder(self._db_session_id, self._church_id)
            self._recorder.record_event("session_start", {
                "church_id": self._church_id, "sample_rate": sample_rate,
            })

        glossary = await get_glossary(self._church_id)
        church_terms = await load_church_terms(self._church_id)

        self._topic_tracker = TopicTracker(
            church_id=self._church_id,
            sermon_topic=sermon_topic,
        )

        self._state_tracker = SermonStateTracker(
            on_mode_change=self._on_mode_change,
        )

        self._sentence_buffer = SentenceBuffer(on_sentence=self._on_sentence)

        self._translation = GoogleTranslateService(
            on_translation=self._on_translation,
            on_correction=self._on_correction,
            on_interim_translation=self._on_interim_translation,
        )

        self._enrichment = LLMEnrichmentService(
            church_id=self._church_id,
            church_terms=church_terms,
            topic_tracker=self._topic_tracker,
            on_translation_update=self._on_translation_update,
            on_phrase_alignment=self._on_phrase_alignment,
            on_verse_detected=self._on_verse_detected,
            on_verse_range_update=self._on_verse_range_update,
            on_verse_suggestion=self._on_verse_suggestion,
            on_enrichment_settled=self._on_enrichment_settled,
            on_buffer_hold=self._on_buffer_hold,
            on_caption_merge=self._on_caption_merge,
            on_segment_metadata=self._on_segment_metadata,
            session_id=self._db_session_id,
            state_tracker=self._state_tracker,
        )

        self._stt_session = GoogleSpeechSession(
            church_id=self._church_id,
            on_interim=self._on_interim,
            on_final=self._on_final,
            on_utterance_end=self._on_utterance_end,
        )
        await self._stt_session.start(glossary=glossary, sample_rate=16000, stt_config=self._stt_config)

        await self._send({
            "type": "session_started",
            "sessionId": self._db_session_id,
            "sourceScriptureVersion": self._source_scripture_version,
            "displayScriptureVersion": self._display_scripture_version,
            "sttConfig": self._stt_config.public_payload(),
        })
        logger.info(
            "[session] Started for church %s (db_id=%s, topic=%r, source_version=%s, display_version=%s, stt_model=%s, stt_languages=%s)",
            self._church_id,
            self._db_session_id,
            sermon_topic or "(none)",
            self._source_scripture_version,
            self._display_scripture_version,
            self._stt_config.model,
            ",".join(self._stt_config.language_codes),
        )

    async def ingest(self, audio_b64: str):
        """Receive a base64 Float32 chunk from the browser, resample, forward to STT."""
        raw = base64_to_float32_bytes(audio_b64)
        pcm16 = resample_float32_to_pcm16(raw, self._sample_rate, dst_rate=16000)
        if self._recorder:
            self._recorder.record_audio(pcm16)
        if self._stt_session:
            await self._stt_session.send(pcm16)

    async def close(self):
        if self._recorder:
            try:
                self._recorder.record_event("session_stop", {"duration_s": 0})
                result = self._recorder.stop()
                await _finalize_capture_in_db(result, self._db_session_id)
            except Exception as e:
                logger.warning("[session] Recorder stop failed: %s", e)
            self._recorder = None
        if self._stt_session:
            await self._stt_session.stop()
        if self._sentence_buffer:
            await self._sentence_buffer.stop()
        if self._translation:
            await self._translation.close()
        if self._enrichment:
            await self._enrichment.close()
        await self._flush_all_pending_commits()
        if self._topic_tracker:
            await self._topic_tracker.stop()
        if self._db_session_id:
            await close_service_session(self._db_session_id)
        logger.info("[session] Closed for church %s", self._church_id)

    # --- STT callbacks ---

    async def _on_utterance_end(self):
        """STT VAD fired utterance end — speaker paused long enough that the
        current buffered fragments form a complete thought. Hard-flush the buffer."""
        if self._sentence_buffer:
            await self._sentence_buffer.utterance_end()

    async def _on_interim(self, text: str, stt_meta: dict | None = None):
        stt_meta = dict(stt_meta or {})
        await self._broadcast({"type": "interim", "text": text, "ts": _now(), **stt_meta})
        preview = _clean_stt(text)
        if preview and self._translation:
            if _translation_mode(stt_meta) == "english":
                await self._on_interim_translation(preview, "stt_passthrough", True)
            else:
                await self._translation.translate_interim(preview)

    async def _on_final(self, text: str, audio_start: float, audio_end: float, stt_meta: dict):
        logger.info("[session:%s] STT final: %s", self._church_id, text)
        await self._broadcast({"type": "stt_final", "text": text, "ts": _now(), **stt_meta})
        if self._recorder:
            _stt_ts = _now()
            self._recorder.record_event("stt_final", {
                "text": text,
                "audio_start": audio_start,
                "audio_end": audio_end,
                "ts": _stt_ts,
                **stt_meta,
            })
            self._recorder.record_timing("stt", _stt_ts)
        # Clean noise artifacts before segmentation; broadcast keeps the raw text.
        clean = _clean_stt(text)
        if clean != text:
            self._stt_noise_removed_count += 1
            logger.debug(
                "[session:%s] STT noise removed (count=%d): %r → %r",
                self._church_id, self._stt_noise_removed_count, text[:60], clean[:60],
            )
        if not clean:
            return
        if self._translation:
            if _translation_mode(stt_meta) == "english":
                await self._on_interim_translation(clean, "stt_passthrough", True)
            else:
                await self._translation.translate_fragment(clean)
        if self._sentence_buffer:
            if stt_meta.get("low_confidence"):
                self._sentence_buffer.hold_next(
                    "low_confidence_stt",
                    hold_secs=self._stt_config.low_confidence_hold_secs,
                )
                logger.debug(
                    "[session:%s] Hold set: low_confidence_stt avg_conf=%.3f threshold=%.3f",
                    self._church_id,
                    float(stt_meta.get("avg_confidence", 0.0)),
                    self._stt_config.confidence_hold_threshold,
                )
            # Proactive hold: if this fragment contains a quote introduction, set
            # a hold BEFORE adding it so the buffer's next timer waits for the
            # actual quote content to arrive. This covers the case where the intro
            # and the quote span separate STT finals — the intro accumulates
            # in the buffer with extra time for the quote to join it.
            if _QUOTE_INTRO.search(clean):
                self._sentence_buffer.hold_next("quote_introduction_proactive", hold_secs=4.0)
                logger.debug("[session:%s] Proactive hold: quote_introduction", self._church_id)
            parts = _split_segments(clean)
            if len(parts) == 1:
                await self._sentence_buffer.add(clean, audio_start, audio_end, stt_meta=stt_meta)
            else:
                # Distribute audio timing across sub-sentences proportionally by word count.
                total_words = max(sum(len(p.split()) for p in parts), 1)
                t = audio_start
                for part in parts:
                    part_end = t + (audio_end - audio_start) * len(part.split()) / total_words
                    await self._sentence_buffer.add(part, t, min(part_end, audio_end), stt_meta=stt_meta)
                    t = part_end

    # --- Sentence buffer callback ---

    async def _on_sentence(
        self,
        text: str,
        audio_start: float,
        audio_end: float,
        flush_reason: str,
        stt_context: dict | None = None,
    ):
        ts = self._next_segment_id()
        terminal_incomplete = flush_reason == "session_close" and _is_incomplete(text)
        stt_context = dict(stt_context or {})
        self._ensure_segment_stt_cache()[ts] = stt_context
        if self._recorder:
            self._recorder.record_event("sentence_flush", {
                "text": text, "flush_reason": flush_reason,
                "audio_start": audio_start, "audio_end": audio_end, "ts": ts, "segment_id": ts,
                **stt_context,
            })
            self._recorder.record_timing("sentence", ts)
        logger.info("[session:%s] Sentence flushed: %s", self._church_id, text)
        await self._broadcast({
            "type": "final_spanish",
            "text": text,
            "flush_reason": flush_reason,
            "terminal_incomplete": terminal_incomplete,
            **stt_context,
            **self._segment_ref(ts),
        })
        if self._topic_tracker:
            mode = self._state_tracker.settled_mode if self._state_tracker else "exposition"
            self._topic_tracker.add_segment(text, mode=mode)

        # Discourse-based holds — applied synchronously here (no LLM wait, no race).
        # We analyse the just-flushed Spanish text and ask the buffer to extend its
        # timer for the next sentence if we can predict what kind of content follows.
        if self._sentence_buffer:
            stripped = text.rstrip()
            if _QUOTE_INTRO.search(text):
                # The preacher just introduced a quotation. The next sentence is
                # almost certainly scripture — give it extra time to arrive in full.
                self._sentence_buffer.hold_next("quote_introduction", hold_secs=4.0)
                logger.debug("[session:%s] Hold set: quote_introduction", self._church_id)
            elif stripped.endswith('?'):
                # Rhetorical question — the preacher will likely answer it immediately.
                # Hold briefly so the answer arrives before we flush the question.
                self._sentence_buffer.hold_next("rhetorical_question", hold_secs=2.0)
                logger.debug("[session:%s] Hold set: rhetorical_question", self._church_id)

        if self._translation:
            commit_delay_s = _preferred_commit_delay_s(
                text,
                terminal_incomplete=terminal_incomplete,
            )
            self._pending_audio_timing[ts] = {
                "audio_start": audio_start,
                "audio_end": audio_end,
                "terminal_incomplete": terminal_incomplete,
                "flush_reason": flush_reason,
                "stt_context": stt_context,
                "commit_delay_s": commit_delay_s,
            }
            # Prune entries older than 120s — these belong to sentences whose
            # translation failed after all retries and will never be consumed.
            cutoff = ts - 120_000
            stale = [k for k in self._pending_audio_timing if k < cutoff]
            for k in stale:
                del self._pending_audio_timing[k]
            stale_settled = [k for k in self._enrichment_settled if k < cutoff]
            for k in stale_settled:
                self._enrichment_settled.discard(k)
            if _translation_mode(stt_context) == "english":
                await self._emit_passthrough_sentence(text, ts, stt_context)
            else:
                await self._translation.translate(text, ts)

    # --- Google Translation callbacks ---

    async def _emit_passthrough_sentence(self, text: str, ts: int, stt_context: dict | None = None):
        timing = self._pending_audio_timing.pop(
            ts,
            {
                "audio_start": 0.0,
                "audio_end": 0.0,
                "terminal_incomplete": False,
                "flush_reason": "",
                "stt_context": stt_context or {},
                "commit_delay_s": PREFERRED_COMMIT_DELAY_S,
            },
        )
        stt_context = dict(stt_context or timing.get("stt_context") or {})
        english = text
        if timing.get("terminal_incomplete"):
            english = _format_deferred_release_text(english, english)
        logger.info("[session:%s] English passthrough: %s", self._church_id, english[:200])
        if self._recorder:
            self._recorder.record_event(
                "translation",
                {"spanish": text, "english": english, "ts": ts, "source": "passthrough"},
            )
            self._recorder.record_timing("translation", ts)
        await self._broadcast_live_translation(
            text=english,
            source="stt_passthrough",
            display_ready=False,
            segment_id=ts,
            merge_strategy="replace",
        )
        await self._queue_feed_commit(
            segment_id=ts,
            spanish=text,
            english=english,
            source="passthrough",
            phrase_alignment=None,
            delay_s=float(timing.get("commit_delay_s", PREFERRED_COMMIT_DELAY_S)),
            stt_context=stt_context,
        )

    async def _on_translation(self, spanish: str, english: str, ts: int):
        timing = self._pending_audio_timing.get(
            ts,
            {
                "audio_start": 0.0,
                "audio_end": 0.0,
                "terminal_incomplete": False,
                "flush_reason": "",
                "stt_context": {},
                "commit_delay_s": PREFERRED_COMMIT_DELAY_S,
            },
        )
        stt_context = dict(timing.get("stt_context") or {})
        if timing.get("terminal_incomplete"):
            english = _format_deferred_release_text(english, english)
        logger.info("[session:%s] Translation: %s -> %s", self._church_id, spanish[:200], english[:200])
        if self._recorder:
            self._recorder.record_event("translation", {"spanish": spanish, "english": english, "ts": ts})
            self._recorder.record_timing("translation", ts)
        await self._broadcast_live_translation(
            text=english,
            source="google_sentence",
            display_ready=False,
            segment_id=ts,
            merge_strategy="replace",
        )
        await self._queue_feed_commit(
            segment_id=ts,
            spanish=spanish,
            english=english,
            source="google",
            phrase_alignment=None,
            delay_s=float(timing.get("commit_delay_s", PREFERRED_COMMIT_DELAY_S)),
            stt_context=stt_context,
        )
        if self._enrichment:
            # Pop timing; defaults to (0.0, 0.0) if translation was retried after
            # the entry aged out (extremely rare — session would need to be very long).
            timing = self._pending_audio_timing.pop(
                ts,
                {
                    "audio_start": 0.0,
                    "audio_end": 0.0,
                    "terminal_incomplete": False,
                    "flush_reason": "",
                    "stt_context": {},
                    "commit_delay_s": PREFERRED_COMMIT_DELAY_S,
                },
            )
            audio_start = float(timing.get("audio_start", 0.0))
            audio_end = float(timing.get("audio_end", 0.0))
            self._enrichment.enrich(
                spanish,
                english,
                ts,
                audio_start,
                audio_end,
                terminal_incomplete=bool(timing.get("terminal_incomplete")),
                stt_context=stt_context,
            )

    async def _on_interim_translation(
        self,
        text: str,
        source: str = "google_fragment",
        replace: bool = False,
    ):
        await self._broadcast_live_translation(
            text=text,
            source=source,
            display_ready=False,
            live_ts=_now(),
            merge_strategy="replace" if replace else "append",
        )

    async def _on_correction(self, ts: int, english: str):
        """Silently update a previously broadcast translation with better context.

        Suppressed if LLM enrichment has already settled for this ts — the LLM
        output always takes priority over Google's dual-pass correction.
        """
        if ts in self._enrichment_settled:
            logger.info(
                "[session:%s] Correction suppressed ts=%d — enrichment already settled",
                self._church_id, ts,
            )
            await self._broadcast({"type": "correction_suppressed", **self._segment_ref(ts)})
            return
        if ts in self._pending_feed_commits:
            self._pending_feed_commits[ts]["english"] = english
            self._pending_feed_commits[ts]["source"] = "google"
            self._pending_feed_commits[ts]["phrase_alignment"] = None
            await self._broadcast_live_translation(
                text=english,
                source="google_correction",
                display_ready=False,
                segment_id=ts,
                merge_strategy="replace",
            )
            return
        await self._broadcast_feed_revision(
            segment_id=ts,
            english=english,
            source="google",
            reason="forward_context_correction",
            phrase_alignment=None,
        )

    # --- LLM Enrichment callbacks ---

    async def _on_buffer_hold(self, reason: str, hold_secs: float):
        """LLM enrichment signals that the previous sentence was incomplete.

        Called when thought_complete=false — the buffer should hold the next
        sentence longer, giving the speaker's continuation more time to arrive
        and accumulate before flushing. This is a forward correction: it can't
        un-flush the incomplete sentence, but it prevents the same pattern from
        cascading into the next sentence boundary.
        """
        if self._sentence_buffer:
            self._sentence_buffer.hold_next(reason, hold_secs)
            logger.info(
                "[session:%s] Buffer hold from enrichment: %s (%.1fs)", self._church_id, reason, hold_secs
            )

    async def _on_translation_update(self, ts: int, english: str, phrase_alignment: list[dict] | None = None):
        """LLM-improved translation; replaces the Google translation on the display."""
        logger.info("[session:%s] Translation update ts=%d: %s", self._church_id, ts, english[:200])
        self._enrichment_settled.add(ts)
        if ts in self._pending_feed_commits:
            pending = self._pending_feed_commits[ts]
            pending["english"] = english
            pending["source"] = "llm"
            pending["phrase_alignment"] = phrase_alignment
            await self._broadcast_live_translation(
                text=english,
                source="llm",
                display_ready=True,
                segment_id=ts,
                merge_strategy="replace",
            )
            await self._commit_pending_segment(ts)
            return
        await self._broadcast_feed_revision(
            segment_id=ts,
            english=english,
            source="llm",
            reason="context_repair",
            phrase_alignment=phrase_alignment,
        )

    async def _on_phrase_alignment(self, ts: int, phrase_alignment: list[dict]):
        if not phrase_alignment:
            return
        if ts in self._pending_feed_commits:
            self._pending_feed_commits[ts]["phrase_alignment"] = phrase_alignment
            return
        cached = self._segment_text_cache.get(ts)
        if not cached:
            return
        await self._broadcast_feed_revision(
            segment_id=ts,
            english=cached.get("english", ""),
            source="llm",
            reason="phrase_alignment",
            phrase_alignment=phrase_alignment,
        )

    async def _on_enrichment_settled(self, ts: int):
        """LLM enrichment completed (with or without a translation change).
        Marks the sentence settled so late-arriving corrections are suppressed."""
        self._enrichment_settled.add(ts)
        if self._recorder:
            self._recorder.record_event("enrichment_settled", {"ts": ts})
            self._recorder.record_timing("enrichment", ts)

    async def _hydrate_detected_verse(self, verse: dict) -> dict:
        payload = dict(verse)
        payload["explanation"] = (
            "Detected as an explicit scripture citation."
            if payload.get("confidence") == "explicit"
            else "Detected as quoted scripture based on the sermon wording."
        )
        payload["source_version_slug"] = self._source_scripture_version
        payload["display_version_slug"] = self._display_scripture_version
        try:
            source_passage = await get_passage(
                self._source_scripture_version,
                payload["book"],
                int(payload["chapter"]),
                int(payload["verse_start"]),
                payload.get("verse_end"),
            )
            display_passage = await get_passage(
                self._display_scripture_version,
                payload["book"],
                int(payload["chapter"]),
                int(payload["verse_start"]),
                payload.get("verse_end"),
            )
            payload["source_passage"] = source_passage
            payload["display_passage"] = display_passage
            if display_passage:
                payload["canonical_english"] = " ".join(v["text"] for v in display_passage["verses"])
        except Exception as e:
            logger.warning(
                "[session:%s] Failed to hydrate detected verse %s: %s",
                self._church_id,
                payload.get("reference"),
                e,
            )
            payload["source_passage"] = None
            payload["display_passage"] = None
        return payload

    async def _hydrate_suggested_verse(self, suggestion: dict) -> dict:
        payload = dict(suggestion)
        payload["explanation"] = payload.get("relevance_note", "")
        payload["source_version_slug"] = self._source_scripture_version
        payload["display_version_slug"] = self._display_scripture_version
        try:
            source_passage = await get_passage_by_reference(
                self._source_scripture_version,
                payload["reference"],
            )
            display_passage = await get_passage_by_reference(
                self._display_scripture_version,
                payload["reference"],
            )
            payload["source_passage"] = source_passage
            payload["display_passage"] = display_passage
            if display_passage:
                payload["canonical_english"] = " ".join(v["text"] for v in display_passage["verses"])
        except Exception as e:
            logger.warning(
                "[session:%s] Failed to hydrate suggested verse %s: %s",
                self._church_id,
                payload.get("reference"),
                e,
            )
            payload["source_passage"] = None
            payload["display_passage"] = None
        return payload

    async def _on_verse_detected(self, ts: int, verse: dict):
        verse = await self._hydrate_detected_verse(verse)
        logger.info("[session:%s] Verse detected: %s", self._church_id, verse.get("reference"))
        if ts not in self._committed_segment_ids:
            self._pending_detected_verses[ts] = verse
            return
        await self._broadcast({"type": "verse_detected", "verse": verse, **self._segment_ref(ts)})

    async def _on_verse_range_update(self, ts: int, verse: dict):
        verse = await self._hydrate_detected_verse(verse)
        logger.info("[session:%s] Verse range update: %s", self._church_id, verse.get("reference"))
        if ts not in self._committed_segment_ids:
            self._pending_detected_verses[ts] = verse
            return
        await self._broadcast({"type": "verse_range_update", "verse": verse, **self._segment_ref(ts)})

    async def _on_verse_suggestion(self, ts: int, suggestions: list[dict]):
        suggestions = [await self._hydrate_suggested_verse(s) for s in suggestions]
        logger.info(
            "[session:%s] Verse suggestions for ts=%d: %s",
            self._church_id, ts, [s["reference"] for s in suggestions],
        )
        if ts not in self._committed_segment_ids:
            self._pending_suggested_verses[ts] = suggestions
            return
        await self._broadcast({"type": "verse_suggestion", "suggestions": suggestions, **self._segment_ref(ts)})

    async def _on_caption_merge(self, absorb_ts: int, keep_ts: int, merged_spanish: str, merged_english: str):
        """LLM signals that two segments should be merged to repair a bad stream split.

        The chain is head-anchored: keep_ts is always the earliest visible segment
        (the anchor); absorb_ts is the fragment being folded into it.
        Broadcasts caption_merge as a segmentation-repair event.
        """
        logger.info(
            "[session:%s] Caption merge: keep=%d absorbs=%d",
            self._church_id, keep_ts, absorb_ts,
        )
        keep_was_committed = keep_ts in self._committed_segment_ids
        await self._drop_pending_commit(absorb_ts)
        self._segment_text_cache.pop(absorb_ts, None)
        self._ensure_segment_stt_cache().pop(absorb_ts, None)
        self._segment_metadata_cache.pop(absorb_ts, None)
        self._pending_segment_metadata.pop(absorb_ts, None)
        self._pending_detected_verses.pop(absorb_ts, None)
        self._pending_suggested_verses.pop(absorb_ts, None)
        if keep_ts in self._pending_feed_commits:
            pending = self._pending_feed_commits[keep_ts]
            pending["spanish"] = merged_spanish
            pending["english"] = merged_english
            pending["source"] = "llm"
            pending["phrase_alignment"] = None
            await self._broadcast_live_translation(
                text=merged_english,
                source="llm",
                display_ready=True,
                segment_id=keep_ts,
                merge_strategy="replace",
            )
            await self._commit_pending_segment(keep_ts)
        await self._broadcast({
            "type": "caption_merge",
            "reason": "segmentation_repair",
            "spanish": merged_spanish,
            "english": merged_english,
            **self._merge_ref(keep_ts, absorb_ts),
        })
        if keep_was_committed:
            await self._broadcast_feed_revision(
                segment_id=keep_ts,
                english=merged_english,
                spanish=merged_spanish,
                source="llm",
                reason="segmentation_repair",
                phrase_alignment=None,
            )
        if self._enrichment:
            metadata = self._segment_metadata_cache.get(keep_ts, {})
            self._enrichment.request_phrase_alignment(
                ts=keep_ts,
                spanish=merged_spanish,
                english=merged_english,
                source_quality=str(metadata.get("source_quality", "clean")),
                translation_register=str(metadata.get("translation_register", "expository")),
            )

    async def _on_segment_metadata(self, ts: int, metadata: dict):
        """Broadcast scaffolding metadata for a committed segment.

        Carries translation_register, paragraph_break, and source_quality.
        The frontend stores these for future display logic (e.g. register will
        drive exact Bible verse text lookup once Bible versions are stored).
        """
        metadata = {
            **metadata,
            **self._ensure_segment_stt_cache().get(ts, {}),
        }
        self._pending_segment_metadata[ts] = metadata
        self._segment_metadata_cache[ts] = dict(metadata)
        if metadata.get("pending_completion") and ts in self._pending_feed_commits:
            pending = self._pending_feed_commits[ts]
            task = pending.get("task")
            if task:
                task.cancel()
                pending["task"] = None
        if ts in self._committed_segment_ids:
            await self._broadcast({
                "type": "segment_metadata",
                **metadata,
                **self._segment_ref(ts),
            })

    async def _on_mode_change(self, old_mode: str, new_mode: str, ts: int):
        """Fired when the settled sermon mode transitions."""
        logger.info(
            "[session:%s] Mode transition: %s → %s (ts=%d)",
            self._church_id, old_mode, new_mode, ts,
        )
        if self._db_session_id:
            try:
                await save_mode_transition(self._db_session_id, old_mode, new_mode, ts)
            except Exception as e:
                logger.warning("[session:%s] save_mode_transition failed: %s", self._church_id, e)
        await self._broadcast({
            "type": "mode_change",
            "from": old_mode,
            "to": new_mode,
            **self._segment_ref(ts),
        })

    # --- Helpers ---

    def get_stats(self) -> dict:
        """Return operational metrics for this session. Used by the stats endpoint."""
        return {
            "sentence_buffer": {
                "structural_flush_block_count": (
                    self._sentence_buffer.structural_flush_block_count
                    if self._sentence_buffer else 0
                ),
                "forced_release_count": (
                    self._sentence_buffer.forced_release_count
                    if self._sentence_buffer else 0
                ),
                "conditional_flush_block_count": (
                    self._sentence_buffer.conditional_flush_block_count
                    if self._sentence_buffer else 0
                ),
            },
            "enrichment": dict(self._enrichment.metrics) if self._enrichment else {},
            "stt_session": self._stt_session.get_stats() if self._stt_session else {},
            "stt_noise_removed_count": self._stt_noise_removed_count,
            "_enrichment_settled_size": len(self._enrichment_settled),
            "session_id": self._db_session_id,
            "latency_ms": self._recorder.compute_latency() if self._recorder else {
                "stt_to_sentence": {"p50": None, "p90": None, "count": 0},
                "sentence_to_translation": {"p50": None, "p90": None, "count": 0},
                "translation_to_enrichment": {"p50": None, "p90": None, "count": 0},
            },
            "capture_active": self._recorder is not None,
        }

    async def _broadcast(self, event: dict):
        await self._broadcaster.publish(self._church_id, event)

    async def _queue_feed_commit(
        self,
        segment_id: int,
        spanish: str,
        english: str,
        source: str,
        phrase_alignment: list[dict] | None,
        delay_s: float,
        stt_context: dict | None = None,
    ) -> None:
        await self._drop_pending_commit(segment_id)
        task = asyncio.create_task(self._delayed_feed_commit(segment_id, delay_s))
        self._pending_feed_commits[segment_id] = {
            "spanish": spanish,
            "english": english,
            "source": source,
            "phrase_alignment": phrase_alignment,
            "stt_context": dict(stt_context or {}),
            "task": task,
        }

    async def _drop_pending_commit(self, segment_id: int) -> None:
        pending = self._pending_feed_commits.pop(segment_id, None)
        if not pending:
            return
        task = pending.get("task")
        if task:
            task.cancel()

    async def _delayed_feed_commit(self, segment_id: int, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
            await self._commit_pending_segment(segment_id)
        except asyncio.CancelledError:
            return

    async def _commit_pending_segment(self, segment_id: int) -> None:
        pending = self._pending_feed_commits.pop(segment_id, None)
        if not pending:
            return
        is_first_commit = segment_id not in self._committed_segment_ids
        task = pending.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
        await self._broadcast_feed_commit(
            segment_id=segment_id,
            spanish=pending["spanish"],
            english=pending["english"],
            source=pending["source"],
            phrase_alignment=pending.get("phrase_alignment"),
            stt_context=pending.get("stt_context"),
        )
        await self._broadcast_live_translation_clear(reason="committed", segment_id=segment_id)
        self._committed_segment_ids.add(segment_id)
        if self._db_session_id and is_first_commit and segment_id not in self._persisted_segment_ids:
            await append_segment(self._db_session_id, pending["spanish"], pending["english"])
            self._persisted_segment_ids.add(segment_id)
        await self._flush_buffered_segment_state(segment_id)

    async def _flush_buffered_segment_state(self, segment_id: int) -> None:
        metadata = self._pending_segment_metadata.pop(segment_id, None)
        if metadata is not None:
            await self._broadcast({
                "type": "segment_metadata",
                **metadata,
                **self._segment_ref(segment_id),
            })
        verse = self._pending_detected_verses.pop(segment_id, None)
        if verse is not None:
            await self._broadcast({"type": "verse_detected", "verse": verse, **self._segment_ref(segment_id)})
        suggestions = self._pending_suggested_verses.pop(segment_id, None)
        if suggestions is not None:
            await self._broadcast({"type": "verse_suggestion", "suggestions": suggestions, **self._segment_ref(segment_id)})

    async def _flush_all_pending_commits(self) -> None:
        for segment_id in list(self._pending_feed_commits.keys()):
            await self._commit_pending_segment(segment_id)

    async def _broadcast_live_translation(
        self,
        text: str,
        source: str,
        display_ready: bool,
        live_ts: int | None = None,
        segment_id: int | None = None,
        merge_strategy: str = "append",
    ) -> None:
        payload = {
            "type": "live_translation",
            "text": text,
            "source": source,
            "display_ready": display_ready,
            "merge_strategy": merge_strategy,
        }
        if segment_id is not None:
            payload.update(self._segment_ref(segment_id))
        else:
            payload["ts"] = live_ts if live_ts is not None else _now()
        await self._broadcast(payload)

    async def _broadcast_live_translation_clear(
        self,
        reason: str,
        segment_id: int | None = None,
    ) -> None:
        payload = {
            "type": "live_translation_clear",
            "reason": reason,
        }
        if segment_id is not None:
            payload.update(self._segment_ref(segment_id))
        else:
            payload["ts"] = _now()
        await self._broadcast(payload)

    async def _broadcast_feed_commit(
        self,
        segment_id: int,
        spanish: str,
        english: str,
        source: str,
        phrase_alignment: list[dict] | None,
        stt_context: dict | None = None,
    ) -> None:
        stt_context = dict(stt_context or {})
        self._segment_text_cache[segment_id] = {
            "spanish": spanish,
            "english": english,
        }
        self._ensure_segment_stt_cache()[segment_id] = stt_context
        payload = {
            "type": "feed_commit",
            "spanish": spanish,
            "english": english,
            "source": source,
            **stt_context,
            **self._segment_ref(segment_id),
        }
        if phrase_alignment:
            payload["phrase_alignment"] = phrase_alignment
        await self._broadcast(payload)

    async def _broadcast_feed_revision(
        self,
        segment_id: int,
        english: str,
        source: str,
        reason: str,
        spanish: str | None = None,
        phrase_alignment: list[dict] | None = None,
    ) -> None:
        cached = self._segment_text_cache.get(segment_id, {})
        self._segment_text_cache[segment_id] = {
            "spanish": spanish if spanish is not None else cached.get("spanish", ""),
            "english": english,
        }
        stt_context = self._ensure_segment_stt_cache().get(segment_id, {})
        payload = {
            "type": "feed_revision",
            "english": english,
            "source": source,
            "reason": reason,
            **stt_context,
            **self._segment_ref(segment_id),
        }
        if spanish is not None:
            payload["spanish"] = spanish
        if phrase_alignment:
            payload["phrase_alignment"] = phrase_alignment
        await self._broadcast(payload)

    async def _send(self, msg: dict):
        try:
            await self._ws.send_json(msg)
        except Exception:
            pass

    def _next_segment_id(self) -> int:
        now = _now()
        if now <= self._last_segment_id:
            now = self._last_segment_id + 1
        self._last_segment_id = now
        return now

    def _segment_ref(self, segment_id: int) -> dict:
        """Emit canonical segment identity alongside the legacy timestamp field."""
        return {
            "segment_id": segment_id,
            "ts": segment_id,
        }

    def _merge_ref(self, keep_segment_id: int, absorb_segment_id: int) -> dict:
        """Emit merge compatibility fields while preserving canonical IDs."""
        return {
            "segment_id_keep": keep_segment_id,
            "segment_id_absorb": absorb_segment_id,
            "ts_keep": keep_segment_id,
            "ts_absorb": absorb_segment_id,
        }


async def _finalize_capture_in_db(result: CaptureResult, session_id: int | None) -> None:
    """Persist capture file paths and metrics to the session_captures table."""
    if not session_id:
        return
    from server.db.sessions import create_capture_record, finalize_capture
    try:
        capture_id = await create_capture_record(session_id)
        await finalize_capture(
            capture_id,
            audio_path=result.audio_path or "",
            events_path=result.events_path or "",
            duration_s=result.duration_s,
            segment_count=result.segment_count,
        )
    except Exception as e:
        logger.warning("[session] DB capture finalize failed: %s", e)


class SessionManager:
    """Tracks one active ServiceSession per church_id."""

    def __init__(self, broadcaster: Broadcaster):
        self._broadcaster = broadcaster
        self._sessions: dict[str, ServiceSession] = {}

    async def create(
        self,
        church_id: str,
        ws: WebSocket,
        sample_rate: int,
        sermon_topic: str = "",
        source_scripture_version: str = "rvr1960",
        display_scripture_version: str = "kjv",
        stt_config: STTConfig | None = None,
    ) -> ServiceSession:
        if church_id in self._sessions:
            await self._sessions[church_id].close()

        session = ServiceSession(church_id, ws, self._broadcaster)
        self._sessions[church_id] = session
        await session.start(
            sample_rate,
            sermon_topic=sermon_topic,
            source_scripture_version=source_scripture_version,
            display_scripture_version=display_scripture_version,
            stt_config=stt_config,
        )
        return session

    async def remove(self, church_id: str):
        session = self._sessions.pop(church_id, None)
        if session:
            await session.close()

    def get(self, church_id: str) -> ServiceSession | None:
        return self._sessions.get(church_id)


def _now() -> int:
    return int(time.time() * 1000)
