import json
from pathlib import Path

from tests.benchmark.review import _action_recommendation, _flat
from tests.benchmark.trajectory import compute_trajectory


def _scorecard(
    run_id: str,
    offset: float,
    wer: float = 15.0,
    quality_rating: float | None = None,
) -> dict:
    payload = {
        "run_id": run_id,
        "audio_dir": "tests/audio/2",
        "clip_start_offset_s": offset,
        "clip_duration_s": 5.0,
        "accuracy": {
            "wer_raw_pct": wer,
            "wer_committed_pct": wer,
            "theological_term_recall": 1.0,
            "theological_term_precision": 1.0,
            "translation_count": 4,
        },
        "latency": {
            "time_to_first_translation_s": 1.0,
            "time_to_first_committed_sentence_s": 1.0,
            "wall_time_s": 5.2,
        },
        "behavioral": {
            "out_of_order_event_count": 0,
            "orphan_correction_count": 0,
            "duplicate_commit_count": 0,
            "stale_correction_suppression_count": 0,
            "incorrect_merge_suspect_count": 0,
            "display_ready_violation_count": 0,
            "fragment_leak_count": 0,
            "mode_flip_count": 0,
            "client_visible_rewrite_count": 0,
        },
    }
    if quality_rating is not None:
        payload["quality"] = {"translation_quality_rating": quality_rating}
    return payload


def _write_scorecards(pipeline_dir: Path, scorecards: list[dict]) -> None:
    scorecard_dir = pipeline_dir / "scorecards"
    scorecard_dir.mkdir(parents=True)
    for item in scorecards:
        (scorecard_dir / f"{item['run_id']}.json").write_text(
            json.dumps(item), encoding="utf-8"
        )


def test_compute_trajectory_tracks_offset_coverage(tmp_path):
    pipeline_dir = tmp_path / "2" / "pipeline"
    _write_scorecards(
        pipeline_dir,
        [
            _scorecard("run-a", 0.0),
            _scorecard("run-b", 30.0),
            _scorecard("run-c", 60.0, quality_rating=4.0),
        ],
    )

    trajectory = compute_trajectory(pipeline_dir)
    coverage = trajectory["coverage"]

    assert coverage["distinct_clip_offsets_s"] == [0.0, 30.0, 60.0]
    assert coverage["has_zero_offset_baseline"] is True
    assert coverage["has_nonzero_offset_window"] is True
    assert coverage["quality_eval_run_count"] == 1


def test_action_recommendation_blocks_promote_for_incomplete_staggered_regime():
    scorecard = _scorecard("run-c", 60.0)
    trajectory = {
        "n_runs": 3,
        "coverage": {
            "has_zero_offset_baseline": True,
            "has_nonzero_offset_window": True,
        },
        "metrics": {
            "wer_committed_pct": {"trend": "improved", "delta_vs_prev": -1.0},
            "theological_term_recall": {"trend": "flat", "delta_vs_prev": 0.0},
            "theological_term_precision": {"trend": "flat", "delta_vs_prev": 0.0},
            "translation_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "time_to_first_translation_s": {"trend": "flat", "delta_vs_prev": 0.0},
            "time_to_first_committed_sentence_s": {"trend": "flat", "delta_vs_prev": 0.0},
            "out_of_order_event_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "orphan_correction_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "duplicate_commit_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "stale_correction_suppression_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "incorrect_merge_suspect_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "display_ready_violation_count": {"trend": "flat", "delta_vs_prev": 0.0},
        },
    }

    action, reasons = _action_recommendation(
        scorecard,
        trajectory,
        _flat(scorecard),
        {
            "results_root_name": "staggered",
            "staggered_set_count": 1,
            "staggered_complete_set_count": 1,
        },
    )

    assert action == "collect_more_runs"
    assert any("not yet complete across benchmark sets" in reason for reason in reasons)


def test_action_recommendation_allows_promote_once_staggered_coverage_is_complete():
    scorecard = _scorecard("run-c", 60.0)
    trajectory = {
        "n_runs": 3,
        "coverage": {
            "has_zero_offset_baseline": True,
            "has_nonzero_offset_window": True,
        },
        "metrics": {
            "wer_committed_pct": {"trend": "improved", "delta_vs_prev": -1.0},
            "theological_term_recall": {"trend": "flat", "delta_vs_prev": 0.0},
            "theological_term_precision": {"trend": "flat", "delta_vs_prev": 0.0},
            "translation_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "time_to_first_translation_s": {"trend": "flat", "delta_vs_prev": 0.0},
            "time_to_first_committed_sentence_s": {"trend": "flat", "delta_vs_prev": 0.0},
            "out_of_order_event_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "orphan_correction_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "duplicate_commit_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "stale_correction_suppression_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "incorrect_merge_suspect_count": {"trend": "flat", "delta_vs_prev": 0.0},
            "display_ready_violation_count": {"trend": "flat", "delta_vs_prev": 0.0},
        },
    }

    action, _ = _action_recommendation(
        scorecard,
        trajectory,
        _flat(scorecard),
        {
            "results_root_name": "staggered",
            "staggered_set_count": 2,
            "staggered_complete_set_count": 2,
        },
    )

    assert action == "promote"
