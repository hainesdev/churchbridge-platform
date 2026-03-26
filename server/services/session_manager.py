import logging
import time
from fastapi import WebSocket

from server.db.glossary import get_glossary
from server.db.sessions import (
    create_service_session,
    close_service_session,
    append_segment,
)
from server.services.audio_utils import resample_float32_to_pcm16, base64_to_float32_bytes
from server.services.deepgram_session import DeepgramSession
from server.services.google_translate_service import GoogleTranslateService
from server.services.sentence_buffer import SentenceBuffer
from server.services.broadcaster import Broadcaster

logger = logging.getLogger(__name__)


class ServiceSession:
    """One active session per church_id. Owns the Deepgram connection,
    SentenceBuffer, GoogleTranslateService, and the admin WebSocket."""

    def __init__(self, church_id: str, ws: WebSocket, broadcaster: Broadcaster):
        self._church_id = church_id
        self._ws = ws
        self._broadcaster = broadcaster
        self._sample_rate = 48000
        self._db_session_id: int | None = None
        self._deepgram: DeepgramSession | None = None
        self._sentence_buffer: SentenceBuffer | None = None
        self._translation: GoogleTranslateService | None = None

    async def start(self, sample_rate: int, sermon_topic: str = ""):
        self._sample_rate = sample_rate
        self._db_session_id = await create_service_session(self._church_id)

        glossary = await get_glossary(self._church_id)

        self._sentence_buffer = SentenceBuffer(on_sentence=self._on_sentence)

        self._translation = GoogleTranslateService(
            on_translation=self._on_translation,
            on_correction=self._on_correction,
            on_interim_translation=self._on_interim_translation,
        )

        self._deepgram = DeepgramSession(
            church_id=self._church_id,
            on_interim=self._on_interim,
            on_final=self._on_final,
        )
        await self._deepgram.start(glossary=glossary, sample_rate=16000)

        await self._send({"type": "session_started", "sessionId": self._db_session_id})
        logger.info(
            "[session] Started for church %s (db_id=%s, topic=%r)",
            self._church_id, self._db_session_id, sermon_topic or "(none)",
        )

    async def ingest(self, audio_b64: str):
        """Receive a base64 Float32 chunk from the browser, resample, forward to Deepgram."""
        import numpy as np
        raw = base64_to_float32_bytes(audio_b64)
        self._ingest_count = getattr(self, '_ingest_count', 0) + 1
        if self._ingest_count == 10:
            samples = np.frombuffer(raw, dtype=np.float32)
            rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0
            if rms < 0.0005:
                logger.warning(
                    "[session:%s] Audio appears SILENT after 10 chunks (RMS=%.5f) — "
                    "mic may not be captured",
                    self._church_id, rms,
                )
            else:
                logger.info(
                    "[session:%s] Audio amplitude OK (RMS=%.4f)", self._church_id, rms
                )
        pcm16 = resample_float32_to_pcm16(raw, self._sample_rate, dst_rate=16000)
        if self._deepgram:
            await self._deepgram.send(pcm16)

    async def close(self):
        if self._sentence_buffer:
            await self._sentence_buffer.stop()  # flush any remaining text
        if self._deepgram:
            await self._deepgram.stop()
        if self._db_session_id:
            await close_service_session(self._db_session_id)
        logger.info("[session] Closed for church %s", self._church_id)

    # --- Deepgram callbacks ---

    async def _on_interim(self, text: str):
        await self._broadcast({"type": "interim", "text": text, "ts": _now()})

    async def _on_final(self, text: str):
        logger.info("[session:%s] STT final: %s", self._church_id, text)
        await self._broadcast({"type": "stt_final", "text": text, "ts": _now()})
        if self._translation:
            await self._translation.translate_fragment(text)  # fast track: show immediately
        if self._sentence_buffer:
            await self._sentence_buffer.add(text)             # accurate track: accumulate

    # --- Sentence buffer callback ---

    async def _on_sentence(self, text: str):
        ts = _now()
        logger.info("[session:%s] Sentence flushed: %s", self._church_id, text)
        await self._broadcast({"type": "final_spanish", "text": text, "ts": ts})
        if self._translation:
            await self._translation.translate(text, ts)

    # --- Translation callbacks ---

    async def _on_translation(self, spanish: str, english: str, ts: int):
        logger.info("[session:%s] Translation: %s -> %s", self._church_id, spanish[:60], english[:60])
        await self._broadcast({
            "type": "translation",
            "spanish": spanish,
            "english": english,
            "ts": ts,
        })
        if self._db_session_id:
            await append_segment(self._db_session_id, spanish, english)

    async def _on_interim_translation(self, text: str):
        await self._broadcast({"type": "interim_translation", "text": text, "ts": _now()})

    async def _on_correction(self, ts: int, english: str):
        """Silently update a previously broadcast translation with better context."""
        await self._broadcast({"type": "correction", "ts": ts, "english": english})

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
