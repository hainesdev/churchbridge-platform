from tests.benchmark.run_pipeline_test import (
    DEFAULT_DURATION_S,
    build_arg_parser,
    filter_srt,
    find_audio_files,
    find_primary_audio_file,
    generate_run_id,
    resolve_ws_base_url,
    resolve_duration,
    resolve_run_namespace,
)
from tests.benchmark.run_capture_pipeline_test import build_capture_result as build_capture_only_result, build_arg_parser as build_capture_arg_parser
from pathlib import Path


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


def test_resolve_ws_base_url_from_https_server():
    assert resolve_ws_base_url(server_base_url="https://churchbridge.dhaines.dev") == "wss://churchbridge.dhaines.dev"


def test_resolve_ws_base_url_from_explicit_ws_server():
    assert resolve_ws_base_url(server_base_url="ws://localhost:8000/") == "ws://localhost:8000"


def test_resolve_ws_base_url_from_local_port():
    assert resolve_ws_base_url(server_port=8799) == "ws://localhost:8799"


def test_build_arg_parser_mentions_concurrent_run_isolation():
    help_text = build_arg_parser().format_help()

    assert "Concurrent runs" in help_text
    assert "different ports" in help_text
    assert "--results-root" in help_text
    assert "--server-base-url" in help_text
    assert "--client-profile" in help_text
    assert "--stt-model" in help_text
    assert "--stt-language" in help_text
    assert "--stt-alt-language" in help_text
    assert "--utterance-end-ms" in help_text


def test_find_primary_audio_file_accepts_audio_only_fixture():
    audio = find_primary_audio_file(Path("tests/audio/3"))
    assert audio.name == "Bilingual Prayer Service.mp3"


def test_find_audio_files_still_requires_srt_for_scored_benchmark():
    try:
        find_audio_files(Path("tests/audio/3"))
    except FileNotFoundError as exc:
        assert "No .srt found" in str(exc)
    else:
        raise AssertionError("Expected scored benchmark audio discovery to require an SRT")


def test_capture_arg_parser_supports_diarization_flags():
    args = build_capture_arg_parser().parse_args([
        "--diarization-enabled",
        "--diarization-min-speakers", "2",
        "--diarization-max-speakers", "4",
    ])

    assert args.diarization_enabled is True
    assert args.diarization_min_speakers == 2
    assert args.diarization_max_speakers == 4


def test_capture_result_summarizes_speaker_and_language_modes():
    result = build_capture_only_result(
        run_id="capture-test",
        messages=[
            {
                "type": "stt_final",
                "text": "Welcome iglesia",
                "detected_language": "en-US",
                "detected_languages": ["en-US"],
                "segment_language_mode": "english",
                "speaker_count": 1,
                "speaker_switch_count": 0,
                "mixed_speaker_segment": False,
                "_elapsed_s": 1.1,
            },
            {
                "type": "final_spanish",
                "text": "Bienvenidos todos",
                "detected_language": "es-US",
                "detected_languages": ["es-US"],
                "segment_language_mode": "mixed",
                "speaker_count": 2,
                "speaker_switch_count": 1,
                "mixed_speaker_segment": True,
                "dominant_speaker": 1,
                "_elapsed_s": 2.2,
            },
            {
                "type": "feed_commit",
                "spanish": "Bienvenidos todos",
                "english": "Welcome everyone",
                "segment_id": 5,
                "ts": 5000,
                "source": "google",
                "_elapsed_s": 3.3,
            },
        ],
        wall_s=5.5,
        audio_path=Path("tests/audio/3/Bilingual Prayer Service.mp3"),
        duration_s=30.0,
        start_offset_s=0.0,
        audio_dir="tests/audio/3",
        note="",
        stt_config={"diarizationEnabled": True},
        transport={},
        client_profile="benchmark",
    )

    assert result["summary"]["detected_language_counts"] == {"en-US": 1, "es-US": 1}
    assert result["summary"]["segment_language_mode_counts"] == {"english": 1, "mixed": 1}
    assert result["summary"]["speaker_count_distribution"] == {"1": 1, "2": 1}
    assert result["summary"]["speaker_switch_segment_count"] == 1
    assert result["summary"]["mixed_speaker_segment_count"] == 1
    assert result["summary"]["total_speaker_switch_count"] == 1
    assert result["summary"]["dominant_speaker_counts"] == {"1": 1}
