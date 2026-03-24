import asyncio
import logging
from typing import Callable, Awaitable

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveTranscriptionEvents,
    LiveOptions,
)

logger = logging.getLogger(__name__)


class DeepgramSession:
    """Manages a single Deepgram streaming connection for one church service.

    Calls on_interim with partial transcripts (for display preview).
    Calls on_final with completed utterances (triggers translation).
    """

    def __init__(
        self,
        church_id: str,
        on_interim: Callable[[str], Awaitable[None]],
        on_final: Callable[[str], Awaitable[None]],
    ):
        self._church_id = church_id
        self._on_interim = on_interim
        self._on_final = on_final
        self._connection = None
        self._client = DeepgramClient()

    async def start(self, glossary: dict[str, int], sample_rate: int = 16000):
        keywords = [f"{term}:{boost}" for term, boost in glossary.items()]

        options = LiveOptions(
            model="nova-2",
            language="es",
            encoding="linear16",
            sample_rate=sample_rate,
            channels=1,
            interim_results=True,
            utterance_end_ms="1000",
            vad_events=True,
            smart_format=True,
            keywords=keywords,
        )

        self._connection = self._client.listen.asyncwebsocket.v("1")

        self._connection.on(LiveTranscriptionEvents.Transcript, self._handle_transcript)
        self._connection.on(LiveTranscriptionEvents.Error, self._handle_error)
        self._connection.on(LiveTranscriptionEvents.Close, self._handle_close)

        started = await self._connection.start(options)
        if not started:
            raise RuntimeError(f"[deepgram] Failed to start connection for church {self._church_id}")

        logger.info("[deepgram] Session started for church %s at %dHz", self._church_id, sample_rate)

    async def send(self, pcm16_bytes: bytes):
        if self._connection:
            await self._connection.send(pcm16_bytes)

    async def stop(self):
        if self._connection:
            await self._connection.finish()
            self._connection = None
            logger.info("[deepgram] Session closed for church %s", self._church_id)

    async def _handle_transcript(self, result, **kwargs):
        try:
            alt = result.channel.alternatives[0]
            text = alt.transcript.strip()
            if not text:
                return

            if result.is_final:
                logger.debug("[deepgram] FINAL: %s", text)
                await self._on_final(text)
            else:
                await self._on_interim(text)
        except Exception as e:
            logger.error("[deepgram] Transcript handler error: %s", e)

    async def _handle_error(self, error, **kwargs):
        logger.error("[deepgram] Error: %s", error)

    async def _handle_close(self, close, **kwargs):
        logger.info("[deepgram] Connection closed for church %s", self._church_id)
