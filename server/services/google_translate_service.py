import asyncio
import logging
import os
from collections import deque
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
SEPARATOR = "\n\n"
CONTEXT_WINDOW = 2  # previous sentences sent alongside current for disambiguation


class GoogleTranslateService:
    """Translates sentences via Google Cloud Translation v2.

    Two display tracks:

    Fast track — translate_fragment() translates each Deepgram final immediately,
    emitting interim_translation so the congregation sees something within ~400ms.
    No context, no correction. Cancelled if a newer fragment arrives first.

    Accurate track — translate() is called when SentenceBuffer flushes a full
    sentence. Uses two additional accuracy techniques in a single API call:

    1. Context injection — prepends up to CONTEXT_WINDOW previous Spanish
       sentences so Google can disambiguate pronouns and theological terms.

    2. Forward-only dual-pass correction — also retranslates the previous
       sentence with the new sentence as trailing context. If the result
       differs, a 'correction' event silently updates that committed line.
    """

    def __init__(
        self,
        on_translation: Callable[[str, str, int], Awaitable[None]],
        on_correction: Callable[[int, str], Awaitable[None]],
        on_interim_translation: Callable[[str], Awaitable[None]],
    ):
        self._on_translation = on_translation
        self._on_correction = on_correction
        self._on_interim_translation = on_interim_translation
        self._api_key = os.environ["GOOGLE_TRANSLATE_API_KEY"]
        self._context: deque[tuple[str, str, int]] = deque(maxlen=CONTEXT_WINDOW)
        self._active_task: asyncio.Task | None = None
        self._fragment_task: asyncio.Task | None = None

    async def translate_fragment(self, spanish: str):
        """Fast track: translate a single fragment immediately, no context.
        Cancels any in-flight fragment request to prevent stale out-of-order updates."""
        if self._fragment_task and not self._fragment_task.done():
            self._fragment_task.cancel()
        self._fragment_task = asyncio.create_task(self._do_translate_fragment(spanish))

    async def _do_translate_fragment(self, spanish: str):
        try:
            english = await self._call_api(spanish)
            await self._on_interim_translation(english)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[google_translate] Fragment error for '%s': %s", spanish[:40], e)

    async def translate(self, spanish: str, ts: int):
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
        self._active_task = asyncio.create_task(self._do_translate(spanish, ts))

    async def _do_translate(self, spanish: str, ts: int):
        context = list(self._context)  # snapshot before any await

        # Combine context + current sentence, separated by double newline
        all_spanish = [s for s, _, _ in context] + [spanish]
        combined = SEPARATOR.join(all_spanish)

        try:
            translated = await self._call_api(combined)
            parts = [p.strip() for p in translated.split(SEPARATOR) if p.strip()]

            # Current translation is always the last part
            current_en = parts[-1] if parts else translated.strip()

            # Dual-pass correction: update immediately previous segment
            if context and len(parts) >= 2:
                _, prev_en_orig, prev_ts = context[-1]
                corrected_en = parts[-2]
                if corrected_en != prev_en_orig:
                    await self._on_correction(prev_ts, corrected_en)

            self._context.append((spanish, current_en, ts))
            await self._on_translation(spanish, current_en, ts)

        except asyncio.CancelledError:
            logger.debug("[google_translate] Task cancelled for: %s", spanish[:40])
        except Exception as e:
            logger.error("[google_translate] Error translating '%s': %s", spanish[:40], e)

    async def _call_api(self, text: str) -> str:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                TRANSLATE_URL,
                params={"key": self._api_key},
                json={"q": text, "source": "es", "target": "en", "format": "text"},
                timeout=10.0,
            )
            resp.raise_for_status()
            return resp.json()["data"]["translations"][0]["translatedText"]
