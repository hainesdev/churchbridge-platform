import asyncio
import logging
import time
from fastapi import WebSocket

from server.db.glossary import get_glossary
from server.db.church_terms import load_church_terms
from server.db.sessions import (
    create_service_session,
    close_service_session,
    append_segment,
)
from server.services.audio_utils import resample_float32_to_pcm16, base64_to_float32_bytes
from server.services.deepgram_session import DeepgramSession
from server.services.translation_service import TranslationService
from server.services.broadcaster import Broadcaster

logger = logging.getLogger(__name__)


class ServiceSession:
    """One active session per church_id. Owns the Deepgram connection,
    TranslationService, and the admin WebSocket."""

    def __init__(self, church_id: str, ws: WebSocket, broadcaster: Broadcaster):
        self._church_id = church_id
        self._ws = ws
        self._broadcaster = broadcaster
        self._sample_rate = 48000
        self._db_session_id: int | None = None
        self._deepgram: DeepgramSession | None = None
        self._translation: TranslationService | None = None

    async def start(self, sample_rate: int):
        self._sample_rate = sample_rate
        self._db_session_id = await create_service_session(self._church_id)

        glossary = await get_glossary(self._church_id)
        church_terms = await load_church_terms(self._church_id)

        self._translation = TranslationService(
            church_id=self._church_id,
            church_terms=church_terms,
            on_token=self._on_token,
            on_complete=self._on_complete,
        )

        self._deepgram = DeepgramSession(
            church_id=self._church_id,
            on_interim=self._on_interim,
            on_final=self._on_final,
        )
        await self._deepgram.start(glossary=glossary, sample_rate=16000)

        await self._send({"type": "session_started", "sessionId": self._db_session_id})
        logger.info("[session] Started for church %s (db_id=%s)", self._church_id, self._db_session_id)

    async def ingest(self, audio_b64: str):
        """Receive a base64 Float32 chunk from the browser, resample, forward to Deepgram."""
        raw = base64_to_float32_bytes(audio_b64)
        pcm16 = resample_float32_to_pcm16(raw, self._sample_rate, dst_rate=16000)
        if self._deepgram:
            await self._deepgram.send(pcm16)

    async def close(self):
        if self._deepgram:
            await self._deepgram.stop()
        if self._db_session_id:
            await close_service_session(self._db_session_id)
        logger.info("[session] Closed for church %s", self._church_id)

    # --- Deepgram callbacks ---

    async def _on_interim(self, text: str):
        """Broadcast interim transcript for live preview on the display."""
        await self._broadcast({"type": "interim", "text": text, "ts": _now()})

    async def _on_final(self, text: str):
        """Final transcript received — trigger translation."""
        await self._broadcast({"type": "final_spanish", "text": text, "ts": _now()})
        if self._translation:
            await self._translation.translate(text)

    # --- Translation callbacks ---

    async def _on_token(self, token: str):
        """Stream each LLM token directly to displays."""
        await self._broadcast({"type": "token", "text": token, "ts": _now()})

    async def _on_complete(self, spanish: str, english: str):
        """Full translation done — broadcast and persist."""
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
            import json
            await self._ws.send_json(msg)
        except Exception:
            pass


class SessionManager:
    """Tracks one active ServiceSession per church_id."""

    def __init__(self, broadcaster: Broadcaster):
        self._broadcaster = broadcaster
        self._sessions: dict[str, ServiceSession] = {}

    async def create(self, church_id: str, ws: WebSocket, sample_rate: int) -> ServiceSession:
        if church_id in self._sessions:
            await self._sessions[church_id].close()

        session = ServiceSession(church_id, ws, self._broadcaster)
        self._sessions[church_id] = session
        await session.start(sample_rate)
        return session

    async def remove(self, church_id: str):
        session = self._sessions.pop(church_id, None)
        if session:
            await session.close()

    def get(self, church_id: str) -> ServiceSession | None:
        return self._sessions.get(church_id)


def _now() -> int:
    return int(time.time() * 1000)
