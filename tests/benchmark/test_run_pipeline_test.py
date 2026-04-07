from tests.benchmark.run_pipeline_test import (
    DEFAULT_DURATION_S,
    filter_srt,
    resolve_duration,
    resolve_run_namespace,
)


def test_resolve_duration_allows_default_length():
    assert resolve_duration(DEFAULT_DURATION_S, allow_long_duration=False) == DEFAULT_DURATION_S


def test_resolve_duration_blocks_unapproved_long_runs():
    try:
        resolve_duration(DEFAULT_DURATION_S + 5, allow_long_duration=False)
    except ValueError as exc:
        assert "--allow-long-duration" in str(exc)
    else:
        raise AssertionError("Expected long duration without opt-in to fail")


def test_resolve_duration_allows_explicit_long_run_opt_in():
    assert resolve_duration(85.0, allow_long_duration=True) == 85.0


def test_filter_srt_honors_start_offset_window():
    segments = [
        {"start": 5.0, "text": "intro"},
        {"start": 30.0, "text": "window start"},
        {"start": 45.0, "text": "inside"},
        {"start": 61.0, "text": "outside"},
    ]

    kept = filter_srt(segments, duration_s=30.0, start_offset_s=30.0)
    assert [item["text"] for item in kept] == ["window start", "inside"]


def test_resolve_run_namespace_uses_audio_name_and_offset_when_unspecified():
    value = resolve_run_namespace("tests/audio/2", 30.5, None)
    assert value == "pipeline_test_2_30500"


def test_resolve_run_namespace_preserves_explicit_church_id():
    assert resolve_run_namespace("tests/audio/1", 0.0, "bench-a") == "bench-a"
