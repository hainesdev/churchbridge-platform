import logging
import re
import time
from fastapi import WebSocket

from server.db.glossary import get_glossary
from server.db.church_terms import load_church_terms
from server.db.modes import save_mode_transition
from server.db.sessions import (
    create_service_session,
    close_service_session,
    append_segment,
)
from server.services.audio_utils import resample_float32_to_pcm16, base64_to_float32_bytes
from server.services.deepgram_session import DeepgramSession
from server.services.google_translate_service import GoogleTranslateService
from server.services.llm_enrichment_service import LLMEnrichmentService
from server.services.sentence_buffer import SentenceBuffer
from server.services.sermon_state_tracker import SermonStateTracker
from server.services.topic_tracker import TopicTracker
from server.services.broadcaster import Broadcaster

logger = logging.getLogger(__name__)

# Splits a Deepgram final at internal sentence boundaries — e.g.
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

# Repeated content word (stutter): "que que" → "que", "el el" → "el"
# Limited to short words (≤ 5 chars) to avoid collapsing intentional emphasis
# like "muy muy" (very very) in longer words — but short function word repeats
# are always noise.
_STT_WORD_REPEAT = re.compile(r'\b(\w{1,5})\s+\1\b', re.IGNORECASE)

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
    """Rewrite 'Pentecostés' to 'Pentecostales' when context signals people/movement."""
    # Strategy 1: direct prefix match
    text = _PENTECOSTES_PEOPLE.sub(lambda m: m.group(1) + ' Pentecostales', text)
    # Strategy 2: discourse context — if ANY discourse marker co-occurs with Pentecostés
    if _PENTECOSTES_RE.search(text) and _PENTECOSTES_DISCOURSE.search(text):
        text = _PENTECOSTES_RE.sub('Pentecostales', text)
    return text


def _clean_stt(text: str) -> str:
    """Normalize STT output before segmentation, translation, and buffering.

    Applied in order, each pass targeted:
    1. Remove multi-char filler sounds (AAA, Uh, Mmm, Este...).
    2. Collapse repeated short function words ("que que" → "que").
    3. Collapse same-character stutters ("a a Cristo" → "a Cristo").
    4. Context-aware Pentecostés normalization.
    5. Normalize internal whitespace.

    The original raw text is still broadcast as stt_final so the operator stream
    is unmodified; only the pipeline-facing text is cleaned.
    """
    text = _STT_FILLER.sub('', text)
    text = _STT_WORD_REPEAT.sub(r'\1', text)
    # Keep the LAST instance of a stuttered single character so "A a Cristo" → "a Cristo"
    # (the article/preposition "a", not the filler "A").
    text = _STT_SINGLE_REPEAT.sub(lambda m: m.group(0).split()[-1], text)
    text = _normalize_pentecostes(text)
    return ' '.join(text.split())


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
    """Split a Deepgram final at internal sentence boundaries, then merge back
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
        if len(part.split()) < _MIN_SPLIT_WORDS:
            # Short answer or fragment — attach to the preceding sentence.
            merged[-1] = merged[-1] + ' ' + part
        else:
            merged.append(part)
    return merged


class ServiceSession:
    """One active session per church_id. Owns the Deepgram connection,
    SentenceBuffer, GoogleTranslateService, LLMEnrichmentService,
    TopicTracker, and the admin WebSocket."""

    def __init__(self, church_id: str, ws: WebSocket, broadcaster: Broadcaster):
        self._church_id = church_id
        self._ws = ws
        self._broadcaster = broadcaster
        self._sample_rate = 48000
        self._db_session_id: int | None = None
        self._deepgram: DeepgramSession | None = None
        self._sentence_buffer: SentenceBuffer | None = None
        self._translation: GoogleTranslateService | None = None
        self._enrichment: LLMEnrichmentService | None = None
        self._topic_tracker: TopicTracker | None = None
        self._state_tracker: SermonStateTracker | None = None
        # Maps sentence ts → (audio_start, audio_end) so enrichment receives
        # Deepgram sermon-relative timing even though translation is async.
        self._pending_audio_timing: dict[int, tuple[float, float]] = {}
        # ts values for which LLM enrichment has completed — used to suppress
        # stale Google dual-pass corrections that arrive after the LLM has settled.
        self._enrichment_settled: set[int] = set()

    async def start(self, sample_rate: int, sermon_topic: str = ""):
        self._sample_rate = sample_rate
        self._db_session_id = await create_service_session(self._church_id)

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
            on_verse_detected=self._on_verse_detected,
            on_verse_range_update=self._on_verse_range_update,
            on_verse_suggestion=self._on_verse_suggestion,
            on_enrichment_settled=self._on_enrichment_settled,
            on_buffer_hold=self._on_buffer_hold,
            session_id=self._db_session_id,
            state_tracker=self._state_tracker,
        )

        self._deepgram = DeepgramSession(
            church_id=self._church_id,
            on_interim=self._on_interim,
            on_final=self._on_final,
            on_utterance_end=self._on_utterance_end,
        )
        await self._deepgram.start(glossary=glossary, sample_rate=16000)

        await self._send({"type": "session_started", "sessionId": self._db_session_id})
        logger.info(
            "[session] Started for church %s (db_id=%s, topic=%r)",
            self._church_id, self._db_session_id, sermon_topic or "(none)",
        )

    async def ingest(self, audio_b64: str):
        """Receive a base64 Float32 chunk from the browser, resample, forward to Deepgram."""
        raw = base64_to_float32_bytes(audio_b64)
        pcm16 = resample_float32_to_pcm16(raw, self._sample_rate, dst_rate=16000)
        if self._deepgram:
            await self._deepgram.send(pcm16)

    async def close(self):
        if self._sentence_buffer:
            await self._sentence_buffer.stop()
        if self._deepgram:
            await self._deepgram.stop()
        if self._translation:
            await self._translation.close()
        if self._enrichment:
            await self._enrichment.close()
        if self._topic_tracker:
            await self._topic_tracker.stop()
        if self._db_session_id:
            await close_service_session(self._db_session_id)
        logger.info("[session] Closed for church %s", self._church_id)

    # --- Deepgram callbacks ---

    async def _on_utterance_end(self):
        """Deepgram VAD fired UtteranceEnd — speaker paused long enough that the
        current buffered fragments form a complete thought. Hard-flush the buffer."""
        if self._sentence_buffer:
            await self._sentence_buffer.utterance_end()

    async def _on_interim(self, text: str):
        await self._broadcast({"type": "interim", "text": text, "ts": _now()})

    async def _on_final(self, text: str, audio_start: float, audio_end: float):
        logger.info("[session:%s] STT final: %s", self._church_id, text)
        await self._broadcast({"type": "stt_final", "text": text, "ts": _now()})
        # Clean noise artifacts before segmentation; broadcast keeps the raw text.
        clean = _clean_stt(text)
        if not clean:
            return
        if self._translation:
            await self._translation.translate_fragment(clean)
        if self._sentence_buffer:
            # Proactive hold: if this fragment contains a quote introduction, set
            # a hold BEFORE adding it so the buffer's next timer waits for the
            # actual quote content to arrive. This covers the case where the intro
            # and the quote span separate Deepgram finals — the intro accumulates
            # in the buffer with extra time for the quote to join it.
            if _QUOTE_INTRO.search(clean):
                self._sentence_buffer.hold_next("quote_introduction_proactive", hold_secs=4.0)
                logger.debug("[session:%s] Proactive hold: quote_introduction", self._church_id)
            parts = _split_segments(clean)
            if len(parts) == 1:
                await self._sentence_buffer.add(clean, audio_start, audio_end)
            else:
                # Distribute audio timing across sub-sentences proportionally by word count.
                total_words = max(sum(len(p.split()) for p in parts), 1)
                t = audio_start
                for part in parts:
                    part_end = t + (audio_end - audio_start) * len(part.split()) / total_words
                    await self._sentence_buffer.add(part, t, min(part_end, audio_end))
                    t = part_end

    # --- Sentence buffer callback ---

    async def _on_sentence(self, text: str, audio_start: float, audio_end: float):
        ts = _now()
        logger.info("[session:%s] Sentence flushed: %s", self._church_id, text)
        await self._broadcast({"type": "final_spanish", "text": text, "ts": ts})
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
            self._pending_audio_timing[ts] = (audio_start, audio_end)
            # Prune entries older than 120s — these belong to sentences whose
            # translation failed after all retries and will never be consumed.
            cutoff = ts - 120_000
            stale = [k for k in self._pending_audio_timing if k < cutoff]
            for k in stale:
                del self._pending_audio_timing[k]
            await self._translation.translate(text, ts)

    # --- Google Translation callbacks ---

    async def _on_translation(self, spanish: str, english: str, ts: int):
        logger.info("[session:%s] Translation: %s -> %s", self._church_id, spanish[:200], english[:200])
        await self._broadcast({
            "type": "translation",
            "spanish": spanish,
            "english": english,
            "ts": ts,
        })
        if self._db_session_id:
            await append_segment(self._db_session_id, spanish, english)
        if self._enrichment:
            # Pop timing; defaults to (0.0, 0.0) if translation was retried after
            # the entry aged out (extremely rare — session would need to be very long).
            audio_start, audio_end = self._pending_audio_timing.pop(ts, (0.0, 0.0))
            self._enrichment.enrich(spanish, english, ts, audio_start, audio_end)

    async def _on_interim_translation(self, text: str):
        await self._broadcast({"type": "interim_translation", "text": text, "ts": _now()})

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
            return
        await self._broadcast({"type": "correction", "ts": ts, "english": english})

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

    async def _on_translation_update(self, ts: int, english: str):
        """LLM-improved translation; replaces the Google translation on the display."""
        logger.info("[session:%s] Translation update ts=%d: %s", self._church_id, ts, english[:200])
        self._enrichment_settled.add(ts)
        await self._broadcast({"type": "translation_update", "ts": ts, "english": english})

    async def _on_enrichment_settled(self, ts: int):
        """LLM enrichment completed (with or without a translation change).
        Marks the sentence settled so late-arriving corrections are suppressed."""
        self._enrichment_settled.add(ts)

    async def _on_verse_detected(self, ts: int, verse: dict):
        logger.info("[session:%s] Verse detected: %s", self._church_id, verse.get("reference"))
        await self._broadcast({"type": "verse_detected", "ts": ts, "verse": verse})

    async def _on_verse_range_update(self, ts: int, verse: dict):
        logger.info("[session:%s] Verse range update: %s", self._church_id, verse.get("reference"))
        await self._broadcast({"type": "verse_range_update", "ts": ts, "verse": verse})

    async def _on_verse_suggestion(self, ts: int, suggestions: list[dict]):
        logger.info(
            "[session:%s] Verse suggestions for ts=%d: %s",
            self._church_id, ts, [s["reference"] for s in suggestions],
        )
        await self._broadcast({"type": "verse_suggestion", "ts": ts, "suggestions": suggestions})

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
            "ts": ts,
        })

    # --- Helpers ---

    async def _broadcast(self, event: dict):
        await self._broadcaster.publish(self._church_id, event)

    async def _send(self, msg: dict):
        try:
            await self._ws.send_json(msg)
        except Exception:
            pass


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
    ) -> ServiceSession:
        if church_id in self._sessions:
            await self._sessions[church_id].close()

        session = ServiceSession(church_id, ws, self._broadcaster)
        self._sessions[church_id] = session
        await session.start(sample_rate, sermon_topic=sermon_topic)
        return session

    async def remove(self, church_id: str):
        session = self._sessions.pop(church_id, None)
        if session:
            await session.close()

    def get(self, church_id: str) -> ServiceSession | None:
        return self._sessions.get(church_id)


def _now() -> int:
    return int(time.time() * 1000)
