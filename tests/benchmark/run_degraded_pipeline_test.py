#!/usr/bin/env python3
"""
ChurchBridge AI — Degraded Pipeline Regression Test
===================================================
Streams deterministic echo/noise degraded audio windows through the existing
live server pipeline. This keeps the current replay benchmark flow but adds a
repeatable degradation layer for robustness tuning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from tests.benchmark.degradations import (  # noqa: E402
    ECHO_PROFILES,
    MATRIX_PRESETS,
    NOISE_TYPES,
    DegradationSpec,
    apply_degradation,
    build_single_spec,
    sanitize_case_label,
    standard_matrix,
)
from tests.benchmark.llm_translation_quality import evaluate_translation_quality  # noqa: E402
from tests.benchmark.run_pipeline_test import (  # noqa: E402
    DEFAULT_DURATION_S,
    ROOT as BENCHMARK_ROOT,
    build_result,
    clip_audio,
    filter_srt,
    find_audio_files,
    generate_run_id,
    get_git_commit,
    print_report,
    resolve_duration,
    resolve_run_namespace,
    run_pipeline,
    wait_for_server,
)
from tests.benchmark.run_benchmark import parse_srt  # noqa: E402
from tests.benchmark.scorecard import generate_scorecard  # noqa: E402
from tests.benchmark.storage import (  # noqa: E402
    append_history_row,
    pipeline_dir_for,
    regime_cycle_log_path,
    regime_report_path,
    resolve_results_root,
    save_run_result,
)
from tests.benchmark.orchestrator import run_evaluation_cycle  # noqa: E402

SERVER_PORT = 8799


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ChurchBridge AI — Degraded Pipeline Regression Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--audio-dir", default="tests/audio/1",
                        help="Directory containing the clean .mp3/.wav and .srt pair")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                        help=f"Seconds of audio to test (default: {DEFAULT_DURATION_S:g})")
    parser.add_argument("--start-offset", type=float, default=0.0,
                        help="Start offset in seconds within the source audio")
    parser.add_argument("--allow-long-duration", action="store_true",
                        help="Allow durations above the default live-test limit")
    parser.add_argument("--port", type=int, default=SERVER_PORT,
                        help=f"Server port for this benchmark run (default: {SERVER_PORT})")
    parser.add_argument("--church-id", default="",
                        help="Optional namespace prefix for these degraded runs")
    parser.add_argument("--results-root", default="tests/benchmark/results/degraded",
                        help="Results root for degraded artifacts")
    parser.add_argument("--capture-only", action="store_true",
                        help="Save raw run JSON only; skip evaluation artifacts")
    parser.add_argument("--note", default="",
                        help="Free-text note recorded with each run")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM interpretation")
    parser.add_argument("--translation-quality", action="store_true",
                        help="Run translation-quality evaluation after each degraded run")
    parser.add_argument("--tq-chunk-size", type=int, default=5,
                        help="Sentences per chunk for translation-quality evaluation")
    parser.add_argument("--condition", default="matrix",
                        choices=["clean", "echo", "noise", "echo_noise", "matrix"],
                        help="Degradation condition to run, or matrix to run a preset suite")
    parser.add_argument("--echo-profile", default="medium",
                        choices=list(ECHO_PROFILES),
                        help="Echo profile for echo-bearing conditions")
    parser.add_argument("--noise-type", default="hvac",
                        choices=list(NOISE_TYPES),
                        help="Noise source for noise-bearing conditions")
    parser.add_argument("--snr-db", type=float, default=10.0,
                        help="Target signal-to-noise ratio in dB for noise-bearing conditions")
    parser.add_argument("--seed", type=int, default=7,
                        help="Deterministic random seed for noise generation")
    parser.add_argument("--matrix-preset", default="standard",
                        choices=list(MATRIX_PRESETS),
                        help="Preset matrix used when --condition matrix")
    parser.add_argument("--stt-model", default="",
                        help="Override the Google Speech model for this run")
    parser.add_argument("--stt-language", default="",
                        help="Override the primary Google Speech language code for this run")
    parser.add_argument("--stt-alt-language", action="append", default=[],
                        help="Optional additional language code for locale/code-switch experiments; may be passed multiple times")
    parser.add_argument("--stt-location", default="",
                        help="Google Speech location override (for example: us)")
    parser.add_argument("--stt-recognizer", default="",
                        help="Google Speech recognizer override (resource name or '_' for inline config)")
    parser.add_argument("--utterance-end-ms", type=int, default=2000,
                        help="Google Speech utterance-end / speech-end timeout in milliseconds")
    parser.add_argument("--confidence-hold-threshold", type=float, default=0.72,
                        help="Average word confidence below which a final gets an extra buffer hold")
    parser.add_argument("--low-confidence-hold-secs", type=float, default=2.5,
                        help="Extra hold duration applied to low-confidence STT finals")
    return parser


def resolve_specs(args: argparse.Namespace) -> list[DegradationSpec]:
    if args.condition == "matrix":
        return standard_matrix(seed=args.seed, preset=args.matrix_preset)
    return [
        build_single_spec(
            condition=args.condition,
            echo_profile=args.echo_profile,
            noise_type=args.noise_type,
            snr_db=args.snr_db,
            seed=args.seed,
        )
    ]


def _case_note(base_note: str, spec: DegradationSpec) -> str:
    suffix = f"degradation={spec.case_id()}"
    return f"{base_note} | {suffix}" if base_note else suffix


async def _evaluate_and_persist(
    result: dict,
    pipeline_dir: Path,
    spec: DegradationSpec,
    args: argparse.Namespace,
    results_root: Path,
) -> None:
    run_file = save_run_result(result, pipeline_dir)
    print(f"\nSaved  : {run_file}")
    print_report(result)

    if args.capture_only:
        print("Capture-only mode: skipping history append and evaluation.")
        return

    history_file = append_history_row(result, pipeline_dir)
    history = json.loads(history_file.read_text(encoding="utf-8"))
    print(f"History: {history_file}  ({len(history)} runs)")

    scorecard_path = pipeline_dir / "scorecards" / f"{result['run_id']}.json"
    scorecard = generate_scorecard(result, scorecard_path)

    if args.translation_quality:
        evaluate_translation_quality(result, pipeline_dir, chunk_size=args.tq_chunk_size)
        scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))

    run_evaluation_cycle(
        result,
        pipeline_dir,
        use_llm=not args.no_llm,
        cycle_log_path=regime_cycle_log_path(results_root),
        report_path=regime_report_path(results_root),
        precomputed_scorecard=scorecard,
    )


async def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    duration_s = resolve_duration(args.duration, args.allow_long_duration)
    start_offset_s = args.start_offset
    results_root = resolve_results_root(args.results_root)
    specs = resolve_specs(args)
    stt_config = {
        "utteranceEndMs": args.utterance_end_ms,
        "confidenceHoldThreshold": args.confidence_hold_threshold,
        "lowConfidenceHoldSecs": args.low_confidence_hold_secs,
    }
    if args.stt_model:
        stt_config["model"] = args.stt_model
    language_codes = []
    if args.stt_language:
        language_codes.append(args.stt_language)
    language_codes.extend(code for code in args.stt_alt_language if code)
    if language_codes:
        stt_config["languageCodes"] = language_codes
        stt_config["language"] = language_codes[0]
    if args.stt_location:
        stt_config["location"] = args.stt_location
    if args.stt_recognizer:
        stt_config["recognizer"] = args.stt_recognizer

    audio_dir = BENCHMARK_ROOT / args.audio_dir
    mp3_path, srt_path = find_audio_files(audio_dir)
    base_samples, sample_rate = clip_audio(mp3_path, duration_s, start_offset_s=start_offset_s)

    all_srt = parse_srt(srt_path)
    ref_segs = filter_srt(all_srt, duration_s, start_offset_s=start_offset_s)
    ref_text = " ".join(s["text"] for s in ref_segs)

    print(f"Audio : {mp3_path.name}")
    print(f"SRT   : {srt_path.name}")
    print(f"Port  : {args.port}")
    print(f"Cases : {len(specs)}")
    print(
        f"SRT   : {len(ref_segs)} segments, {len(ref_text.split())} words in "
        f"window {start_offset_s}s–{start_offset_s + duration_s}s\n"
    )

    print(f"Starting server on port {args.port}...")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.main:app",
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=os.environ.copy(),
    )

    try:
        await wait_for_server(args.port)
        print("Server ready.\n")

        for index, spec in enumerate(specs, start=1):
            case_label = sanitize_case_label(args.audio_dir, spec)
            church_scope = resolve_run_namespace(case_label, start_offset_s, args.church_id or None)
            run_id = generate_run_id(case_label, start_offset_s)
            degraded_samples = apply_degradation(base_samples, sample_rate, spec)

            print(f"[{index}/{len(specs)}] Running {case_label}")
            print(f"  Degradation: {spec.metadata()}")
            messages, wall_s, transport = await run_pipeline(
                degraded_samples,
                sample_rate,
                church_scope,
                server_port=args.port,
                stt_config=stt_config,
            )

            result = build_result(
                run_id=run_id,
                messages=messages,
                wall_s=wall_s,
                ref_text=ref_text,
                ref_segments=ref_segs,
                mp3_path=mp3_path,
                srt_path=srt_path,
                duration_s=duration_s,
                start_offset_s=start_offset_s,
                audio_dir=case_label,
                note=_case_note(args.note, spec),
                transport=transport,
            )
            result["benchmark_variant"] = "degraded_replay"
            result["source_audio_dir"] = args.audio_dir
            result["degradation"] = spec.metadata()
            result["git_commit"] = get_git_commit()

            pipeline_dir = pipeline_dir_for(case_label, results_root)
            await _evaluate_and_persist(result, pipeline_dir, spec, args, results_root)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
