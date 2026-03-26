import asyncio
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

SENTENCE_ENDINGS = frozenset('.?!…;')
MAX_WORDS = 40
FLUSH_DELAY_S = 3.5


class SentenceBuffer:
    """Accumulates Deepgram final segments and flushes at sentence boundaries.

    Flushes when:
    - Last non-space character is sentence-ending punctuation (. ? ! … ;)
    - Accumulated word count reaches MAX_WORDS (unpunctuated speech safety valve)
    - FLUSH_DELAY_S seconds pass with no new segment (silence / end of thought)

    This ensures Google Translate receives complete thoughts rather than
    mid-sentence fragments, which dramatically improves translation accuracy.
    """

    def __init__(self, on_sentence: Callable[[str], Awaitable[None]]):
        self._on_sentence = on_sentence
        self._parts: list[str] = []
        self._timer: asyncio.Task | None = None

    async def add(self, text: str):
        self._cancel_timer()
        self._parts.append(text)
        combined = ' '.join(self._parts)

        if self._should_flush(combined):
            await self._flush()
        else:
            self._timer = asyncio.create_task(self._delayed_flush())

    async def stop(self):
        """Flush any remaining buffered text on session close."""
        self._cancel_timer()
        if self._parts:
            await self._flush()

    def _should_flush(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        return stripped[-1] in SENTENCE_ENDINGS or len(stripped.split()) >= MAX_WORDS

    async def _delayed_flush(self):
        try:
            await asyncio.sleep(FLUSH_DELAY_S)
            if self._parts:
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self):
        self._cancel_timer()
        if self._parts:
            sentence = ' '.join(self._parts)
            self._parts = []
            logger.debug("[sentence_buffer] Flushing: %s", sentence[:60])
            await self._on_sentence(sentence)

    def _cancel_timer(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None
