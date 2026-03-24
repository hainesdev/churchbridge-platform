import asyncio
import logging
from typing import Callable, Awaitable

from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType
from deepgram.listen.v1.types import ListenV1Results

logger = logging.getLogger(__name__)


class DeepgramSession:
    """Manages a single Deepgram streaming connection for one church service.

    Calls on_interim with partial transcripts (for display preview).
    Calls on_final with completed utterances (triggers translation).

    Architecture: the connection lives inside a background asyncio Task. The
    async context manager handles the WebSocket lifecycle — we must NOT call
    start_listening() after __aenter__, as __aenter__ already starts the
    internal thread (calling it again raises "threads can only be started once").
    We hold the context manager open with a stop_event until stop() is called.
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
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._client = AsyncDeepgramClient()

    async def start(self, glossary: dict[str, int], sample_rate: int = 16000):
        self._stop_event = asyncio.Event()
        ready = asyncio.Event()
        keywords = [f"{term}:{boost}" for term, boost in glossary.items()]

        self._task = asyncio.create_task(
            self._run(keywords, sample_rate, ready)
        )
        # Wait until connection is established (or failed) before returning
        await ready.wait()
        logger.info("[deepgram] Session started for church %s at %dHz", self._church_id, sample_rate)

    async def _run(self, keywords: list[str], sample_rate: int, ready: asyncio.Event):
        """Background task: holds the async context manager open until stop() fires."""
        try:
            async with self._client.listen.v1.connect(
                model="nova-2",
                language="es",
                encoding="linear16",
                sample_rate=str(sample_rate),
                channels="1",
                interim_results="true",
                utterance_end_ms="1000",
                vad_events="true",
                smart_format="true",
                keywords=keywords,
            ) as conn:
                self._connection = conn
                conn.on(EventType.MESSAGE, self._handle_message)
                conn.on(EventType.ERROR, self._handle_error)
                conn.on(EventType.CLOSE, self._handle_close)
                ready.set()
                # Stay open until stop() sets the event
                await self._stop_event.wait()
        except Exception as e:
            logger.error("[deepgram] Connection error for church %s: %s", self._church_id, e)
            ready.set()  # unblock start() even on failure
        finally:
            self._connection = None

    async def send(self, pcm16_bytes: bytes):
        if self._connection:
            await self._connection.send_media(pcm16_bytes)

    async def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=3.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        self._task = None
        logger.info("[deepgram] Session closed for church %s", self._church_id)

    async def _handle_message(self, result):
        if not isinstance(result, ListenV1Results):
            return
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

    async def _handle_error(self, error):
        logger.error("[deepgram] Error: %s", error)

    async def _handle_close(self, close):
        logger.info("[deepgram] Connection closed for church %s", self._church_id)
