from pathlib import Path

from tests.benchmark.scorecard import (
    _check_ts_ordering,
    _correction_latencies,
    _translation_latencies,
    build_scorecard,
    scorecard_from_file,
)
from tests.benchmark.trajectory import (
    _trend_label,
    _clip_duration_regime_changed,
    LOWER_IS_BETTER,
)
from tests.benchmark.trajectory import (
    _trend_label,
    _clip_duration_regime_changed,
    LOWER_IS_BETTER,
)


RUN_FIXTURE = Path("tests/benchmark/results/1/pipeline/2026-04-07T15-30-27Z.json")


def test_ordering_check_ignores_downstream_events_with_reused_ts():
    committed = [
        {"type": "final_spanish", "ts": 1000, "_elapsed_s": 1.0},
        {"type": "final_spanish", "ts": 2000, "_elapsed_s": 2.0},
        {"type": "final_spanish", "ts": 3000, "_elapsed_s": 3.0},
    ]

    assert _check_ts_ordering(committed) == 0


def test_latency_pairing_accepts_raw_event_elapsed_field_names():
    committed = [
        {"ts": 1000, "_elapsed_s": 1.5},
        {"ts": 2000, "_elapsed_s": 4.0},
    ]
    translations = [
        {"ts": 1000, "_elapsed_s": 2.0},
        {"ts": 2000, "_elapsed_s": 4.4},
    ]
    corrections = [
        {"ts": 1000, "_elapsed_s": 2.3},
        {"ts": 2000, "_elapsed_s": 5.0},
    ]

    assert _translation_latencies(committed, translations) == [(1.5, 2.0), (4.0, 4.4)]
    assert _correction_latencies(translations, corrections) == [(2.0, 2.3), (4.4, 5.0)]


def test_scorecard_from_real_run_populates_latency_and_clears_false_ordering():
    scorecard = scorecard_from_file(RUN_FIXTURE)

    assert scorecard["behavioral"]["out_of_order_event_count"] == 0
    assert scorecard["latency"]["avg_translation_latency_s"] == 0.287
    assert scorecard["latency"]["avg_llm_correction_latency_s"] == 3.706


def _make_result(seg_metadata=None, extra_msgs=None):
    """Minimal result dict for scorecard tests."""
    base_msgs = [
        {"type": "final_spanish", "ts": 1000, "text": "hola", "_elapsed_s": 1.0},
        {"type": "translation", "ts": 1000, "english": "hello", "_elapsed_s": 1.5},
    ]
    if extra_msgs:
        base_msgs.extend(extra_msgs)
    return {
        "run_id": "test-run",
        "layers": {
            "raw_deepgram": {},
            "committed_sentences": {"sentence_count": 1},
            "translations": [{"ts": 1000, "english": "hello", "elapsed_s": 1.5}],
            "llm_corrections": [],
            "verse_events": [],
            "segment_metadata": seg_metadata or [],
            "mode_changes": [],
        },
        "all_messages": base_msgs,
    }


def test_deferred_release_count_uses_pending_completion():
    """deferred_release_count counts segment_metadata entries with pending_completion=True."""
    result = _make_result(
        seg_metadata=[
            {"type": "segment_metadata", "ts": 1000, "pending_completion": True, "_elapsed_s": 2.0},
        ]
    )
    scorecard = build_scorecard(result)
    assert scorecard["latency"]["deferred_release_count"] == 1
    assert scorecard["latency"]["deferred_release_timeout_count"] == 0


def test_deferred_release_timeout_count_fires_when_translation_update_arrives():
    """deferred_release_timeout_count increments when timeout fires (translation_update, no merge)."""
    result = _make_result(
        seg_metadata=[
            {"type": "segment_metadata", "ts": 1000, "pending_completion": True, "_elapsed_s": 2.0},
        ],
        extra_msgs=[
            {"type": "translation_update", "ts": 1000, "english": "hello world", "_elapsed_s": 8.0},
        ],
    )
    result["layers"]["llm_corrections"] = [
        {"type": "translation_update", "ts": 1000, "english": "hello world", "elapsed_s": 8.0}
    ]
    scorecard = build_scorecard(result)
    assert scorecard["latency"]["deferred_release_count"] == 1
    assert scorecard["latency"]["deferred_release_timeout_count"] == 1


def test_deferred_release_timeout_not_counted_when_caption_merge_absorbs():
    """When a caption_merge absorbs the ts, it is not counted as a timeout."""
    result = _make_result(
        seg_metadata=[
            {"type": "segment_metadata", "ts": 1000, "pending_completion": True, "_elapsed_s": 2.0},
        ],
        extra_msgs=[
            {"type": "caption_merge", "ts_absorb": 1000, "ts_keep": 500, "_elapsed_s": 4.0},
        ],
    )
    scorecard = build_scorecard(result)
    assert scorecard["latency"]["deferred_release_count"] == 1
    assert scorecard["latency"]["deferred_release_timeout_count"] == 0


def test_caption_merge_count_uses_caption_merge_events():
    """caption_merge_count counts caption_merge events in all_messages, not a seg_metadata flag."""
    result = _make_result(
        extra_msgs=[
            {"type": "caption_merge", "ts_absorb": 1000, "ts_keep": 500, "_elapsed_s": 3.0},
            {"type": "caption_merge", "ts_absorb": 2000, "ts_keep": 500, "_elapsed_s": 5.0},
        ],
    )
    scorecard = build_scorecard(result)
    assert scorecard["latency"]["caption_merge_count"] == 2


def test_client_visible_rewrite_count_only_counts_actual_text_changes():
    result = {
        "run_id": "test-run",
        "layers": {
            "raw_deepgram": {},
            "committed_sentences": {"sentence_count": 1},
            "translations": [{"ts": 1000, "english": "hello", "elapsed_s": 1.0}],
            "llm_corrections": [
                {"type": "translation_update", "ts": 1000, "english": "hello", "elapsed_s": 1.5},
                {"type": "translation_update", "ts": 1000, "english": "hello there", "elapsed_s": 2.0},
                {"type": "translation_update", "ts": 2000, "english": "orphan", "elapsed_s": 2.5},
            ],
            "verse_events": [],
            "segment_metadata": [],
            "mode_changes": [],
        },
        "all_messages": [
            {"type": "final_spanish", "ts": 1000, "text": "hola"},
            {"type": "translation", "ts": 1000, "english": "hello", "_elapsed_s": 1.0},
            {"type": "translation_update", "ts": 1000, "english": "hello", "_elapsed_s": 1.5},
            {"type": "translation_update", "ts": 1000, "english": "hello there", "_elapsed_s": 2.0},
            {"type": "translation_update", "ts": 2000, "english": "orphan", "_elapsed_s": 2.5},
        ],
    }

    scorecard = build_scorecard(result)

    assert scorecard["accuracy"]["llm_correction_count"] == 3
    assert scorecard["behavioral"]["orphan_correction_count"] == 1
    assert scorecard["behavioral"]["client_visible_rewrite_count"] == 1


# ---------------------------------------------------------------------------
# Trajectory: LOWER_IS_BETTER completeness
# ---------------------------------------------------------------------------

class TestLowerIsBetterCompleteness:
    def test_time_to_first_translation_in_lower_is_better(self):
        """A faster first translation should be treated as an improvement."""
        assert "time_to_first_translation_s" in LOWER_IS_BETTER

    def test_time_to_first_committed_sentence_in_lower_is_better(self):
        """A faster first committed sentence should be treated as an improvement."""
        assert "time_to_first_committed_sentence_s" in LOWER_IS_BETTER

    def test_time_to_first_llm_correction_in_lower_is_better(self):
        """A faster first LLM correction should be treated as an improvement."""
        assert "time_to_first_llm_correction_s" in LOWER_IS_BETTER

    def test_decreasing_time_to_first_translation_labeled_improved(self):
        """Values decreasing over 3 runs → improved (lower latency is better)."""
        values = [8.64, 8.594, 8.359]
        assert _trend_label(values, "time_to_first_translation_s") == "improved"

    def test_increasing_time_to_first_translation_labeled_regressed(self):
        """Values increasing over 3 runs → regressed (higher latency is worse)."""
        values = [8.0, 8.3, 9.1]
        assert _trend_label(values, "time_to_first_translation_s") == "regressed"

    def test_decreasing_wer_labeled_improved(self):
        """Confirming that wer_committed_pct remains in LOWER_IS_BETTER."""
        values = [20.0, 18.0, 15.0]
        assert _trend_label(values, "wer_committed_pct") == "improved"


# ---------------------------------------------------------------------------
# Trajectory: clip duration regime change detection
# ---------------------------------------------------------------------------

class TestClipDurationRegimeChange:
    def test_no_change_when_durations_consistent(self):
        """Same clip duration across runs → no regime change."""
        assert not _clip_duration_regime_changed([85.0, 85.0, 85.0])

    def test_detects_large_drop_from_85s_to_30s(self):
        """30s clip after two 85s clips is a >40% change → regime change."""
        assert _clip_duration_regime_changed([85.0, 85.0, 30.0])

    def test_no_false_positive_for_small_variation(self):
        """Small variation (28s, 30s, 32s clips) is within threshold."""
        assert not _clip_duration_regime_changed([28.0, 30.0, 32.0])

    def test_single_run_never_triggers(self):
        """With only one run there's nothing to compare against."""
        assert not _clip_duration_regime_changed([85.0])

    def test_none_values_skipped(self):
        """None entries (missing clip_duration_s) are ignored."""
        assert not _clip_duration_regime_changed([85.0, None, 85.0])

    def test_detects_large_increase(self):
        """Large increase in clip duration also counts as a regime change."""
        assert _clip_duration_regime_changed([30.0, 30.0, 85.0])
