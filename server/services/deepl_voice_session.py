import asyncio
import json
import logging
import os
from typing import Callable, Awaitable

import httpx
import websockets

logger = logging.getLogger(__name__)

DEEPL_REST_URL = "https://api.deepl.com"
DEEPL_WS_URL = "wss://api.deepl.com"


class DeepLVoiceSession:
    """Streams PCM16 16kHz mono audio to DeepL Voice API.

    DeepL handles both STT (Spanish) and translation (English) in one service.
    Dispatches four callbacks:
      on_interim_spanish  — tentative source transcript (for live display)
      on_final_spanish    — concluded source segment (for DB logging)
      on_interim_english  — tentative translation (replaces token-by-token streaming)
      on_final_english    — concluded translation (commit to DB + broadcast)
    """

    def __init__(
        self,
        church_id: str,
        glossary_id: str | None,
        on_interim_spanish: Callable[[str], Awaitable[None]],
        on_final_spanish: Callable[[str], Awaitable[None]],
        on_interim_english: Callable[[str], Awaitable[None]],
        on_final_english: Callable[[str, str], Awaitable[None]],
    ):
        self._church_id = church_id
        self._glossary_id = glossary_id
        self._on_interim_spanish = on_interim_spanish
        self._on_final_spanish = on_final_spanish
        self._on_interim_english = on_interim_english
        self._on_final_english = on_final_english

        self._ws: websockets.ClientConnection | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        # Track last concluded Spanish segment for pairing with English final
        self._pending_spanish = ""

    async def start(self):
        api_key = os.environ["DEEPL_API_KEY"]
        token = await self._create_token(api_key)
        ready = asyncio.Event()
        self._task = asyncio.create_task(self._run(token, ready))
        await asyncio.wait_for(ready.wait(), timeout=15.0)
        logger.info("[deepl_voice] Session started for church %s", self._church_id)

    async def _create_token(self, api_key: str) -> str:
        body: dict = {"source_lang": "ES", "target_lang": "EN"}
        if self._glossary_id:
            body["glossary_id"] = self._glossary_id

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{DEEPL_REST_URL}/v3/voice/realtime",
                json=body,
                headers={"Authorization": f"DeepL-Auth-Key {api_key}"},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()["token"]

    async def _run(self, token: str, ready: asyncio.Event):
        url = f"{DEEPL_WS_URL}/v3/voice/realtime/connect?token={token}"
        try:
            async with websockets.connect(url) as ws:
                self._ws = ws
                ready.set()
                async for message in ws:
                    if self._stop_event.is_set():
                        break
                    if isinstance(message, str):
                        await self._handle_message(message)
        except websockets.ConnectionClosed:
            logger.info("[deepl_voice] Connection closed for church %s", self._church_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[deepl_voice] Error for church %s: %s", self._church_id, e)

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")

        if msg_type == "source_transcript_update":
            segments = msg.get("segments", [])
            if not segments:
                return
            last = segments[-1]
            text = last.get("text", "").strip()
            if not text:
                return
            if last.get("type") == "tentative":
                await self._on_interim_spanish(text)
            elif last.get("type") == "concluded":
                self._pending_spanish = text
                await self._on_final_spanish(text)

        elif msg_type == "target_transcript_update":
            segments = msg.get("segments", [])
            if not segments:
                return
            last = segments[-1]
            text = last.get("text", "").strip()
            if not text:
                return
            if last.get("type") == "tentative":
                await self._on_interim_english(text)
            elif last.get("type") == "concluded":
                spanish = self._pending_spanish
                self._pending_spanish = ""
                await self._on_final_english(spanish, text)

    async def send(self, pcm16_bytes: bytes):
        """Forward a PCM16 16kHz mono chunk to DeepL."""
        if self._ws is None:
            return
        try:
            await self._ws.send(pcm16_bytes)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            logger.error("[deepl_voice] Send error: %s", e)

    async def stop(self):
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._ws = None
        logger.info("[deepl_voice] Stopped for church %s", self._church_id)
