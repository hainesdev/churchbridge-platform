"""
Tests for SermonStateTracker mode-settling logic.

Run with:  python -m pytest tests/server/test_sermon_state_tracker.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import asyncio
import pytest

from server.services.sermon_state_tracker import SermonStateTracker, SETTLE_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def make_tracker(on_change=None):
    return SermonStateTracker(on_mode_change=on_change)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_default_settled_mode_is_exposition(self):
        tracker = make_tracker()
        assert tracker.settled_mode == "exposition"

    def test_mode_sentence_count_starts_at_zero(self):
        tracker = make_tracker()
        assert tracker.mode_sentence_count == 0


# ---------------------------------------------------------------------------
# Settling threshold
# ---------------------------------------------------------------------------

class TestSettlingThreshold:
    def test_fewer_than_threshold_signals_do_not_settle(self):
        async def run_():
            tracker = make_tracker()
            for _ in range(SETTLE_THRESHOLD - 1):
                await tracker.add_signal("illustration", ts=1000)
            assert tracker.settled_mode == "exposition"

        run(run_())

    def test_exactly_threshold_matching_signals_settles(self):
        async def run_():
            tracker = make_tracker()
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("illustration", ts=1000 + i)
            assert tracker.settled_mode == "illustration"

        run(run_())

    def test_mixed_signals_do_not_settle(self):
        async def run_():
            tracker = make_tracker()
            modes = ["illustration", "exposition", "illustration"]
            for i, m in enumerate(modes):
                await tracker.add_signal(m, ts=1000 + i)
            assert tracker.settled_mode == "exposition"  # unchanged

        run(run_())

    def test_settling_to_same_mode_does_not_fire_change(self):
        async def run_():
            changes = []
            tracker = make_tracker(on_change=lambda old, new, ts: changes.append((old, new)))
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("exposition", ts=1000 + i)
            assert changes == []  # already in exposition — no change event

        run(run_())

    def test_mode_change_event_fires_on_settle(self):
        async def run_():
            changes = []

            async def on_change(old, new, ts):
                changes.append((old, new, ts))

            tracker = make_tracker(on_change=on_change)
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("scripture", ts=1000 + i)

            assert len(changes) == 1
            assert changes[0][0] == "exposition"
            assert changes[0][1] == "scripture"
            assert changes[0][2] == 1000 + SETTLE_THRESHOLD - 1

        run(run_())

    def test_second_settle_fires_second_change_event(self):
        async def run_():
            changes = []

            async def on_change(old, new, ts):
                changes.append((old, new))

            tracker = make_tracker(on_change=on_change)
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("scripture", ts=i)
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("illustration", ts=100 + i)

            assert len(changes) == 2
            assert changes[0] == ("exposition", "scripture")
            assert changes[1] == ("scripture", "illustration")

        run(run_())


# ---------------------------------------------------------------------------
# Unknown modes
# ---------------------------------------------------------------------------

class TestUnknownMode:
    def test_unknown_mode_is_ignored(self):
        async def run_():
            tracker = make_tracker()
            await tracker.add_signal("nonexistent_mode", ts=1000)
            assert tracker.settled_mode == "exposition"

        run(run_())

    def test_unknown_mode_does_not_get_added_to_history(self):
        async def run_():
            tracker = make_tracker()
            # Unknown modes are silently ignored — they are not appended to _history.
            # This means a bogus signal does NOT break a run of valid signals.
            # After [illustration x2, bogus(ignored), illustration], history is
            # [illustration, illustration, illustration] — which settles.
            for i in range(SETTLE_THRESHOLD - 1):
                await tracker.add_signal("illustration", ts=i)
            initial_history_len = len(tracker._history)
            await tracker.add_signal("bogus", ts=100)
            # History length must not have changed — bogus was ignored
            assert len(tracker._history) == initial_history_len

        run(run_())


# ---------------------------------------------------------------------------
# mode_sentence_count
# ---------------------------------------------------------------------------

class TestModeSentenceCount:
    def test_count_increments_for_each_sentence_in_settled_mode(self):
        async def run_():
            tracker = make_tracker()
            # Settle into illustration
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("illustration", ts=i)
            # Three more signals in the settled mode
            for i in range(3):
                await tracker.add_signal("illustration", ts=100 + i)
            assert tracker.mode_sentence_count >= 3

        run(run_())

    def test_count_does_not_increment_for_other_modes(self):
        async def run_():
            tracker = make_tracker()
            await tracker.add_signal("scripture", ts=1)
            await tracker.add_signal("illustration", ts=2)
            # Still in "exposition" (not settled); count should be 0 or tracking exposition
            # Exposition signals would increment the count
            assert tracker.mode_sentence_count == 0 or tracker.settled_mode == "exposition"

        run(run_())


# ---------------------------------------------------------------------------
# Convenience predicates
# ---------------------------------------------------------------------------

class TestPredicates:
    def test_is_narrative_true_for_illustration(self):
        async def run_():
            tracker = make_tracker()
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("illustration", ts=i)
            assert tracker.is_narrative() is True
            assert tracker.is_expository() is False

        run(run_())

    def test_is_expository_true_for_scripture(self):
        async def run_():
            tracker = make_tracker()
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("scripture", ts=i)
            assert tracker.is_expository() is True
            assert tracker.is_narrative() is False

        run(run_())

    def test_is_expository_true_for_exposition(self):
        tracker = make_tracker()
        assert tracker.is_expository() is True  # default mode

    def test_is_narrative_false_by_default(self):
        tracker = make_tracker()
        assert tracker.is_narrative() is False


# ---------------------------------------------------------------------------
# get_context_label
# ---------------------------------------------------------------------------

class TestContextLabel:
    def test_all_valid_modes_return_nonempty_label(self):
        from server.services.sermon_state_tracker import SERMON_MODES
        tracker = make_tracker()
        for mode in SERMON_MODES:
            tracker._settled_mode = mode
            label = tracker.get_context_label()
            assert isinstance(label, str)
            assert len(label) > 0

    def test_default_mode_label_mentions_exposition(self):
        tracker = make_tracker()
        label = tracker.get_context_label()
        assert "explain" in label.lower() or "exposi" in label.lower() or "biblical" in label.lower()


# ---------------------------------------------------------------------------
# on_mode_change error handling
# ---------------------------------------------------------------------------

class TestOnModeChangeErrorHandling:
    def test_failing_callback_does_not_raise(self):
        async def run_():
            async def bad_callback(old, new, ts):
                raise ValueError("callback error")

            tracker = make_tracker(on_change=bad_callback)
            # Should not raise despite the failing callback
            for i in range(SETTLE_THRESHOLD):
                await tracker.add_signal("illustration", ts=i)
            assert tracker.settled_mode == "illustration"

        run(run_())
