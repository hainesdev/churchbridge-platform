import logging
import time
from fastapi import WebSocket

from server.db.church_terms import load_church_terms
from server.db.sessions import (
    create_service_session,
    close_service_session,
    append_segment,
)
from server.services.audio_utils import resample_float32_to_pcm16, base64_to_float32_bytes
from server.services.deepl_voice_session import DeepLVoiceSession
from server.services.deepl_glossary import get_or_create_glossary
from server.services.broadcaster import Broadcaster

logger = logging.getLogger(__name__)


class ServiceSession:
    """One active session per church_id. Owns the DeepL Voice connection and admin WebSocket."""

    def __init__(self, church_id: str, ws: WebSocket, broadcaster: Broadcaster):
        self._church_id = church_id
        self._ws = ws
        self._broadcaster = broadcaster
        self._sample_rate = 48000
        self._db_session_id: int | None = None
        self._deepl: DeepLVoiceSession | None = None

    async def start(self, sample_rate: int, sermon_topic: str = ""):
        self._sample_rate = sample_rate
        self._db_session_id = await create_service_session(self._church_id)

        church_terms = await load_church_terms(self._church_id)
        glossary_id = await get_or_create_glossary(self._church_id, church_terms)

        self._deepl = DeepLVoiceSession(
            church_id=self._church_id,
            glossary_id=glossary_id,
            on_interim_spanish=self._on_interim_spanish,
            on_final_spanish=self._on_final_spanish,
            on_interim_english=self._on_interim_english,
            on_final_english=self._on_final_english,
        )
        await self._deepl.start()

        await self._send({"type": "session_started", "sessionId": self._db_session_id})
        logger.info(
            "[session] Started for church %s (db_id=%s, topic=%r)",
            self._church_id, self._db_session_id, sermon_topic or "(none)",
        )

    async def ingest(self, audio_b64: str):
        """Receive a base64 Float32 chunk from the browser, resample to 16kHz PCM16, forward to DeepL."""
        raw = base64_to_float32_bytes(audio_b64)
        pcm16 = resample_float32_to_pcm16(raw, self._sample_rate, dst_rate=16000)
        if self._deepl:
            await self._deepl.send(pcm16)

    async def close(self):
        if self._deepl:
            await self._deepl.stop()
        if self._db_session_id:
            await close_service_session(self._db_session_id)
        logger.info("[session] Closed for church %s", self._church_id)

    # --- DeepL Voice callbacks ---

    async def _on_interim_spanish(self, text: str):
        await self._broadcast({"type": "interim", "text": text, "ts": _now()})

    async def _on_final_spanish(self, text: str):
        # Broadcast for any display that wants to show committed Spanish
        await self._broadcast({"type": "final_spanish", "text": text, "ts": _now()})

    async def _on_interim_english(self, text: str):
        # Full tentative phrase — listeners replace (not append) their partial display
        await self._broadcast({"type": "interim_translation", "text": text, "ts": _now()})

    async def _on_final_english(self, spanish: str, english: str):
        await self._broadcast({
            "type": "translation",
            "spanish": spanish,
            "english": english,
            "ts": _now(),
        })
        if self._db_session_id:
            await append_segment(self._db_session_id, spanish, english)

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
