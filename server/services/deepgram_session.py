import asyncio
import json
import os
import logging
from typing import Callable, Awaitable
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"
RECONNECT_DELAY_S = 1.5   # initial backoff after a clean drop
RECONNECT_ERROR_S = 3.0   # backoff after an unexpected error


class DeepgramSession:
    """Manages a single Deepgram streaming connection via raw WebSocket.

    Bypasses the Deepgram Python SDK to avoid its internal threading model,
    which raises 'threads can only be started once' when connections are
    created more than once per process.

    Uses the websockets library (pure asyncio) directly against the
    Deepgram v1/listen WebSocket endpoint.

    Timestamp normalization
    -----------------------
    Deepgram's `start` field resets to 0.0 on each new WebSocket connection.
    _stream_offset accumulates the high-water audio end time across reconnects
    so all (audio_start, audio_end) values emitted by on_final are
    monotonically increasing sermon-relative seconds, regardless of drops.
    """

    def __init__(
        self,
        church_id: str,
        on_interim: Callable[[str], Awaitable[None]],
        on_final: Callable[[str, float, float], Awaitable[None]],
        on_utterance_end: Callable[[], Awaitable[None]] | None = None,
    ):
        self._church_id = church_id
        self._on_interim = on_interim
        self._on_final = on_final
        self._on_utterance_end = on_utterance_end
        self._ws = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        # Sermon-relative timestamp normalization
        self._stream_offset: float = 0.0
        # High-water mark within the CURRENT websocket stream only.
        # This must stay stream-relative so reconnect offset accumulation does
        # not double-count prior reconnects.
        self._last_stream_audio_end: float = 0.0

    async def start(self, glossary: dict[str, int], sample_rate: int = 16000):
        self._api_key = os.environ["DEEPGRAM_API_KEY"]  # fail fast at session start
        self._stop_event = asyncio.Event()
        ready: asyncio.Event = asyncio.Event()

        self._task = asyncio.create_task(
            self._run(glossary, sample_rate, ready)
        )
        await ready.wait()

        if self._ws is None:
            raise RuntimeError(f"[deepgram] Failed to connect for church {self._church_id}")

        logger.info("[deepgram] Session started for church %s at %dHz", self._church_id, sample_rate)

    async def _run(self, glossary: dict[str, int], sample_rate: int, ready: asyncio.Event):
        params: list[tuple[str, str]] = [
            ("model",            "nova-3"),
            ("language",         "es"),
            ("encoding",         "linear16"),
            ("sample_rate",      str(sample_rate)),
            ("channels",         "1"),
            ("interim_results",  "true"),
            ("utterance_end_ms", "2000"),
            ("vad_events",       "true"),
            ("smart_format",     "true"),
            ("punctuate",        "true"),
        ]
        # nova-3 uses keyterms (term only, no boost value); keywords is deprecated
        for term in glossary:
            params.append(("keyterms", term))

        url = f"{DEEPGRAM_WS_URL}?{urlencode(params)}"
        first_connect = True

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(
                    url,
                    additional_headers={"Authorization": f"Token {self._api_key}"},
                    ping_interval=10,
                    ping_timeout=5,
                ) as ws:
                    self._ws = ws
                    if first_connect:
                        ready.set()
                        first_connect = False
                    logger.info(
                        "[deepgram] Connected for church %s (stream_offset=%.1fs)",
                        self._church_id, self._stream_offset,
                    )

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        await self._handle_raw(raw)

            except websockets.ConnectionClosedOK:
                if self._stop_event.is_set():
                    break
                logger.info(
                    "[deepgram] Connection dropped for church %s — reconnecting (offset was %.1fs)",
                    self._church_id, self._stream_offset,
                )
                self._stream_offset += self._last_stream_audio_end
                self._last_stream_audio_end = 0.0
                self._ws = None
                await asyncio.sleep(RECONNECT_DELAY_S)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.error(
                    "[deepgram] Connection error for church %s: %s — retrying in %.1fs",
                    self._church_id, e, RECONNECT_ERROR_S,
                )
                self._stream_offset += self._last_stream_audio_end
                self._last_stream_audio_end = 0.0
                self._ws = None
                await asyncio.sleep(RECONNECT_ERROR_S)

        self._ws = None
        if first_connect:
            ready.set()  # unblock start() if we never successfully connected

    async def send(self, pcm16_bytes: bytes):
        if self._ws:
            try:
                await self._ws.send(pcm16_bytes)
            except websockets.ConnectionClosed:
                logger.warning("[deepgram] Send on closed socket for church %s", self._church_id)

    async def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("[deepgram] Session closed for church %s", self._church_id)

    async def _handle_raw(self, raw: str | bytes):
        if isinstance(raw, bytes):
            return  # Deepgram sends text JSON; ignore binary frames
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        if msg_type == "Results":
            await self._handle_transcript(msg)
        elif msg_type == "Metadata":
            logger.debug("[deepgram] Metadata: %s", msg)
        elif msg_type == "Error":
            logger.error("[deepgram] Server error: %s", msg)
        elif msg_type == "UtteranceEnd":
            # Deepgram's VAD fired: speaker stopped after utterance_end_ms of silence.
            # This is a more reliable "end of thought" signal than our fallback timer.
            if self._on_utterance_end:
                await self._on_utterance_end()
        elif msg_type == "SpeechStarted":
            pass

    async def _handle_transcript(self, msg: dict):
        try:
            channel = msg["channel"]
            alt = channel["alternatives"][0]
            text = alt.get("transcript", "").strip()
            if not text:
                return

            # Normalize Deepgram's stream-relative timestamps to sermon-relative seconds.
            # _stream_offset accumulates the total audio duration of previous connections
            # so values remain monotonically increasing across reconnects.
            raw_start: float = msg.get("start", 0.0)
            duration: float = msg.get("duration", 0.0)
            raw_end = raw_start + duration
            audio_start = raw_start + self._stream_offset
            audio_end = raw_end + self._stream_offset
            self._last_stream_audio_end = max(self._last_stream_audio_end, raw_end)

            is_final = msg.get("is_final", False)
            if is_final:
                logger.debug("[deepgram] FINAL: %s", text)
                await self._on_final(text, audio_start, audio_end)
            else:
                await self._on_interim(text)
        except (KeyError, IndexError) as e:
            logger.error("[deepgram] Result parse error: %s — msg: %s", e, msg)
