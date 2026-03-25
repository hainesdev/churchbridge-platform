import asyncio
import logging
import os
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"


class GoogleTranslateService:
    """Translates Deepgram final transcripts via Google Cloud Translation v2.

    Each Deepgram is_final event triggers a non-streaming translation request.
    If a new segment arrives before the previous request completes, the in-flight
    request is cancelled (same pattern as the old TranslationService).
    """

    def __init__(
        self,
        on_complete: Callable[[str, str], Awaitable[None]],
    ):
        self._on_complete = on_complete
        self._api_key = os.environ["GOOGLE_TRANSLATE_API_KEY"]
        self._active_task: asyncio.Task | None = None

    async def translate(self, spanish_text: str):
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
        self._active_task = asyncio.create_task(self._do_translate(spanish_text))

    async def _do_translate(self, text: str):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    TRANSLATE_URL,
                    params={"key": self._api_key},
                    json={
                        "q": text,
                        "source": "es",
                        "target": "en",
                        "format": "text",
                    },
                    timeout=10.0,
                )
                resp.raise_for_status()
                english = resp.json()["data"]["translations"][0]["translatedText"]
                await self._on_complete(text, english)
        except asyncio.CancelledError:
            logger.debug("[google_translate] Task cancelled for: %s", text[:40])
        except Exception as e:
            logger.error("[google_translate] Error translating '%s': %s", text[:40], e)
