"""
Tests for SentenceBuffer async flush behavior.

Covers: punctuation flush, fragment accumulation, UtteranceEnd guard,
discourse holds, MAX_EXTENSIONS cap, stop(), metrics, and audio timing.

Run with:  python -m pytest tests/server/test_sentence_buffer.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import asyncio
import pytest

from server.services.sentence_buffer import (
    SentenceBuffer,
    FLUSH_DELAY_S,
    FLUSH_DELAY_EXTENDED_S,
    ABSOLUTE_MAX_WORDS,
    MAX_EXTENSIONS,
    MIN_FLUSH_WORDS,
    UTTERANCE_END_GUARD_S,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Collector:
    """Captures on_sentence calls for assertion."""
    def __init__(self):
        self.calls: list[tuple[str, float, float]] = []

    async def on_sentence(self, text: str, audio_start: float, audio_end: float):
        self.calls.append((text, audio_start, audio_end))

    @property
    def texts(self) -> list[str]:
        return [c[0] for c in self.calls]


def make_buffer(collector: Collector) -> SentenceBuffer:
    return SentenceBuffer(on_sentence=collector.on_sentence)


# ---------------------------------------------------------------------------
# _should_flush — synchronous logic
# ---------------------------------------------------------------------------

class TestShouldFlush:
    def test_terminal_period_flushes(self):
        buf = make_buffer(Collector())
        assert buf._should_flush("Dios es amor.") is True

    def test_terminal_question_flushes(self):
        buf = make_buffer(Collector())
        assert buf._should_flush("¿Quién es él?") is True

    def test_terminal_exclamation_flushes(self):
        buf = make_buffer(Collector())
        assert buf._should_flush("Aleluya!") is True

    def test_terminal_ellipsis_flushes(self):
        buf = make_buffer(Collector())
        assert buf._should_flush("Él viene…") is True

    def test_no_punctuation_does_not_flush(self):
        buf = make_buffer(Collector())
        assert buf._should_flush("Dios es amor") is False

    def test_absolute_max_words_flushes(self):
        buf = make_buffer(Collector())
        long_text = " ".join(["palabra"] * ABSOLUTE_MAX_WORDS)
        assert buf._should_flush(long_text) is True
        assert buf.forced_release_count == 1

    def test_just_under_absolute_max_does_not_force_flush(self):
        buf = make_buffer(Collector())
        text = " ".join(["palabra"] * (ABSOLUTE_MAX_WORDS - 1))
        assert buf._should_flush(text) is False

    def test_empty_string_does_not_flush(self):
        buf = make_buffer(Collector())
        assert buf._should_flush("") is False


# ---------------------------------------------------------------------------
# add() — immediate flush on terminal punctuation
# ---------------------------------------------------------------------------

class TestImmediateFlush:
    def test_punctuated_fragment_flushes_immediately(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("Dios es amor.")
            assert col.texts == ["Dios es amor."]

        asyncio.run(run())

    def test_unpunctuated_fragment_does_not_flush_immediately(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("Dios es amor")
            assert col.calls == []
            buf._cancel_timer()  # clean up pending timer

        asyncio.run(run())

    def test_two_fragments_accumulate_before_punctuation(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("Dios")
            await buf.add("es amor.")
            assert col.texts == ["Dios es amor."]

        asyncio.run(run())

    def test_timer_cancelled_on_punctuation_flush(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("primer fragmento")   # sets timer
            assert buf._timer is not None
            await buf.add("segundo fragmento.")  # punctuation → flush + cancel timer
            # Yield to event loop so the cancelled task can process CancelledError
            await asyncio.sleep(0)
            assert buf._timer is None or buf._timer.cancelled() or buf._timer.done()
            assert len(col.calls) == 1

        asyncio.run(run())

    def test_audio_timing_passed_to_callback(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("Dios es amor.", audio_start=1.0, audio_end=3.5)
            assert col.calls[0][1] == 1.0
            assert col.calls[0][2] == 3.5

        asyncio.run(run())

    def test_audio_start_pinned_to_first_fragment(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("primer", audio_start=1.0, audio_end=2.0)
            await buf.add("fragmento.", audio_start=2.5, audio_end=4.0)
            _, start, end = col.calls[0]
            assert start == 1.0   # pinned to first
            assert end == 4.0     # advanced to last

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Timer-based flush
# ---------------------------------------------------------------------------

class TestTimerFlush:
    def test_complete_text_flushes_after_delay(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("Dios es amor y su gracia es eterna")
            # Wait longer than FLUSH_DELAY_S
            await asyncio.wait_for(
                _wait_for_flush(col), timeout=FLUSH_DELAY_S + 2.0
            )
            assert len(col.calls) == 1

        asyncio.run(run())

    def test_incomplete_tail_extends_timer(self):
        """An incomplete tail blocks the first timer; completing the text lets it flush."""
        async def run():
            col = Collector()
            buf = make_buffer(col)
            # "de" is an incomplete tail — the initial timer blocks on it
            await buf.add("el amor de")
            # Wait past FLUSH_DELAY_S so the extension fires
            await asyncio.sleep(FLUSH_DELAY_S + 0.5)
            assert buf.structural_flush_block_count >= 1
            assert len(col.calls) == 0   # still blocked
            # Complete the thought — next add flushes immediately (no trailing incomplete)
            await buf.add("Dios.")
            assert len(col.calls) == 1

        asyncio.run(run())

    def test_adding_fragment_cancels_pending_timer(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("primera oración")
            first_timer = buf._timer
            assert first_timer is not None
            await asyncio.sleep(0.1)
            await buf.add("segunda oración.")
            # The punctuation flush replaces (and cancels) the first timer.
            # buf._timer is now None (flush cleared it) or a new task — either way
            # it is no longer the same object as first_timer.
            assert buf._timer is not first_timer
            assert col.texts == ["primera oración segunda oración."]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# utterance_end()
# ---------------------------------------------------------------------------

class TestUtteranceEnd:
    def test_complete_text_flushes_on_utterance_end(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("Dios es luz y en él no hay tinieblas")
            await buf.utterance_end()
            assert len(col.calls) == 1

        asyncio.run(run())

    def test_incomplete_tail_gets_guard_not_immediate_flush(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("el amor de")  # ends with preposition — incomplete
            await buf.utterance_end()
            # Should NOT flush immediately
            assert len(col.calls) == 0
            # Guard re-extends when text is still incomplete; wait long enough for
            # the extension chain to exhaust or new content to arrive.
            # Each guard cycle is UTTERANCE_END_GUARD_S; give several cycles + margin.
            await asyncio.wait_for(
                _wait_for_flush(col), timeout=(MAX_EXTENSIONS + 2) * UTTERANCE_END_GUARD_S + 2.0
            )
            assert len(col.calls) == 1

        asyncio.run(run())

    def test_empty_buffer_utterance_end_does_nothing(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.utterance_end()
            assert col.calls == []

        asyncio.run(run())

    def test_extension_count_increments_on_guard(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("el amor de")
            initial = buf._extension_count
            await buf.utterance_end()
            await asyncio.sleep(0.05)
            assert buf._extension_count == initial + 1

        asyncio.run(run())


# ---------------------------------------------------------------------------
# hold_next()
# ---------------------------------------------------------------------------

class TestHoldNext:
    def test_hold_increases_effective_delay(self):
        async def run():
            col_base = Collector()
            col_held = Collector()
            buf_base = make_buffer(col_base)
            buf_held = make_buffer(col_held)
            hold_extra = 1.5

            # Both buffers get a complete sentence without punctuation
            await buf_base.add("Dios es luz y en él no hay tinieblas")
            buf_held.hold_next("test_hold", hold_secs=hold_extra)
            await buf_held.add("Dios es luz y en él no hay tinieblas")

            # Base buffer should flush at ~FLUSH_DELAY_S; held should not yet
            await asyncio.sleep(FLUSH_DELAY_S + 0.3)
            assert len(col_base.calls) == 1
            assert len(col_held.calls) == 0  # still held

            # Held buffer flushes after additional hold
            await asyncio.wait_for(
                _wait_for_flush(col_held), timeout=hold_extra + 1.0
            )
            assert len(col_held.calls) == 1

        asyncio.run(run())

    def test_multiple_holds_keep_maximum_not_sum(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            buf.hold_next("reason_a", hold_secs=1.0)
            buf.hold_next("reason_b", hold_secs=2.0)
            buf.hold_next("reason_c", hold_secs=0.5)
            assert buf._hold_secs == 2.0  # maximum wins

        asyncio.run(run())

    def test_hold_consumed_on_timer_flush_not_immediate_flush(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            # Hold is consumed when _delayed_flush() runs, not on immediate punctuation flush.
            # After a punctuation flush the hold remains set (it targets the *next* timer flush).
            buf.hold_next("test", hold_secs=3.0)
            await buf.add("Dios es amor.")   # immediate punctuation flush
            assert len(col.calls) == 1
            # Hold is still set — it hasn't been consumed yet (no timer fired)
            assert buf._hold_secs == 3.0
            # Now add a non-punctuated fragment — this starts the timer, consuming the hold
            await buf.add("siguiente oración sin punto")
            # _hold_secs is reset when _delayed_flush() consumes it
            await asyncio.sleep(0.05)
            assert buf._hold_secs == 0.0
            buf._cancel_timer()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# MAX_EXTENSIONS cap
# ---------------------------------------------------------------------------

class TestMaxExtensions:
    def test_max_extensions_forces_flush(self):
        """After MAX_EXTENSIONS extensions on perpetually incomplete text, must flush."""
        async def run():
            col = Collector()
            buf = make_buffer(col)
            # "de" is perpetually incomplete — will extend every cycle
            await buf.add("el amor de")
            total_wait = FLUSH_DELAY_S + (MAX_EXTENSIONS + 1) * FLUSH_DELAY_EXTENDED_S + 2.0
            await asyncio.wait_for(_wait_for_flush(col), timeout=total_wait)
            assert len(col.calls) == 1

        asyncio.run(run())


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_flushes_remaining_text(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("texto sin terminar")
            assert col.calls == []
            await buf.stop()
            assert col.texts == ["texto sin terminar"]

        asyncio.run(run())

    def test_stop_on_empty_buffer_does_nothing(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.stop()
            assert col.calls == []

        asyncio.run(run())

    def test_stop_cancels_pending_timer(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("texto pendiente")
            timer = buf._timer
            await buf.stop()
            # Yield so the event loop can process the CancelledError on the timer task
            await asyncio.sleep(0)
            assert timer is None or timer.cancelled() or timer.done()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_forced_release_count_increments_on_absolute_max(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            long_text = " ".join(["palabra"] * ABSOLUTE_MAX_WORDS)
            await buf.add(long_text)
            assert buf.forced_release_count == 1

        asyncio.run(run())

    def test_structural_flush_block_count_increments_on_extension(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("el amor de")  # incomplete tail
            await asyncio.sleep(FLUSH_DELAY_S + 0.3)  # let first timer fire and extend
            await asyncio.sleep(0.1)
            assert buf.structural_flush_block_count >= 1
            buf._cancel_timer()

        asyncio.run(run())

    def test_conditional_flush_block_count_increments_on_si_incomplete(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            # Short Si-clause without apodosis → _is_conditional_incomplete returns True
            await buf.add("Si decimos que tenemos comunión con él")
            await asyncio.sleep(FLUSH_DELAY_S + 0.3)
            await asyncio.sleep(0.1)
            assert buf.conditional_flush_block_count >= 1
            buf._cancel_timer()

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Audio timing propagation
# ---------------------------------------------------------------------------

class TestAudioTiming:
    def test_start_pinned_end_advances(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("primero", audio_start=1.0, audio_end=2.0)
            await buf.add("segundo", audio_start=2.5, audio_end=3.5)
            await buf.add("tercero.", audio_start=4.0, audio_end=5.0)
            _, start, end = col.calls[0]
            assert start == 1.0
            assert end == 5.0

        asyncio.run(run())

    def test_reset_after_flush(self):
        async def run():
            col = Collector()
            buf = make_buffer(col)
            await buf.add("primera.", audio_start=1.0, audio_end=2.0)
            assert buf._audio_start is None  # reset after flush
            await buf.add("segunda.", audio_start=5.0, audio_end=6.0)
            _, start, end = col.calls[1]
            assert start == 5.0
            assert end == 6.0

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

async def _wait_for_flush(col: Collector, poll_interval: float = 0.05):
    """Poll until at least one flush has been collected."""
    while not col.calls:
        await asyncio.sleep(poll_interval)
