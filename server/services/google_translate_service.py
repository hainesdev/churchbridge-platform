import asyncio
import html
import logging
import os
import re
from collections import deque
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)

# TODO (item 5): Migrate to Google Cloud Translation v3 Advanced for:
#   - Theological glossary support (force-map terms like Espíritu Santo → Holy Spirit,
#     gracia → grace, redención → redemption, hermanos → brothers/sisters, etc.)
#   - Adaptive MT with congregation-specific example sentence pairs to bias register
#   - Explicit NMT model selection instead of v2's implicit default
#   Client: pip install google-cloud-translate; use TranslationServiceClient
#   Ref: https://cloud.google.com/translate/docs/advanced/glossary

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
CONTEXT_WINDOW = 2  # previous sentences sent alongside current for disambiguation
_P_TAG = re.compile(r'<p>(.*?)</p>', re.DOTALL)


def _wrap(segments: list[str]) -> str:
    """Wrap each segment in <p> tags so Google preserves boundaries regardless
    of whitespace normalization. Input is HTML-escaped to protect the structure."""
    return "".join(f"<p>{html.escape(s)}</p>" for s in segments)


def _unwrap(response: str) -> list[str]:
    """Extract text from Google's <p>...</p> blocks, unescaping HTML entities."""
    return [html.unescape(m).strip() for m in _P_TAG.findall(response)]


class GoogleTranslateService:
    """Translates sentences via Google Cloud Translation v2.

    Two display tracks:

    Fast track — translate_fragment() translates each Deepgram final immediately,
    emitting interim_translation so the congregation sees something within ~400ms.
    All fragments accumulated since the last sentence flush are sent as leading
    context so Google can disambiguate long sentences; only the English for the
    *current* fragment is broadcast. Cancelled when a newer fragment arrives or
    when a full sentence is translated.

    Accurate track — translate() is called when SentenceBuffer flushes a full
    sentence. Uses two additional accuracy techniques in a single API call:

    1. Context injection — prepends up to CONTEXT_WINDOW previous Spanish
       sentences so Google can disambiguate pronouns and theological terms.

    2. Forward-only dual-pass correction — also retranslates the previous
       sentence with the new sentence as trailing context. If the result
       differs, a 'correction' event silently updates that committed line.

    Segments are sent as HTML <p> blocks (format=html) so Google preserves
    boundaries exactly, ensuring the part count always matches the input count.
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
        # All Deepgram finals accumulated within the current sentence window
        # (before SentenceBuffer flushes). Grows with each fragment; reset on
        # translate(). Gives Google the full in-progress sentence as leading
        # context so later fragments benefit from the same disambiguation as
        # earlier ones — not just the immediately prior fragment.
        self._fragment_context: list[str] = []
        self._http = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        """Release the shared HTTP client. Call from ServiceSession.close()."""
        await self._http.aclose()

    async def translate_fragment(self, spanish: str):
        """Fast track: translate one STT final. Uses all prior fragments in the
        current sentence as leading context; only the current fragment's English
        is emitted to the display."""
        if self._fragment_task and not self._fragment_task.done():
            self._fragment_task.cancel()
        # Snapshot and advance context synchronously so cancelled in-flight
        # requests cannot leave _fragment_context stale for the next fragment.
        prev_context = list(self._fragment_context)
        self._fragment_context.append(spanish)
        self._fragment_task = asyncio.create_task(
            self._do_translate_fragment(spanish, prev_context)
        )

    async def _do_translate_fragment(self, spanish: str, prev_context: list[str]):
        my_task = asyncio.current_task()
        try:
            segments = prev_context + [spanish]
            translated = await self._call_api(_wrap(segments))
            # A newer fragment may have replaced _fragment_task; do not emit stale English.
            if my_task is not self._fragment_task:
                return
            parts = _unwrap(translated)
            english = parts[-1] if parts else translated.strip()
            await self._on_interim_translation(english)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[google_translate] Fragment error for '%s': %s", spanish[:40], e)

    async def translate(self, spanish: str, ts: int):
        """Accurate sentence translation; supersedes in-flight fragment interim."""
        if self._fragment_task and not self._fragment_task.done():
            self._fragment_task.cancel()
        self._fragment_task = None
        self._fragment_context = []
        if self._active_task and not self._active_task.done():
            self._active_task.cancel()
        self._active_task = asyncio.create_task(self._do_translate(spanish, ts))

    async def _do_translate(self, spanish: str, ts: int):
        my_task = asyncio.current_task()
        context = list(self._context)  # snapshot before any await

        all_spanish = [s for s, _, _ in context] + [spanish]
        combined = _wrap(all_spanish)

        translated: str | None = None
        for attempt in range(3):
            try:
                translated = await self._call_api(combined)
                break
            except asyncio.CancelledError:
                logger.debug("[google_translate] Task cancelled for: %s", spanish[:40])
                raise
            except Exception as e:
                if attempt == 2:
                    logger.error(
                        "[google_translate] Giving up after 3 attempts for '%s': %s",
                        spanish[:40], e,
                    )
                    return
                wait = 0.5 * (2 ** attempt)
                logger.warning(
                    "[google_translate] Attempt %d failed, retrying in %.1fs: %s",
                    attempt + 1, wait, e,
                )
                await asyncio.sleep(wait)
                if my_task is not self._active_task:  # superseded during backoff
                    return

        if my_task is not self._active_task or translated is None:
            return

        parts = _unwrap(translated)
        current_en = parts[-1] if parts else translated.strip()

        logger.info("[google_translate] Translation: '%s' → '%s'", spanish[:60], current_en[:60])

        # Dual-pass correction: update immediately previous segment
        if context and len(parts) >= 2:
            _, prev_en_orig, prev_ts = context[-1]
            corrected_en = parts[-2]
            if corrected_en != prev_en_orig:
                logger.info(
                    "[google_translate] Dual-pass correction ts=%d:\n  before: %s\n   after: %s",
                    prev_ts, prev_en_orig[:80], corrected_en[:80],
                )
                await self._on_correction(prev_ts, corrected_en)
            else:
                logger.info("[google_translate] Dual-pass checked ts=%d — no change", prev_ts)

        self._context.append((spanish, current_en, ts))
        await self._on_translation(spanish, current_en, ts)

    async def _call_api(self, html_body: str) -> str:
        resp = await self._http.post(
            TRANSLATE_URL,
            params={"key": self._api_key},
            json={"q": html_body, "source": "es", "target": "en", "format": "html"},
        )
        resp.raise_for_status()
        return resp.json()["data"]["translations"][0]["translatedText"]
