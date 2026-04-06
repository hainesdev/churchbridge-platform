import asyncio
import logging
import re
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

SENTENCE_ENDINGS = frozenset('.?!…;')
MAX_WORDS = 40
FLUSH_DELAY_S = 3.5         # fallback timer: flush if no new fragment for this long
FLUSH_DELAY_EXTENDED_S = 2.0  # each extension granted when tail looks incomplete
MAX_EXTENSIONS = 2          # maximum timer extensions before flushing unconditionally
UTTERANCE_END_GUARD_S = 1.0 # short hold when UtteranceEnd fires on an incomplete tail

# Spanish words that, when they end the accumulated text, signal an incomplete
# clause — a preposition, subordinating conjunction, article, relative pronoun,
# or possessive that makes it clear the speaker hasn't finished the thought yet.
# Used in both the fallback timer path and the UtteranceEnd soft guard.
_INCOMPLETE_TAIL = re.compile(
    r'\b('
    # subordinating conjunctions
    r'que|porque|cuando|si|aunque|mientras|como|ya\s+que|para\s+que|'
    r'dado\s+que|a\s+menos\s+que|con\s+tal\s+que|'
    # adversative / additive conjunctions at sentence end signal continuation
    r'pero|sino|más|pues|ni|y|o|'
    # relative pronouns / adverbs
    r'quien|quienes|cual|cuales|donde|cuanto|'
    # prepositions
    r'de|en|con|por|para|a|al|del|lo|ante|bajo|hacia|hasta|sin|sobre|tras|'
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
       applies an incomplete-tail soft guard first; flushes if tail looks complete.
    3. Accumulated word count reaches MAX_WORDS (safety valve)
    4. Fallback timer (FLUSH_DELAY_S) with incomplete-tail guard:
       if the accumulated text ends with a preposition/conjunction/article that
       signals an incomplete clause, the timer is extended up to MAX_EXTENSIONS times.

    Discourse-based holds
    ---------------------
    The session can call hold_next(reason, secs) after flushing a sentence that
    introduced a quote or asked a rhetorical question. The next delayed flush will
    wait an extra hold_secs before triggering, giving the continuation time to arrive.
    This is also used as a feedback loop from LLM enrichment: when thought_complete
    is false for sentence N, hold_next is called so sentence N+1 accumulates longer.

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
        self._extension_count: int = 0  # number of timer extensions granted so far
        # Extra hold requested by the session for the next delayed flush
        self._hold_secs: float = 0.0
        self._hold_reason: str = ""
        # Sermon-relative audio span of the current accumulated sentence
        self._audio_start: float | None = None
        self._audio_end: float = 0.0

    def hold_next(self, reason: str, hold_secs: float = 3.0) -> None:
        """Request an extended delay before the next sentence is flushed.

        Called by the session when the just-flushed sentence introduced a quote,
        asked a rhetorical question, or was flagged incomplete by the LLM.
        The next timer-based flush will sleep an extra hold_secs before triggering.
        Multiple calls keep the maximum, not the sum — one hold at a time.
        Has no effect on punctuation-triggered or UtteranceEnd flushes.
        """
        self._hold_secs = max(self._hold_secs, hold_secs)
        self._hold_reason = reason
        logger.debug("[sentence_buffer] Hold queued: %s (%.1fs)", reason, hold_secs)

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
        """Flush triggered by Deepgram's UtteranceEnd VAD event.

        UtteranceEnd is the strongest "speaker stopped" signal we have, but
        preachers frequently pause mid-clause for emphasis. If the accumulated
        text ends with a clear incomplete-clause marker, we grant one short hold
        (UTTERANCE_END_GUARD_S) to allow the next Deepgram final to arrive and
        complete the thought. If the tail looks complete — or the guard has
        already fired once — we flush unconditionally.
        """
        self._cancel_timer()
        if not self._parts:
            return
        combined = ' '.join(self._parts)
        if self._extension_count == 0 and _INCOMPLETE_TAIL.search(combined):
            logger.debug(
                "[sentence_buffer] UtteranceEnd soft guard — incomplete tail: %s", combined[:60]
            )
            self._extension_count += 1
            self._timer = asyncio.create_task(self._utterance_end_guard())
        else:
            logger.debug("[sentence_buffer] UtteranceEnd flush: %s", combined[:60])
            await self._flush()

    async def _utterance_end_guard(self):
        """Short hold after UtteranceEnd on an incomplete tail.

        Waits UTTERANCE_END_GUARD_S for the next Deepgram final. If a new
        fragment arrives (via add()), this task is cancelled and the buffer
        continues normally. If nothing arrives, we flush unconditionally.
        """
        try:
            await asyncio.sleep(UTTERANCE_END_GUARD_S)
            if self._parts:
                logger.debug(
                    "[sentence_buffer] UtteranceEnd guard expired — flushing: %s",
                    ' '.join(self._parts)[:60],
                )
                await self._flush()
        except asyncio.CancelledError:
            pass

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
        """Fallback timer flush. Consumes any pending discourse hold first, then
        extends up to MAX_EXTENSIONS times when the tail looks incomplete.
        """
        # Consume hold — set by the session after a quote intro or rhetorical question
        hold = self._hold_secs
        hold_reason = self._hold_reason
        self._hold_secs = 0.0
        self._hold_reason = ""
        effective_delay = FLUSH_DELAY_S + hold

        try:
            if hold > 0:
                logger.debug(
                    "[sentence_buffer] Discourse hold %.1fs (%s): %s",
                    hold, hold_reason, ' '.join(self._parts)[:60],
                )
            await asyncio.sleep(effective_delay)
            if not self._parts:
                return
            combined = ' '.join(self._parts)
            if self._extension_count < MAX_EXTENSIONS and _INCOMPLETE_TAIL.search(combined):
                self._extension_count += 1
                logger.debug(
                    "[sentence_buffer] Incomplete tail (extension %d/%d): %s",
                    self._extension_count, MAX_EXTENSIONS, combined[:60],
                )
                self._timer = asyncio.create_task(self._extended_flush())
            else:
                await self._flush()
        except asyncio.CancelledError:
            pass

    async def _extended_flush(self):
        """Second (and subsequent) timer after an incomplete-tail extension."""
        try:
            await asyncio.sleep(FLUSH_DELAY_EXTENDED_S)
            if self._parts:
                combined = ' '.join(self._parts)
                if self._extension_count < MAX_EXTENSIONS and _INCOMPLETE_TAIL.search(combined):
                    self._extension_count += 1
                    logger.debug(
                        "[sentence_buffer] Incomplete tail (extension %d/%d): %s",
                        self._extension_count, MAX_EXTENSIONS, combined[:60],
                    )
                    self._timer = asyncio.create_task(self._extended_flush())
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
            self._extension_count = 0
            # Do not reset _hold_secs/_hold_reason here — they are set by the session
            # AFTER _on_sentence returns, in response to the content of the sentence
            # just flushed. Resetting them here would clear them before they can be used.
            logger.debug("[sentence_buffer] Flushing: %s", sentence[:60])
            await self._on_sentence(sentence, audio_start, audio_end)

    def _cancel_timer(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None
