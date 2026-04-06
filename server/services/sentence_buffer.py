import asyncio
import logging
import re
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

SENTENCE_ENDINGS = frozenset('.?!…;')
MAX_WORDS = 40              # at this word count, route through timer (completeness check still applies)
ABSOLUTE_MAX_WORDS = 60     # unconditional flush — true last resort, no completeness check
MIN_FLUSH_WORDS = 4         # below this word count, timer/UtteranceEnd extends instead of flushing
FLUSH_DELAY_S = 3.5         # fallback timer: flush if no new fragment for this long
FLUSH_DELAY_EXTENDED_S = 2.0  # each extension granted when tail looks incomplete
MAX_EXTENSIONS = 8          # hard cap on timer extensions (~19.5s total hold); MAX_WORDS is the other escape
UTTERANCE_END_GUARD_S = 1.5 # hold when UtteranceEnd fires on an incomplete tail

# Spanish words that, when they end the accumulated text, signal an incomplete
# clause — a preposition, subordinating conjunction, article, relative pronoun,
# or possessive that makes it clear the speaker hasn't finished the thought yet.
# Used in both the fallback timer path and the UtteranceEnd soft guard.
_INCOMPLETE_TAIL = re.compile(
    r'(?:'
    # Conjunction/preposition + subject pronoun — the verb is missing:
    # "que él", "pero ella", "y nosotros", "porque yo", "si ellos"
    r'(?:que|porque|si|aunque|cuando|mientras|como|pero|sino|y|o|ni|de|en|con|por|para|a)'
    r'\s+(?:él|ella|ellos|ellas|yo|tú|usted|ustedes|nosotros|nosotras|'
    r'lo|la|los|las|le|les|este|esta|esto|ese|esa|eso|aquel|aquella)'
    r'|'
    # Single incomplete-tail word (original patterns)
    r'\b(?:'
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
    r'su|sus|mi|mis|tu|tus|nuestro|nuestra|nuestros|nuestras|'
    # dangling copula/auxiliary — predicate object or complement not yet delivered
    r'es|está|están|son|era|eran|fue|fueron|hay|había'
    r')'
    r')\s*$',
    re.IGNORECASE,
)


def _is_conditional_incomplete(text: str) -> bool:
    """Return True if text opens a conditional clause ('Si...') that has no apodosis.

    Catches:  "Si decimos que tenemos como Jesucristo"  → incomplete (no resolution)
    Allows:   "Si tienes fe, todo es posible."           → complete (comma + apodosis)
              "Si buscas a Dios, entonces lo encontrarás." → complete (entonces)
              "Si nosotros decimos la verdad."            → complete (verb closes)
    """
    stripped = text.strip().rstrip('.')
    if not re.match(r'^Si\s+', stripped, re.IGNORECASE):
        return False
    # Explicit resolution markers — apodosis present
    if re.search(r'\b(?:entonces|por\s+eso|pues\s+bien|por\s+tanto|así\s+que)\b',
                 stripped, re.IGNORECASE):
        return False
    # Comma with at least 3 words of content after it — protasis/apodosis split present
    if ',' in stripped:
        after_comma = stripped.split(',', 1)[1].strip()
        if len(after_comma.split()) >= 3:
            return False
    # Sentence ends with a noun/proper noun and the conditional verb is still "que" +
    # another subordinator — predicate resolution hasn't arrived
    # Heuristic: "Si ... que ..." ending with a noun is still open
    if re.search(r'\bque\b.*\b[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+$', stripped):
        return True
    # Short conditional without a clear predicate resolution — hold
    words = stripped.split()
    if len(words) <= 10:
        return True
    return False


def _is_incomplete(text: str) -> bool:
    """Return True if the text should not be flushed on a timer or UtteranceEnd yet.

    Checks five conditions:
    1. Too few words to be a meaningful standalone sentence (< MIN_FLUSH_WORDS).
    2. Ends with a trailing comma — speaker is mid-list or mid-clause.
    3. Ends with a clause-opening word (preposition, conjunction, article, dangling verb).
    4. Contains an unclosed interrogative: ¿ opened but no ? following it.
    5. Opens a conditional clause ('Si...') with no apodosis resolution.
    """
    stripped = text.strip()
    if len(stripped.split()) < MIN_FLUSH_WORDS:
        return True
    # Trailing comma is always mid-thought: "Pero ella no entendía que yo hacía conferencias,"
    if stripped.endswith(','):
        return True
    if _INCOMPLETE_TAIL.search(text):
        return True
    # Unclosed interrogative — a ¿ was opened but the sentence never closed with ?
    if re.search(r'¿[^?]*$', text):
        return True
    # Conditional clause without apodosis — "Si decimos que tenemos como Jesucristo"
    if _is_conditional_incomplete(text):
        return True
    return False


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
        # Metrics
        self.structural_flush_block_count: int = 0   # times incomplete guard blocked a flush
        self.forced_release_count: int = 0           # times ABSOLUTE_MAX_WORDS forced a flush
        self.conditional_flush_block_count: int = 0  # times conditional-clause guard specifically blocked

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
            await self._flush("immediate_flush")
        else:
            self._timer = asyncio.create_task(self._delayed_flush())

    async def utterance_end(self):
        """Flush triggered by Deepgram's UtteranceEnd VAD event.

        UtteranceEnd is the strongest "speaker stopped" signal we have, but
        preachers frequently pause mid-clause for emphasis. If the accumulated
        text is incomplete, always apply the guard — regardless of how many
        extensions have already fired. Only flush immediately when the text is
        structurally complete.
        """
        self._cancel_timer()
        if not self._parts:
            return
        combined = ' '.join(self._parts)
        if _is_incomplete(combined) and self._extension_count < MAX_EXTENSIONS:
            logger.debug(
                "[sentence_buffer] UtteranceEnd soft guard — incomplete (ext %d): %s",
                self._extension_count, combined[:60],
            )
            self._extension_count += 1
            self._timer = asyncio.create_task(self._utterance_end_guard())
        else:
            await self._flush("utterance_end")

    async def _utterance_end_guard(self):
        """Short hold after UtteranceEnd on an incomplete tail.

        Waits UTTERANCE_END_GUARD_S for the next Deepgram final. If new content
        arrives (via add()), this task is cancelled and the buffer continues
        normally. If nothing arrives and the text is still incomplete, extend
        again (up to MAX_EXTENSIONS). Only flush once the text is complete or
        the hard cap is reached.
        """
        try:
            await asyncio.sleep(UTTERANCE_END_GUARD_S)
            if self._parts:
                combined = ' '.join(self._parts)
                if _is_incomplete(combined) and self._extension_count < MAX_EXTENSIONS:
                    logger.debug(
                        "[sentence_buffer] UtteranceEnd guard re-extended (ext %d): %s",
                        self._extension_count, combined[:60],
                    )
                    self._extension_count += 1
                    self._timer = asyncio.create_task(self._utterance_end_guard())
                else:
                    await self._flush("utterance_end_guard")
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Flush any remaining buffered text on session close."""
        self._cancel_timer()
        if self._parts:
            await self._flush("session_close")

    def _should_flush(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        # Terminal punctuation: speaker explicitly ended the thought — always flush.
        if stripped[-1] in SENTENCE_ENDINGS:
            return True
        # Absolute safety valve: unconditional flush at ABSOLUTE_MAX_WORDS regardless
        # of completeness, to prevent runaway buffers.
        if len(stripped.split()) >= ABSOLUTE_MAX_WORDS:
            self.forced_release_count += 1
            return True
        # Between MAX_WORDS and ABSOLUTE_MAX_WORDS: don't flush immediately — let the
        # timer fire so _is_incomplete() can gate the flush.
        return False

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
            if self._extension_count < MAX_EXTENSIONS and _is_incomplete(combined):
                self._extension_count += 1
                self.structural_flush_block_count += 1
                if _is_conditional_incomplete(combined):
                    self.conditional_flush_block_count += 1
                logger.debug(
                    "[sentence_buffer] Incomplete tail (extension %d/%d): %s",
                    self._extension_count, MAX_EXTENSIONS, combined[:60],
                )
                self._timer = asyncio.create_task(self._extended_flush())
            else:
                await self._flush("buffer_timer")
        except asyncio.CancelledError:
            pass

    async def _extended_flush(self):
        """Second (and subsequent) timer after an incomplete-tail extension."""
        try:
            await asyncio.sleep(FLUSH_DELAY_EXTENDED_S)
            if self._parts:
                combined = ' '.join(self._parts)
                if self._extension_count < MAX_EXTENSIONS and _is_incomplete(combined):
                    self._extension_count += 1
                    self.structural_flush_block_count += 1
                    if _is_conditional_incomplete(combined):
                        self.conditional_flush_block_count += 1
                    logger.debug(
                        "[sentence_buffer] Incomplete tail (extension %d/%d): %s",
                        self._extension_count, MAX_EXTENSIONS, combined[:60],
                    )
                    self._timer = asyncio.create_task(self._extended_flush())
                else:
                    await self._flush("buffer_timer")
        except asyncio.CancelledError:
            pass

    async def _flush(self, reason: str = "unknown"):
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
            logger.info(
                "[sentence_buffer] decision=%s words=%d text=%s",
                reason, len(sentence.split()), sentence[:80],
            )
            await self._on_sentence(sentence, audio_start, audio_end)

    def _cancel_timer(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None
