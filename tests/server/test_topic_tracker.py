"""Tests for the structured-memory TopicTracker.

Run with::

    python -m pytest tests/server/test_topic_tracker.py -v

The tests stub out the Anthropic client so the tracker never makes a
network call. They cover:

- request_refresh strength/reason validation, dedupe, and cooldown
- in-flight protection + strongest-pending-request rerun
- structured memory parsing + fallback when JSON is malformed
- prompt-block assembly (Phase 4)
- backward-compatible ``get_context()`` view
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Awaitable, Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest

from server.services import topic_tracker as tt_module
from server.services.topic_tracker import (
    ActivePassageState,
    SermonStateMemory,
    SummaryStateMemory,
    TopicTracker,
    _SIGNAL_DEDUPE_WINDOW_SECS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_anthropic_response(raw_json_text: str):
    usage = type(
        "Usage",
        (),
        {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    )()

    class _Block:
        def __init__(self, text: str):
            self.text = text

    class _Resp:
        def __init__(self, text: str):
            self.content = [_Block(text)]
            self.usage = usage

    return _Resp(raw_json_text)


class FakeMessages:
    def __init__(
        self,
        responses: list[str] | None = None,
        delay_s: float = 0.0,
        on_call: Callable[[dict], None] | None = None,
    ):
        self._responses = list(responses or [])
        self._default_response = self._responses[0] if self._responses else None
        self._call_count = 0
        self._delay_s = delay_s
        self._on_call = on_call
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._on_call is not None:
            self._on_call(kwargs)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        if self._call_count < len(self._responses):
            text = self._responses[self._call_count]
        else:
            text = self._default_response or "{}"
        self._call_count += 1
        return _make_anthropic_response(text)


class FakeAnthropicClient:
    def __init__(self, messages: FakeMessages):
        self.messages = messages


def _structured_response_payload(**overrides) -> str:
    payload = {
        "active_passage_override": None,
        "sermon_arc": "development",
        "rhetorical_goal": "establish biblical authority for the main claim",
        "primary_themes": ["light", "fellowship"],
        "supporting_themes": ["holiness"],
        "illustration_state": {"active": False, "subject": None},
        "short_summary": "Walking in the light requires confession and fellowship.",
        "long_summary": "The preacher unpacks 1 John 1, contrasting darkness with light...",
        "confidence": 0.85,
        "refresh_reason_echo": "periodic",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _make_tracker(
    *,
    raw_responses: list[str] | None = None,
    delay_s: float = 0.0,
    on_observability_event: Callable[[dict], Awaitable[None]] | None = None,
    sermon_topic: str = "",
) -> tuple[TopicTracker, FakeMessages]:
    fake_messages = FakeMessages(responses=raw_responses, delay_s=delay_s)
    tracker = TopicTracker(
        church_id="test-church",
        sermon_topic=sermon_topic,
        on_observability_event=on_observability_event,
    )
    tracker._client = FakeAnthropicClient(fake_messages)
    return tracker, fake_messages


def run(coro):
    return asyncio.run(coro)


async def _wait_for_in_flight(tracker: TopicTracker) -> None:
    if tracker._update_task is not None:
        try:
            await tracker._update_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_default_memory_is_empty(self):
        tracker, _ = _make_tracker()
        memory = tracker.get_memory()
        assert memory.active_passage.reference == ""
        assert memory.sermon_state.current_mode == "exposition"
        assert memory.theme_state.primary_themes == []
        assert memory.illustration_state.active is False
        # Backward-compat surface still works without any refresh
        assert tracker.get_context() == ""

    def test_sermon_topic_seeds_short_summary(self):
        tracker, _ = _make_tracker(sermon_topic="The light of Christ")
        memory = tracker.get_memory()
        assert memory.summary_state.short_summary == "The light of Christ"
        assert "The light of Christ" in tracker.get_context()


# ---------------------------------------------------------------------------
# set_active_passage
# ---------------------------------------------------------------------------

class TestSetActivePassage:
    def test_active_passage_updates_immediately(self):
        tracker, _ = _make_tracker()
        tracker.set_active_passage("1 John 1:5-7", "God is light...")
        ap = tracker.get_memory().active_passage
        assert ap.reference == "1 John 1:5-7"
        assert ap.canonical_english == "God is light..."
        assert ap.source == "verse_detection"
        assert ap.confidence == "explicit"
        assert ap.updated_at_ts > 0

    def test_active_passage_reflected_in_get_context(self):
        tracker, _ = _make_tracker()
        tracker.set_active_passage("John 3:16", "For God so loved the world...")
        ctx = tracker.get_context()
        assert "John 3:16" in ctx
        assert "For God so loved" in ctx

    def test_blank_reference_is_ignored(self):
        tracker, _ = _make_tracker()
        tracker.set_active_passage("1 John 1", "God is light")
        tracker.set_active_passage("", "")
        ap = tracker.get_memory().active_passage
        assert ap.reference == "1 John 1"


# ---------------------------------------------------------------------------
# request_refresh: validation + dedupe + cooldown
# ---------------------------------------------------------------------------

class TestRequestRefreshValidation:
    def test_unknown_strength_is_invalid(self):
        tracker, _ = _make_tracker()
        outcome = tracker.request_refresh(reason="passage_change", strength="strongest")
        assert outcome == "invalid"
        assert tracker.metrics["refresh_suppressed_invalid"] == 1

    def test_unknown_reason_is_invalid(self):
        tracker, _ = _make_tracker()
        outcome = tracker.request_refresh(reason="something_new", strength="strong")
        assert outcome == "invalid"

    def test_none_strength_is_ignored(self):
        tracker, _ = _make_tracker()
        outcome = tracker.request_refresh(reason="mode_shift", strength="none")
        assert outcome == "ignored"
        assert tracker.metrics["refresh_count"] == 0

    def test_strong_request_with_too_few_segments_ignored_unless_urgent(self):
        tracker, _ = _make_tracker()
        # No segments yet — only urgent reasons (passage_change) are allowed
        # to force an early summary.
        outcome = tracker.request_refresh(reason="mode_shift", strength="strong")
        assert outcome == "ignored"

    def test_urgent_passage_change_forces_early_refresh(self):
        async def _():
            tracker, fake = _make_tracker(raw_responses=[_structured_response_payload()])
            outcome = tracker.request_refresh(
                reason="passage_change", strength="strong",
            )
            assert outcome == "scheduled"
            assert tracker.metrics["refresh_count"] == 1
            await _wait_for_in_flight(tracker)
            assert len(fake.calls) == 1

        run(_())


class TestRequestRefreshDedupe:
    def test_repeated_identical_signal_is_deduped(self):
        async def _():
            tracker, _ = _make_tracker(
                raw_responses=[_structured_response_payload()],
            )
            # Seed enough segments so non-urgent strong refreshes are allowed.
            for i in range(8):
                tracker.add_segment(f"sentence {i}")
            await _wait_for_in_flight(tracker)
            # Now any subsequent identical signal inside the dedupe window
            # should be dropped, regardless of cooldown.
            tracker._last_refresh_monotonic = 0.0  # bypass cooldown check
            outcome_1 = tracker.request_refresh(reason="mode_shift", strength="soft")
            outcome_2 = tracker.request_refresh(reason="mode_shift", strength="soft")
            # outcome_1 may have scheduled; outcome_2 must dedupe.
            assert outcome_1 in {"scheduled", "queued_pending", "cooldown"}
            assert outcome_2 == "deduped"

        run(_())


class TestRequestRefreshCooldown:
    def test_soft_signal_blocked_by_cooldown(self, monkeypatch):
        async def _():
            monkeypatch.setattr(tt_module, "MIN_REFRESH_GAP_SECS", 60.0)
            monkeypatch.setattr(tt_module, "STRONG_REFRESH_GAP_SECS", 5.0)
            tracker, _msgs = _make_tracker(
                raw_responses=[_structured_response_payload()],
            )
            for i in range(8):
                tracker.add_segment(f"seg-{i}")
            await _wait_for_in_flight(tracker)
            # Bypass dedupe by varying the active passage between requests.
            tracker.set_active_passage("Romans 8:28", "And we know...")
            outcome = tracker.request_refresh(reason="theme_shift", strength="soft")
            assert outcome == "cooldown"
            assert tracker.metrics["refresh_suppressed_cooldown"] >= 1

        run(_())

    def test_strong_non_urgent_uses_short_cooldown(self, monkeypatch):
        async def _():
            monkeypatch.setattr(tt_module, "MIN_REFRESH_GAP_SECS", 60.0)
            monkeypatch.setattr(tt_module, "STRONG_REFRESH_GAP_SECS", 0.0)
            tracker, msgs = _make_tracker(
                raw_responses=[
                    _structured_response_payload(),
                    _structured_response_payload(),
                ],
            )
            for i in range(8):
                tracker.add_segment(f"seg-{i}")
            await _wait_for_in_flight(tracker)
            tracker.set_active_passage("Romans 8:28", "And we know...")
            outcome = tracker.request_refresh(
                reason="application_started", strength="strong",
            )
            assert outcome == "scheduled"
            await _wait_for_in_flight(tracker)
            assert tracker.metrics["strong_refresh_count"] == 1
            assert len(msgs.calls) == 2

        run(_())


# ---------------------------------------------------------------------------
# In-flight protection + pending rerun
# ---------------------------------------------------------------------------

class TestInFlightProtection:
    def test_stronger_pending_request_reruns_after_completion(self, monkeypatch):
        async def _():
            monkeypatch.setattr(tt_module, "MIN_REFRESH_GAP_SECS", 0.0)
            monkeypatch.setattr(tt_module, "STRONG_REFRESH_GAP_SECS", 0.0)
            tracker, msgs = _make_tracker(
                raw_responses=[
                    _structured_response_payload(short_summary="first"),
                    _structured_response_payload(short_summary="second"),
                ],
                delay_s=0.05,
            )
            for i in range(8):
                tracker.add_segment(f"seg-{i}")
            # The add_segment calls likely scheduled a periodic refresh —
            # while it is in flight, queue a stronger request.
            outcome = tracker.request_refresh(
                reason="passage_change", strength="strong",
            )
            assert outcome in {"queued_pending", "scheduled"}
            await _wait_for_in_flight(tracker)
            await _wait_for_in_flight(tracker)
            # We should have made at most one extra call — the queued
            # strong rerun coalesces into a single follow-up.
            assert 1 <= len(msgs.calls) <= 3
            assert tracker.metrics["refresh_count"] >= 1

        run(_())

    def test_only_one_task_runs_at_a_time(self, monkeypatch):
        async def _():
            monkeypatch.setattr(tt_module, "MIN_REFRESH_GAP_SECS", 0.0)
            monkeypatch.setattr(tt_module, "STRONG_REFRESH_GAP_SECS", 0.0)
            tracker, _msgs = _make_tracker(
                raw_responses=[_structured_response_payload()],
                delay_s=0.05,
            )
            for i in range(8):
                tracker.add_segment(f"seg-{i}")
            first_task = tracker._update_task
            # Even strong urgent refreshes should not spawn a second
            # concurrent task while one is in flight.
            tracker.request_refresh(reason="passage_change", strength="strong")
            assert tracker._update_task is first_task
            await _wait_for_in_flight(tracker)

        run(_())


# ---------------------------------------------------------------------------
# Structured memory parsing + fallback
# ---------------------------------------------------------------------------

class TestMemoryParsing:
    def test_structured_memory_is_applied(self):
        async def _():
            tracker, _msgs = _make_tracker(
                raw_responses=[
                    _structured_response_payload(
                        sermon_arc="climax",
                        rhetorical_goal="moving congregation toward repentance",
                        primary_themes=["repentance", "grace"],
                        supporting_themes=["mercy"],
                        short_summary="A short summary.",
                        long_summary="A longer review summary.",
                        illustration_state={"active": True, "subject": "the prodigal son"},
                        confidence=0.7,
                        refresh_reason_echo="exhortation_started",
                    ),
                ],
            )
            tracker.request_refresh(reason="passage_change", strength="strong")
            await _wait_for_in_flight(tracker)
            memory = tracker.get_memory()
            assert memory.sermon_state.sermon_arc == "climax"
            assert memory.sermon_state.rhetorical_goal == "moving congregation toward repentance"
            assert memory.theme_state.primary_themes == ["repentance", "grace"]
            assert memory.theme_state.supporting_themes == ["mercy"]
            assert memory.illustration_state.active is True
            assert memory.illustration_state.subject == "the prodigal son"
            assert memory.summary_state.short_summary == "A short summary."
            assert memory.summary_state.long_summary == "A longer review summary."
            assert memory.summary_state.refresh_reason == "passage_change"
            assert memory.sermon_state.confidence == pytest.approx(0.7)

        run(_())

    def test_explicit_active_passage_is_preserved_against_summary_inference(self):
        async def _():
            tracker, _msgs = _make_tracker(
                raw_responses=[
                    _structured_response_payload(
                        active_passage_override={
                            "reference": "Some Book 5:1",
                            "canonical_english": "made up text",
                        },
                    ),
                ],
            )
            tracker.set_active_passage("1 John 1:5-7", "God is light...")
            tracker.request_refresh(reason="passage_change", strength="strong")
            await _wait_for_in_flight(tracker)
            ap = tracker.get_memory().active_passage
            # The verse-detection-sourced passage MUST not be overwritten by
            # a summary-inferred override.
            assert ap.reference == "1 John 1:5-7"
            assert ap.source == "verse_detection"

        run(_())

    def test_malformed_json_falls_back_to_text_summary(self):
        async def _():
            tracker, _msgs = _make_tracker(
                raw_responses=[
                    "this is not JSON it is just prose about the sermon.",
                ],
            )
            tracker.request_refresh(reason="passage_change", strength="strong")
            await _wait_for_in_flight(tracker)
            memory = tracker.get_memory()
            assert memory.summary_state.short_summary.startswith("this is not JSON")
            assert tracker.metrics["summary_parse_failure_count"] == 1

        run(_())

    def test_markdown_fenced_json_is_parsed(self):
        async def _():
            tracker, _msgs = _make_tracker(
                raw_responses=[
                    "```json\n" + _structured_response_payload(short_summary="ok") + "\n```",
                ],
            )
            tracker.request_refresh(reason="passage_change", strength="strong")
            await _wait_for_in_flight(tracker)
            assert tracker.get_memory().summary_state.short_summary == "ok"

        run(_())


# ---------------------------------------------------------------------------
# Phase 4 prompt assembly
# ---------------------------------------------------------------------------

class TestPromptBlocks:
    def test_prompt_blocks_carry_active_passage_and_themes(self):
        tracker, _ = _make_tracker(sermon_topic="The light of Christ")
        tracker.set_active_passage("1 John 1:5-7", "God is light...")
        tracker._memory.sermon_state = SermonStateMemory(
            current_mode="exposition",
            sermon_arc="development",
            rhetorical_goal="explaining the passage",
            confidence=0.5,
            updated_at_ts=1,
        )
        tracker._memory.theme_state.primary_themes = ["light", "fellowship"]
        text = tracker.get_prompt_blocks_text()
        assert "[ACTIVE PASSAGE]\n1 John 1:5-7 — God is light..." in text
        assert "[SERMON MODE]\nexposition" in text
        assert "[SERMON ARC]\ndevelopment" in text
        assert "[RHETORICAL GOAL]\nexplaining the passage" in text
        assert "[PRIMARY THEMES]\nlight, fellowship" in text

    def test_illustration_state_only_emitted_when_active(self):
        tracker, _ = _make_tracker()
        text_inactive = tracker.get_prompt_blocks_text()
        assert "[ILLUSTRATION STATE]" not in text_inactive

        tracker._memory.illustration_state.active = True
        tracker._memory.illustration_state.subject = "the prodigal son"
        text_active = tracker.get_prompt_blocks_text()
        assert "[ILLUSTRATION STATE]\nactive — the prodigal son" in text_active


# ---------------------------------------------------------------------------
# Backward compatibility surface
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_get_context_returns_topic_seed_string(self):
        tracker, _ = _make_tracker(sermon_topic="Walking in the light")
        ctx = tracker.get_context()
        assert "Walking in the light" in ctx

    def test_get_context_obj_returns_sermon_context(self):
        tracker, _ = _make_tracker(sermon_topic="seed")
        view = tracker.get_context_obj()
        assert view.summary == "seed"
        assert view.current_mode == "exposition"


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

class TestObservability:
    def test_topic_state_event_is_emitted_on_apply(self):
        async def _():
            events: list[dict] = []

            async def capture(event):
                events.append(event)

            tracker, _msgs = _make_tracker(
                raw_responses=[_structured_response_payload(short_summary="snap")],
                on_observability_event=capture,
            )
            tracker.request_refresh(reason="passage_change", strength="strong")
            await _wait_for_in_flight(tracker)
            stages = [e.get("trace_stage") for e in events]
            assert "summary.prompt" in stages
            assert "summary.response" in stages
            assert "summary.applied" in stages
            assert "topic_state" in stages
            topic_state = next(e for e in events if e["trace_stage"] == "topic_state")
            memory = topic_state["data"]["memory"]
            assert memory["summary_state"]["short_summary"] == "snap"

        run(_())
