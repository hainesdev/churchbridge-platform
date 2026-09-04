#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from tests.benchmark.provider_model_benchmark import (
    build_reference_text,
    resolve_scenarios,
    run_provider_suite,
    summarize_results,
)
from tests.benchmark.run_pipeline_test import clip_audio, find_audio_files, resolve_duration
from tests.benchmark.storage import resolve_results_root
from server.services.stt import STTConfig


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 1 STT baseline comparison across raw, echo, and noise conditions.",
    )
    parser.add_argument("--audio-dir", default="tests/audio/1", help="Directory containing the source audio and SRT pair")
    parser.add_argument("--duration", type=float, default=30.0, help="Clip duration in seconds")
    parser.add_argument("--start-offset", type=float, default=0.0, help="Clip start offset in seconds")
    parser.add_argument("--allow-long-duration", action="store_true", help="Allow runs longer than the default benchmark cap")
    parser.add_argument("--condition", action="append", default=[], choices=["raw", "echo", "noise"], help="Condition(s) to run; may be repeated")
    parser.add_argument("--echo-profile", default="medium", choices=["light", "medium", "heavy"], help="Echo profile when running echo")
    parser.add_argument("--noise-type", default="hvac", choices=["white", "pink", "hvac", "crowd", "street", "babble"], help="Noise source when running noise")
    parser.add_argument("--snr-db", type=float, default=10.0, help="Target SNR in dB for noise")
    parser.add_argument("--seed", type=int, default=7, help="Deterministic seed for degradations")
    parser.add_argument("--results-root", default="tests/benchmark/results/provider-comparison", help="Results root for run artifacts")
    parser.add_argument("--deepgram-model", default="nova-3", help="Deepgram model id")
    parser.add_argument("--chirp-language", default="es-US", help="Primary Chirp 3 language code")
    parser.add_argument("--chirp-alt-language", action="append", default=["en-US"], help="Additional Chirp 3 language code(s)")
    parser.add_argument("--chirp-location", default="us", help="Google Speech location")
    parser.add_argument("--chirp-recognizer", default="_", help="Google Speech recognizer name")
    parser.add_argument("--openai-source-language", default="es", help="OpenAI realtime translate source language")
    parser.add_argument("--openai-target-language", default="es", help="OpenAI realtime translate target language; default stays Spanish for apples-to-apples WER")
    return parser


def _run_id(audio_dir: str, start_offset_s: float) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    return f"{timestamp}-{Path(audio_dir).name}-{int(round(start_offset_s * 1000))}"


async def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    conditions = args.condition or ["raw", "echo", "noise"]
    scenarios = resolve_scenarios(
        conditions,
        echo_profile=args.echo_profile,
        noise_type=args.noise_type,
        snr_db=args.snr_db,
        seed=args.seed,
    )

    duration_s = resolve_duration(args.duration, args.allow_long_duration)
    audio_dir = ROOT / args.audio_dir
    mp3_path, srt_path = find_audio_files(audio_dir)
    samples, sample_rate = clip_audio(mp3_path, duration_s, start_offset_s=args.start_offset)
    reference_text = build_reference_text(srt_path, duration_s=duration_s, start_offset_s=args.start_offset)

    chirp_languages = [args.chirp_language, *(code for code in args.chirp_alt_language if code)]
    chirp_config = STTConfig.from_payload({
        "model": "chirp_3",
        "languageCodes": chirp_languages,
        "location": args.chirp_location,
        "recognizer": args.chirp_recognizer,
    })

    run_id = _run_id(args.audio_dir, args.start_offset)
    results_root = resolve_results_root(args.results_root)
    results_dir = results_root / Path(args.audio_dir).name / run_id
    results_dir.mkdir(parents=True, exist_ok=True)

    all_results = []
    for scenario in scenarios:
        degraded = samples if scenario.label == "raw" else None
        if degraded is None:
            from tests.benchmark.provider_model_benchmark import apply_degradation

            degraded = apply_degradation(samples, sample_rate, scenario.spec)
        condition_results = await run_provider_suite(
            degraded_samples=degraded,
            sample_rate=sample_rate,
            reference_text=reference_text,
            condition=scenario.label,
            deepgram_model=args.deepgram_model,
            chirp_config=chirp_config,
            openai_source_language=args.openai_source_language,
            openai_target_language=args.openai_target_language,
        )
        all_results.extend(condition_results)

    artifact = {
        "run_id": run_id,
        "evaluation_phase": "phase_1_stt_baseline",
        "evaluation_goal": "Compare source-language transcript robustness under raw, echo, and noise as the baseline before translation and interpretation analysis.",
        "audio_dir": args.audio_dir,
        "audio_file": mp3_path.name,
        "srt_file": srt_path.name,
        "clip_duration_s": duration_s,
        "clip_start_offset_s": args.start_offset,
        "conditions": [scenario.label for scenario in scenarios],
        "results": [result.to_dict() for result in all_results],
        "summary": summarize_results(all_results),
    }
    artifact_path = results_dir / "provider-benchmark.json"
    artifact_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Saved: {artifact_path}")
    print()
    for provider, conditions_summary in artifact["summary"].items():
        print(provider)
        for condition, metrics in conditions_summary.items():
            print(
                f"  {condition:<5} role={metrics['evaluation_role']} latency={metrics['latency_s']:.3f}s "
                f"ttft={metrics['time_to_first_text_s']} wer={metrics['wer_pct']} "
                f"words={metrics['transcript_word_count']} ok={metrics['ok']}"
            )


if __name__ == "__main__":
    asyncio.run(main())
