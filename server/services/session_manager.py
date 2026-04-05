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

# Strips common STT noise artifacts before translation — repeated filler sounds
# ("AAA", "Mm", "Uh") that slip through Deepgram and would pollute the translation.
# Applied to the text passed to Google and the sentence buffer; the original
# raw text is still broadcast as stt_final so the stream display stays unmodified.
_STT_NOISE = re.compile(r'\b(A{2,}|M{2,}|Uh+|Um+|Eh+)\b', re.IGNORECASE)


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
        # Strip noise artifacts before translation; broadcast keeps the raw text.
        clean = _STT_NOISE.sub('', text).strip()
        if not clean:
            return
        if self._translation:
            await self._translation.translate_fragment(clean)
        if self._sentence_buffer:
            parts = _SENTENCE_SPLIT.split(clean)
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
