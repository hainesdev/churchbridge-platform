import asyncio
import json
import os
import logging
from typing import Callable, Awaitable
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class DeepgramSession:
    """Manages a single Deepgram streaming connection via raw WebSocket.

    Bypasses the Deepgram Python SDK to avoid its internal threading model,
    which raises 'threads can only be started once' when connections are
    created more than once per process.

    Uses the websockets library (pure asyncio) directly against the
    Deepgram v1/listen WebSocket endpoint.
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
        self._ws = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    async def start(self, glossary: dict[str, int], sample_rate: int = 16000):
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
            ("model",            "nova-2"),
            ("language",         "es"),
            ("encoding",         "linear16"),
            ("sample_rate",      str(sample_rate)),
            ("channels",         "1"),
            ("interim_results",  "true"),
            ("utterance_end_ms", "1000"),
            ("vad_events",       "true"),
            ("smart_format",     "true"),
        ]
        # Deepgram accepts repeated ?keywords=term:boost params
        for term, boost in glossary.items():
            params.append(("keywords", f"{term}:{boost}"))

        url = f"{DEEPGRAM_WS_URL}?{urlencode(params)}"
        api_key = os.environ["DEEPGRAM_API_KEY"]

        try:
            async with websockets.connect(
                url,
                additional_headers={"Authorization": f"Token {api_key}"},
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                self._ws = ws
                ready.set()
                logger.debug("[deepgram] WebSocket connected for church %s", self._church_id)

                async for raw in ws:
                    if self._stop_event.is_set():
                        break
                    await self._handle_raw(raw)

        except websockets.ConnectionClosedOK:
            logger.info("[deepgram] Connection closed cleanly for church %s", self._church_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[deepgram] Connection error for church %s: %s", self._church_id, e)
        finally:
            self._ws = None
            ready.set()  # unblock start() if we failed before setting it

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
        elif msg_type in ("UtteranceEnd", "SpeechStarted"):
            pass  # VAD events — no action needed here

    async def _handle_transcript(self, msg: dict):
        try:
            channel = msg["channel"]
            alt = channel["alternatives"][0]
            text = alt.get("transcript", "").strip()
            if not text:
                return

            is_final = msg.get("is_final", False)
            if is_final:
                logger.debug("[deepgram] FINAL: %s", text)
                await self._on_final(text)
            else:
                await self._on_interim(text)
        except (KeyError, IndexError) as e:
            logger.error("[deepgram] Result parse error: %s — msg: %s", e, msg)
