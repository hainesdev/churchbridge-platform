import json

from tests.benchmark.orchestrator import _write_report


def _trajectory(current_wer: float) -> dict:
    return {
        "n_runs": 2,
        "confidence": "insufficient_data",
        "metrics": {
            "wer_committed_pct": {
                "current": current_wer,
                "delta_vs_prev": 0.5,
                "trend": "insufficient_data",
            },
            "wer_raw_pct": {
                "current": current_wer,
                "delta_vs_prev": 0.5,
                "trend": "insufficient_data",
            },
            "out_of_order_event_count": {
                "current": 0,
                "delta_vs_prev": 0,
                "trend": "insufficient_data",
            },
            "orphan_correction_count": {
                "current": 0,
                "delta_vs_prev": 0,
                "trend": "insufficient_data",
            },
            "fragment_leak_count": {
                "current": 0,
                "delta_vs_prev": 0,
                "trend": "insufficient_data",
            },
            "duplicate_commit_count": {
                "current": 0,
                "delta_vs_prev": 0,
                "trend": "insufficient_data",
            },
            "time_to_first_translation_s": {
                "current": 10.5,
                "delta_vs_prev": -0.1,
                "trend": "insufficient_data",
            },
            "wall_time_s": {
                "current": 18.9,
                "delta_vs_prev": -0.1,
                "trend": "insufficient_data",
            },
        },
    }


def test_write_report_aggregates_all_sets_for_staggered_regime(tmp_path):
    results_root = tmp_path / "staggered"
    set_one = results_root / "1" / "pipeline"
    set_two = results_root / "2" / "pipeline"
    set_one.mkdir(parents=True)
    set_two.mkdir(parents=True)

    (set_one / "trajectory.json").write_text(json.dumps(_trajectory(14.9)), encoding="utf-8")
    (set_two / "trajectory.json").write_text(json.dumps(_trajectory(23.1)), encoding="utf-8")

    report_path = results_root / "SELF_IMPROVEMENT_REPORT.md"
    cycle_log_path = results_root / "cycle_log.json"

    _write_report(
        audio_dir_name="2",
        trajectory=_trajectory(23.1),
        cycle_entry={"cycle_id": "cycle-1", "git_commit": "abc123", "outcome": "pending"},
        action="collect_more_runs",
        llm_analysis=None,
        cycle_log_path=cycle_log_path,
        report_path=report_path,
    )

    report = report_path.read_text(encoding="utf-8")

    assert "## Benchmark Sets" in report
    assert "### Set 1" in report
    assert "### Set 2" in report


def test_write_report_keeps_single_set_legacy_behavior(tmp_path):
    report_path = tmp_path / "SELF_IMPROVEMENT_REPORT.md"

    _write_report(
        audio_dir_name="1",
        trajectory=_trajectory(14.9),
        cycle_entry={"cycle_id": "cycle-1", "git_commit": "abc123", "outcome": "pending"},
        action="collect_more_runs",
        llm_analysis=None,
        cycle_log_path=tmp_path / "cycle_log.json",
        report_path=report_path,
    )

    report = report_path.read_text(encoding="utf-8")

    assert "## Benchmark Set: 1" in report
    assert "## Benchmark Sets" not in report


def test_write_report_uses_conservative_action_across_staggered_sets(tmp_path):
    results_root = tmp_path / "staggered"
    set_one = results_root / "1" / "pipeline"
    set_two = results_root / "2" / "pipeline"
    set_one.mkdir(parents=True)
    set_two.mkdir(parents=True)

    (set_one / "trajectory.json").write_text(json.dumps(_trajectory(14.9)), encoding="utf-8")
    (set_two / "trajectory.json").write_text(json.dumps(_trajectory(23.1)), encoding="utf-8")

    cycle_log_path = results_root / "cycle_log.json"
    cycle_log_path.write_text(
        json.dumps(
            [
                {
                    "cycle_id": "cycle-a",
                    "audio_dir_name": "1",
                    "review_action": "collect_more_runs",
                    "git_commit": "abc123",
                    "outcome": "pending",
                },
                {
                    "cycle_id": "cycle-b",
                    "audio_dir_name": "2",
                    "review_action": "promote",
                    "git_commit": "abc123",
                    "outcome": "pending",
                },
            ]
        ),
        encoding="utf-8",
    )
    report_path = results_root / "SELF_IMPROVEMENT_REPORT.md"

    _write_report(
        audio_dir_name="2",
        trajectory=_trajectory(23.1),
        cycle_entry={"cycle_id": "cycle-1", "git_commit": "abc123", "outcome": "pending"},
        action="promote",
        llm_analysis=None,
        cycle_log_path=cycle_log_path,
        report_path=report_path,
    )

    report = report_path.read_text(encoding="utf-8")
    assert "## Current Action" in report
    assert "**`collect_more_runs`**" in report


def test_write_report_aggregates_all_sets_for_degraded_batch_root(tmp_path):
    results_root = tmp_path / "degraded-validation"
    set_one = results_root / "1__clean" / "pipeline"
    set_two = results_root / "1__echo_medium" / "pipeline"
    set_one.mkdir(parents=True)
    set_two.mkdir(parents=True)

    (set_one / "trajectory.json").write_text(json.dumps(_trajectory(14.9)), encoding="utf-8")
    (set_two / "trajectory.json").write_text(json.dumps(_trajectory(23.1)), encoding="utf-8")

    report_path = results_root / "SELF_IMPROVEMENT_REPORT.md"
    cycle_log_path = results_root / "cycle_log.json"

    _write_report(
        audio_dir_name="1__echo_medium",
        trajectory=_trajectory(23.1),
        cycle_entry={"cycle_id": "cycle-1", "git_commit": "abc123", "outcome": "pending"},
        action="collect_more_runs",
        llm_analysis=None,
        cycle_log_path=cycle_log_path,
        report_path=report_path,
    )

    report = report_path.read_text(encoding="utf-8")

    assert "## Benchmark Sets" in report
    assert "### Set 1__clean" in report
    assert "### Set 1__echo_medium" in report
