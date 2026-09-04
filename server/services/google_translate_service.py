import asyncio
import html
import logging
import os
import re
from collections import deque
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

# Suppress httpx's own request/response logging - it includes the full URL
# with query-string parameters, which would expose the API key.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# TODO (item 5): Migrate to Google Cloud Translation v3 Advanced for:
#   - Theological glossary support (force-map terms like Espiritu Santo -> Holy Spirit,
#     gracia -> grace, redencion -> redemption, hermanos -> brothers/sisters, etc.)
#   - Adaptive MT with congregation-specific example sentence pairs to bias register
#   - Explicit NMT model selection instead of v2's implicit default
#   Client: pip install google-cloud-translate; use TranslationServiceClient
#   Ref: https://cloud.google.com/translate/docs/advanced/glossary

TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"
CONTEXT_WINDOW = 2  # previous sentences sent alongside current for disambiguation
_P_TAG = re.compile(r"<p>(.*?)</p>", re.DOTALL)
INTERIM_PREVIEW_DEBOUNCE_S = 0.18
INTERIM_PREVIEW_MIN_CHARS = 8


def _wrap(segments: list[str]) -> str:
    """Wrap each segment in <p> tags so Google preserves boundaries regardless
    of whitespace normalization. Input is HTML-escaped to protect the structure."""
    return "".join(f"<p>{html.escape(s)}</p>" for s in segments)


def _unwrap(response: str) -> list[str]:
    """Extract text from Google's <p>...</p> blocks, unescaping HTML entities."""
    return [html.unescape(m).strip() for m in _P_TAG.findall(response)]


def _join_preview_parts(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip()).strip()


class GoogleTranslateService:
    """Translates sentences via Google Cloud Translation v2.

    Two display tracks:

    Fast track - translate_fragment() translates each STT final immediately,
    emitting interim_translation so the congregation sees something within ~400ms.
    All fragments accumulated since the last sentence flush are sent as leading
    context so Google can disambiguate long sentences; only the English for the
    current fragment is broadcast. Cancelled when a newer fragment arrives or
    when a full sentence is translated.

    Accurate track - translate() is called when SentenceBuffer flushes a full
    sentence. Uses two additional accuracy techniques in a single API call:

    1. Context injection - prepends up to CONTEXT_WINDOW previous Spanish
       sentences so Google can disambiguate pronouns and theological terms.

    2. Forward-only dual-pass correction - also retranslates the previous
       sentence with the new sentence as trailing context. If the result
       differs, a 'correction' event silently updates that committed line.

    Segments are sent as HTML <p> blocks (format=html) so Google preserves
    boundaries exactly, ensuring the part count always matches the input count.
    """

    def __init__(
        self,
        on_translation: Callable[[str, str, int], Awaitable[None]],
        on_correction: Callable[[int, str], Awaitable[None]],
        on_interim_translation: Callable[[str, str, bool, str, str], Awaitable[None]],
    ):
        self._on_translation = on_translation
        self._on_correction = on_correction
        self._on_interim_translation = on_interim_translation
        self._api_key = os.environ["GOOGLE_TRANSLATE_API_KEY"]
        self._context: deque[tuple[str, str, int]] = deque(maxlen=CONTEXT_WINDOW)
        self._active_task: asyncio.Task | None = None
        self._fragment_task: asyncio.Task | None = None
        self._sentence_tasks: list[asyncio.Task] = []
        # Accurate sentence translations must commit in order so later sentences
        # cannot cancel or overtake earlier committed captions.
        self._sentence_lock = asyncio.Lock()
        # All STT finals accumulated within the current sentence window
        # (before SentenceBuffer flushes). Grows with each fragment; reset on
        # translate(). Gives Google the full in-progress sentence as leading
        # context so later fragments benefit from the same disambiguation as
        # earlier ones - not just the immediately prior fragment.
        self._fragment_context: list[str] = []
        self._last_preview_spanish = ""
        self._http = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        """Drain queued work, then release the shared HTTP client."""
        await self.wait_for_idle()
        await self._http.aclose()

    async def wait_for_idle(self):
        """Wait for any in-flight fragment or sentence translation work to finish."""
        tasks: list[asyncio.Task] = []
        if self._fragment_task and not self._fragment_task.done():
            tasks.append(self._fragment_task)
        sentence_tasks = getattr(self, "_sentence_tasks", [])
        sentence_tasks = [t for t in sentence_tasks if not t.done()]
        self._sentence_tasks = sentence_tasks
        tasks.extend(sentence_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._sentence_tasks.clear()

    async def translate_fragment(self, spanish: str):
        """Fast track: translate one STT final. Uses all prior fragments in the
        current sentence as leading context; the full stable prefix is emitted
        so the live display preserves context while new audio keeps arriving."""
        if self._fragment_task and not self._fragment_task.done():
            self._fragment_task.cancel()
        self._last_preview_spanish = ""
        prev_context = list(self._fragment_context)
        self._fragment_context.append(spanish)
        self._fragment_task = asyncio.create_task(
            self._do_translate_fragment(spanish, prev_context)
        )

    async def translate_interim(self, spanish: str):
        """Preview track: translate the latest STT interim as a replace-in-place
        live preview for the current sentence hypothesis. The emitted preview
        preserves the translated stable prefix from prior STT finals and keeps
        the latest in-flight tail separate for the UI."""
        spanish = spanish.strip()
        if len(spanish) < INTERIM_PREVIEW_MIN_CHARS:
            return
        if spanish == self._last_preview_spanish:
            return
        self._last_preview_spanish = spanish
        if self._fragment_task and not self._fragment_task.done():
            self._fragment_task.cancel()
        prev_context = list(self._fragment_context)
        self._fragment_task = asyncio.create_task(
            self._do_translate_interim(spanish, prev_context)
        )

    async def _do_translate_fragment(self, spanish: str, prev_context: list[str]):
        my_task = asyncio.current_task()
        try:
            segments = prev_context + [spanish]
            translated = await self._call_api(_wrap(segments))
            if my_task is not self._fragment_task:
                return
            parts = _unwrap(translated)
            full_english = _join_preview_parts(parts) if parts else translated.strip()
            await self._on_interim_translation(
                full_english,
                "google_fragment",
                True,
                full_english,
                "",
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[google_translate] Fragment error for '%s': %s", spanish[:40], e)

    async def _do_translate_interim(self, spanish: str, prev_context: list[str]):
        my_task = asyncio.current_task()
        try:
            await asyncio.sleep(INTERIM_PREVIEW_DEBOUNCE_S)
            segments = prev_context + [spanish]
            translated = await self._call_api(_wrap(segments))
            if my_task is not self._fragment_task:
                return
            parts = _unwrap(translated)
            if parts:
                stable_english = _join_preview_parts(parts[:-1])
                draft_english = parts[-1].strip()
                full_english = _join_preview_parts([stable_english, draft_english])
            else:
                stable_english = ""
                draft_english = translated.strip()
                full_english = draft_english
            await self._on_interim_translation(
                full_english,
                "google_interim",
                True,
                stable_english,
                draft_english,
            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[google_translate] Interim preview error for '%s': %s", spanish[:40], e)

    async def translate(self, spanish: str, ts: int):
        """Accurate sentence translation; supersedes in-flight fragment interim."""
        if self._fragment_task and not self._fragment_task.done():
            self._fragment_task.cancel()
        self._fragment_task = None
        self._fragment_context = []
        self._last_preview_spanish = ""
        self._active_task = asyncio.create_task(self._do_translate(spanish, ts))
        self._sentence_tasks = [t for t in getattr(self, "_sentence_tasks", []) if not t.done()]
        self._sentence_tasks.append(self._active_task)

    async def _do_translate(self, spanish: str, ts: int):
        async with self._sentence_lock:
            context = list(self._context)  # snapshot in commit order
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

            if translated is None:
                return

            parts = _unwrap(translated)
            current_en = parts[-1] if parts else translated.strip()
            logger.info("[google_translate] Translation: '%s' -> '%s'", spanish[:200], current_en[:200])

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
                    logger.info("[google_translate] Dual-pass checked ts=%d - no change", prev_ts)

            self._context.append((spanish, current_en, ts))
            await self._on_translation(spanish, current_en, ts)

    def _redact(self, text: str) -> str:
        """Remove the API key from any string before it reaches a log or exception."""
        return text.replace(self._api_key, "[REDACTED]") if self._api_key else text

    async def _call_api(self, html_body: str) -> str:
        try:
            resp = await self._http.post(
                TRANSLATE_URL,
                params={"key": self._api_key},
                json={"q": html_body, "source": "es", "target": "en", "format": "html"},
            )
            resp.raise_for_status()
            return resp.json()["data"]["translations"][0]["translatedText"]
        except Exception as e:
            raise RuntimeError(self._redact(str(e))) from None
