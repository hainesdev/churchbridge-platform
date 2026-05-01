from tests.benchmark.run_pipeline_test import (
    DEFAULT_DURATION_S,
    build_arg_parser,
    filter_srt,
    generate_run_id,
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


def test_generate_run_id_includes_audio_context_and_unique_suffix():
    first = generate_run_id("tests/audio/1", 30.0)
    second = generate_run_id("tests/audio/2", 30.0)

    assert "-1-30000-" in first
    assert "-2-30000-" in second
    assert first != second


def test_build_arg_parser_mentions_concurrent_run_isolation():
    help_text = build_arg_parser().format_help()

    assert "Concurrent runs" in help_text
    assert "different ports" in help_text
    assert "--results-root" in help_text
    assert "--stt-model" in help_text
    assert "--stt-language" in help_text
    assert "--stt-alt-language" in help_text
    assert "--utterance-end-ms" in help_text
