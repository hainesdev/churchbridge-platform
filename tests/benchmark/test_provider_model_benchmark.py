from tests.benchmark.provider_model_benchmark import (
    OpenAIRealtimeTextAccumulator,
    resolve_scenarios,
    resample_mono_pcm16,
    split_pcm16_for_max_seconds,
    summarize_results,
    ProviderRunResult,
    _extract_openai_text_delta,
)


def test_resolve_scenarios_maps_raw_echo_noise() -> None:
    scenarios = resolve_scenarios(
        ["raw", "echo", "noise"],
        echo_profile="medium",
        noise_type="hvac",
        snr_db=10.0,
        seed=7,
    )

    assert [scenario.label for scenario in scenarios] == ["raw", "echo", "noise"]
    assert scenarios[0].spec.condition == "clean"
    assert scenarios[1].spec.condition == "echo"
    assert scenarios[2].spec.condition == "noise"


def test_openai_accumulator_collects_text_and_completion() -> None:
    accumulator = OpenAIRealtimeTextAccumulator()

    accumulator.consume({"type": "session.output_transcript.delta", "delta": "Hola "}, 0.4)
    accumulator.consume({"type": "session.output_transcript.delta", "delta": "mundo"}, 0.6)
    accumulator.consume({"type": "session.output_transcript.done", "transcript": "Hola mundo"}, 0.9)

    assert accumulator.text() == "Hola mundo"
    assert accumulator.first_text_at_s == 0.4
    assert accumulator.completed is True


def test_extract_openai_text_delta_supports_translation_session_events() -> None:
    assert _extract_openai_text_delta({"type": "session.output_transcript.delta", "delta": "Así"}) == "Así"
    assert _extract_openai_text_delta({"type": "session.output_transcript.done", "transcript": "Así vamos"}) == "Así vamos"
    assert _extract_openai_text_delta({"type": "session.output_audio.delta", "delta": "AAA="}) == ""


def test_resample_mono_pcm16_preserves_nonempty_signal() -> None:
    source = [0.0, 0.5, -0.5, 0.25]

    pcm16 = resample_mono_pcm16(source, 8000, 16000)

    assert pcm16.dtype.name == "int16"
    assert len(pcm16) >= len(source)
    assert max(abs(int(value)) for value in pcm16) > 0


def test_split_pcm16_for_max_seconds_chunks_long_audio() -> None:
    samples = [1] * (16000 * 120)

    chunks = split_pcm16_for_max_seconds(samples, sample_rate=16000, max_seconds=55.0)

    assert len(chunks) == 3
    assert len(chunks[0]) == 16000 * 55
    assert len(chunks[1]) == 16000 * 55
    assert len(chunks[2]) == 16000 * 10


def test_summarize_results_groups_by_provider_and_condition() -> None:
    results = [
        ProviderRunResult(
            provider="deepgram",
            model="nova-3",
            condition="raw",
            evaluation_role="primary_stt_baseline",
            evaluation_phase="phase_1_stt_baseline",
            transcript="hola",
            latency_s=1.23,
            time_to_first_text_s=None,
            wer={"score_pct": 12.0},
            transcript_word_count=1,
            metadata={},
        ),
        ProviderRunResult(
            provider="chirp_3",
            model="chirp_3",
            condition="echo",
            evaluation_role="primary_stt_baseline",
            evaluation_phase="phase_1_stt_baseline",
            transcript="hola mundo",
            latency_s=2.34,
            time_to_first_text_s=None,
            wer={"score_pct": 15.0},
            transcript_word_count=2,
            metadata={},
        ),
    ]

    summary = summarize_results(results)

    assert summary["deepgram"]["raw"]["wer_pct"] == 12.0
    assert summary["chirp_3"]["echo"]["transcript_word_count"] == 2
    assert summary["deepgram"]["raw"]["evaluation_phase"] == "phase_1_stt_baseline"
