from pathlib import Path

from server.services.session_recorder import BenchmarkCaptureMetadata, SessionRecorder


def test_benchmark_recorder_compacts_long_filenames_and_still_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    recorder = SessionRecorder(
        session_id=1,
        church_id="benchmark-lab",
        benchmark_capture=BenchmarkCaptureMetadata(
            enabled=True,
            benchmark_session_id="session-20260513T-box-fan-high-mic-steering-subtle-dfn3",
            benchmark_run_id=(
                "run-04-1_pr-bullón-por-qué-sientes-que-nada-cambia-en-tu-vida-"
                "spvit79bjds_clip_01-apple_aec_plus_deepfilternet3-auto-dfn3-subtle"
            ),
            benchmark_scenario_id="1_pr-bullón-clip_01",
            benchmark_pipeline_id="apple_aec_plus_deepfilternet3",
            benchmark_capture_label=(
                "1_pr-bullón-por-qué-sientes-que-nada-cambia-en-tu-vida-"
                "spvit79bjds_clip_01-apple_aec_plus_deepfilternet3-auto-dfn3-subtle"
            ),
        ),
    )

    recorder.record_audio(b"\x00\x00" * 1600)
    recorder.record_event("session_start", {"church_id": "benchmark-lab"})
    result = recorder.stop()

    assert result.audio_path is not None
    assert result.events_path is not None
    assert result.metadata_path is not None

    audio_path = Path(result.audio_path)
    events_path = Path(result.events_path)
    metadata_path = Path(result.metadata_path)

    assert audio_path.exists()
    assert events_path.exists()
    assert metadata_path.exists()
    assert len(audio_path.name) < 255
    assert len(events_path.name) < 255
    assert len(metadata_path.name) < 255
