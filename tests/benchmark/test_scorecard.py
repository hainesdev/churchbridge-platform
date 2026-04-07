from pathlib import Path

from tests.benchmark.scorecard import (
    _check_ts_ordering,
    _correction_latencies,
    _translation_latencies,
    scorecard_from_file,
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
