import asyncio
import logging
import re
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

SENTENCE_ENDINGS = frozenset('.?!…;')
MAX_WORDS = 40
FLUSH_DELAY_S = 3.5       # fallback timer: flush if no new fragment for this long
FLUSH_DELAY_EXTENDED_S = 2.0  # one-time extension granted when tail looks incomplete

# Spanish words that, when they end the accumulated text, signal an incomplete
# clause — a preposition, subordinating conjunction, article, or possessive that
# makes it clear the speaker hasn't finished the thought yet.
# Only used in the fallback timer path; UtteranceEnd flushes unconditionally.
_INCOMPLETE_TAIL = re.compile(
    r'\b('
    # subordinating conjunctions
    r'que|porque|cuando|si|aunque|mientras|como|ya\s+que|para\s+que|'
    # coordinating (at end without punctuation they're clearly incomplete)
    r'y|o|ni|'
    # prepositions
    r'de|en|con|por|para|a|al|del|ante|bajo|hacia|hasta|sin|sobre|tras|'
    # articles
    r'el|la|los|las|un|una|unos|unas|'
    # possessives
    r'su|sus|mi|mis|tu|tus|nuestro|nuestra|nuestros|nuestras'
    r')\s*$',
    re.IGNORECASE,
)


class SentenceBuffer:
    """Accumulates Deepgram final segments and flushes at sentence boundaries.

    Primary flush signals (in priority order):
    1. Terminal punctuation at end of combined text (. ? ! … ;)
    2. utterance_end() called by the session when Deepgram fires UtteranceEnd —
       the VAD detected the speaker stopped; flush whatever is buffered.
    3. Accumulated word count reaches MAX_WORDS (safety valve)
    4. Fallback timer (FLUSH_DELAY_S) with incomplete-tail guard:
       if the accumulated text ends with a preposition/conjunction/article that
       signals an incomplete clause, the timer is extended once (FLUSH_DELAY_EXTENDED_S)
       before flushing unconditionally.

    Audio timing
    ------------
    Each fragment carries Deepgram sermon-relative (audio_start, audio_end) floats.
    The buffer tracks the span of the accumulated sentence:
      - audio_start: set from the first fragment in the sentence
      - audio_end:   updated with each fragment; reflects the last spoken word
    Both are passed to on_sentence so the enrichment layer can use the real
    sermon timeline for verse consolidation.
    """

    def __init__(self, on_sentence: Callable[[str, float, float], Awaitable[None]]):
        self._on_sentence = on_sentence
        self._parts: list[str] = []
        self._timer: asyncio.Task | None = None
        self._timer_extended: bool = False
        # Sermon-relative audio span of the current accumulated sentence
        self._audio_start: float | None = None
        self._audio_end: float = 0.0

    async def add(self, text: str, audio_start: float = 0.0, audio_end: float = 0.0):
        self._cancel_timer()
        self._parts.append(text)
        # Pin audio_start to the first fragment; advance audio_end with each new one
        if self._audio_start is None:
            self._audio_start = audio_start
        self._audio_end = audio_end

        combined = ' '.join(self._parts)
        if self._should_flush(combined):
            await self._flush()
        else:
            self._timer = asyncio.create_task(self._delayed_flush())

    async def utterance_end(self):
        """Hard flush triggered by Deepgram's UtteranceEnd VAD event.

        Called when Deepgram detects the speaker has stopped. Flushes
        unconditionally — this is a higher-quality signal than the fallback timer
        so we trust it regardless of whether the text looks complete.
        """
        self._cancel_timer()
        if self._parts:
            logger.debug("[sentence_buffer] UtteranceEnd flush: %s", ' '.join(self._parts)[:60])
            await self._flush()

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
        """Fallback timer flush. Extends once if the tail looks like an incomplete clause."""
        try:
            await asyncio.sleep(FLUSH_DELAY_S)
            if not self._parts:
                return
            combined = ' '.join(self._parts)
            if not self._timer_extended and _INCOMPLETE_TAIL.search(combined):
                # The text ends mid-clause. Grant one extension — Deepgram's UtteranceEnd
                # should arrive before this fires if the speaker is just pausing.
                logger.debug(
                    "[sentence_buffer] Incomplete tail detected, extending timer: %s", combined[:60]
                )
                self._timer_extended = True
                self._timer = asyncio.create_task(self._delayed_flush())
            else:
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _flush(self):
        self._cancel_timer()
        if self._parts:
            sentence = ' '.join(self._parts)
            audio_start = self._audio_start or 0.0
            audio_end = self._audio_end
            self._parts = []
            self._audio_start = None
            self._audio_end = 0.0
            self._timer_extended = False
            logger.debug("[sentence_buffer] Flushing: %s", sentence[:60])
            await self._on_sentence(sentence, audio_start, audio_end)

    def _cancel_timer(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None
