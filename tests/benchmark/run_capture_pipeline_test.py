#!/usr/bin/env python3
"""
ChurchBridge AI - Capture-Only Pipeline Test
===========================================
Streams a clipped audio window through the live production pipeline and records
all display events without requiring an SRT reference.

Use this for exploratory evaluation of new recordings, bilingual services, or
other audio where we do not yet have a reference transcript. The output is a
raw capture artifact with timing, event counts, and simple summaries, but no
WER or scorecard.
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

from tests.benchmark.run_pipeline_test import (  # noqa: E402
    DEFAULT_DURATION_S,
    SERVER_PORT,
    clip_audio,
    find_primary_audio_file,
    generate_run_id,
    get_git_commit,
    resolve_duration,
    resolve_run_namespace,
    run_pipeline,
    wait_for_server,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ChurchBridge AI - Capture-Only Pipeline Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--audio-dir", default="tests/audio/3",
                        help="Directory containing a .mp3 or .wav file")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                        help=f"Seconds of audio to test (default: {DEFAULT_DURATION_S:g})")
    parser.add_argument("--start-offset", type=float, default=0.0,
                        help="Start offset in seconds within the source audio")
    parser.add_argument("--allow-long-duration", action="store_true",
                        help="Allow durations above the default live-test limit")
    parser.add_argument("--port", type=int, default=SERVER_PORT,
                        help=f"Server port for this benchmark run (default: {SERVER_PORT})")
    parser.add_argument("--server-base-url", default="",
                        help="Optional remote server base URL instead of a local uvicorn server")
    parser.add_argument("--church-id", default="",
                        help="Optional church/session namespace override")
    parser.add_argument("--results-root", default="tests/benchmark/results/capture",
                        help="Results root for raw capture artifacts")
    parser.add_argument("--client-profile", default="benchmark", choices=["benchmark", "web"],
                        help="How closely to mirror the real web client transport behavior")
    parser.add_argument("--note", default="",
                        help="Free-text note recorded with this run")
    parser.add_argument("--stt-model", default="",
                        help="Override the Google Speech model for this run")
    parser.add_argument("--stt-language", default="",
                        help="Override the primary Google Speech language code for this run")
    parser.add_argument("--stt-alt-language", action="append", default=[],
                        help="Optional additional language code; may be passed multiple times")
    parser.add_argument("--stt-location", default="",
                        help="Google Speech location override (for example: us)")
    parser.add_argument("--stt-recognizer", default="",
                        help="Google Speech recognizer override (resource name or '_' for inline config)")
    parser.add_argument("--diarization-enabled", action="store_true",
                        help="Enable Chirp 3 speaker diarization for this capture run")
    parser.add_argument("--diarization-min-speakers", type=int, default=2,
                        help="Minimum speaker count hint when diarization is enabled")
    parser.add_argument("--diarization-max-speakers", type=int, default=2,
                        help="Maximum speaker count hint when diarization is enabled")
    parser.add_argument("--utterance-end-ms", type=int, default=2000,
                        help="Google Speech utterance-end / speech-end timeout in milliseconds")
    parser.add_argument("--confidence-hold-threshold", type=float, default=0.72,
                        help="Average word confidence below which a final gets an extra buffer hold")
    parser.add_argument("--low-confidence-hold-secs", type=float, default=2.5,
                        help="Extra hold duration applied to low-confidence STT finals")
    return parser


def build_capture_result(
    *,
    run_id: str,
    messages: list[dict],
    wall_s: float,
    audio_path: Path,
    duration_s: float,
    start_offset_s: float,
    audio_dir: str,
    note: str,
    stt_config: dict | None,
    transport: dict | None,
    client_profile: str,
) -> dict:
    stt_finals = [m for m in messages if m.get("type") == "stt_final"]
    committed = [m for m in messages if m.get("type") == "feed_commit"]
    live_translations = [m for m in messages if m.get("type") == "live_translation"]
    revisions = [m for m in messages if m.get("type") == "feed_revision"]
    final_spanish = [m for m in messages if m.get("type") == "final_spanish"]

    def _first_elapsed(kind: str) -> float | None:
        for message in messages:
            if message.get("type") == kind:
                value = message.get("_elapsed_s")
                return float(value) if value is not None else None
        return None

    def _message_language_mode(message: dict) -> str:
        return str(
            message.get("segment_language_mode")
            or message.get("stt_segment_language_mode")
            or ""
        ).strip().lower()

    def _message_speaker_count(message: dict) -> int:
        value = message.get("speaker_count")
        if value is None:
            value = message.get("stt_speaker_count")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    def _message_speaker_switches(message: dict) -> int:
        value = message.get("speaker_switch_count")
        if value is None:
            value = message.get("stt_speaker_switch_count")
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    language_counts: dict[str, int] = {}
    segment_language_mode_counts: dict[str, int] = {}
    speaker_count_distribution: dict[str, int] = {}
    mixed_speaker_segment_count = 0
    speaker_switch_segment_count = 0
    total_speaker_switch_count = 0
    dominant_speakers: dict[str, int] = {}
    for message in stt_finals + final_spanish:
        codes = message.get("detected_languages") or message.get("stt_detected_languages") or []
        primary = message.get("detected_language") or message.get("stt_primary_language") or ""
        normalized_codes: list[str] = []
        for code in ([primary] if primary else []) + list(codes):
            key = str(code).strip()
            if not key or key in normalized_codes:
                continue
            normalized_codes.append(key)
        for key in normalized_codes:
            language_counts[key] = language_counts.get(key, 0) + 1
        language_mode = _message_language_mode(message)
        if language_mode:
            segment_language_mode_counts[language_mode] = segment_language_mode_counts.get(language_mode, 0) + 1
        speaker_count = _message_speaker_count(message)
        speaker_count_distribution[str(speaker_count)] = speaker_count_distribution.get(str(speaker_count), 0) + 1
        switches = _message_speaker_switches(message)
        total_speaker_switch_count += switches
        if switches > 0:
            speaker_switch_segment_count += 1
        if bool(message.get("mixed_speaker_segment") or message.get("stt_mixed_speaker_segment")):
            mixed_speaker_segment_count += 1
        dominant = message.get("dominant_speaker")
        if dominant is None:
            dominant = message.get("stt_dominant_speaker")
        if dominant not in (None, "", 0, "0"):
            key = str(dominant)
            dominant_speakers[key] = dominant_speakers.get(key, 0) + 1

    return {
        "run_id": run_id,
        "git_commit": get_git_commit(),
        "audio_dir": audio_dir,
        "audio_file": audio_path.name,
        "clip_start_offset_s": start_offset_s,
        "clip_duration_s": duration_s,
        "note": note,
        "client_profile": client_profile,
        "stt_config": stt_config or {},
        "transport": transport or {},
        "wall_time_s": wall_s,
        "reference": None,
        "summary": {
            "stt_final_count": len(stt_finals),
            "final_spanish_count": len(final_spanish),
            "live_translation_count": len(live_translations),
            "feed_commit_count": len(committed),
            "feed_revision_count": len(revisions),
            "first_interim_s": _first_elapsed("interim"),
            "first_live_translation_s": _first_elapsed("live_translation"),
            "first_final_spanish_s": _first_elapsed("final_spanish"),
            "first_feed_commit_s": _first_elapsed("feed_commit"),
            "detected_language_counts": language_counts,
            "segment_language_mode_counts": segment_language_mode_counts,
            "speaker_count_distribution": speaker_count_distribution,
            "speaker_switch_segment_count": speaker_switch_segment_count,
            "mixed_speaker_segment_count": mixed_speaker_segment_count,
            "total_speaker_switch_count": total_speaker_switch_count,
            "dominant_speaker_counts": dominant_speakers,
        },
        "layers": {
            "raw_stt": {
                "finals_count": len(stt_finals),
                "text": " ".join(m.get("text", "") for m in stt_finals),
            },
            "committed_sentences": {
                "sentence_count": len(final_spanish),
                "sentences": [m.get("text", "") for m in final_spanish],
                "text": " ".join(m.get("text", "") for m in final_spanish),
            },
            "feed_commits": [
                {
                    "spanish": m.get("spanish"),
                    "english": m.get("english"),
                    "segment_id": m.get("segment_id"),
                    "ts": m.get("ts"),
                    "source": m.get("source"),
                    "elapsed_s": m.get("_elapsed_s"),
                }
                for m in committed
            ],
            "feed_revisions": [
                {
                    "english": m.get("english"),
                    "spanish": m.get("spanish"),
                    "segment_id": m.get("segment_id"),
                    "ts": m.get("ts"),
                    "source": m.get("source"),
                    "reason": m.get("reason"),
                    "phrase_alignment": m.get("phrase_alignment"),
                    "elapsed_s": m.get("_elapsed_s"),
                }
                for m in revisions
            ],
        },
        "all_messages": messages,
    }


def save_capture_result(result: dict, results_root: Path) -> Path:
    audio_dir_name = Path(result["audio_dir"]).name
    capture_dir = results_root / audio_dir_name / "capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / f"{result['run_id']}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_capture_report(result: dict) -> None:
    divider = "=" * 62
    summary = result["summary"]
    print(f"\n{divider}")
    print(f"  CAPTURE PIPELINE TEST - {result['run_id']}")
    print(divider)
    print(f"  Audio  : {result['audio_file']}  (offset={result['clip_start_offset_s']}s, {result['clip_duration_s']}s clip)")
    if result["note"]:
        print(f"  Note   : {result['note']}")
    print("\n  Event Summary:")
    print(f"    STT finals        : {summary['stt_final_count']}")
    print(f"    Final Spanish     : {summary['final_spanish_count']}")
    print(f"    Live translations : {summary['live_translation_count']}")
    print(f"    Feed commits      : {summary['feed_commit_count']}")
    print(f"    Feed revisions    : {summary['feed_revision_count']}")
    print("\n  First timings:")
    print(f"    interim           : {summary['first_interim_s']}")
    print(f"    live_translation  : {summary['first_live_translation_s']}")
    print(f"    final_spanish     : {summary['first_final_spanish_s']}")
    print(f"    feed_commit       : {summary['first_feed_commit_s']}")
    if summary["detected_language_counts"]:
        print("\n  Detected languages:")
        for code, count in sorted(summary["detected_language_counts"].items()):
            print(f"    {code}: {count}")
    if summary["segment_language_mode_counts"]:
        print("\n  Segment language modes:")
        for code, count in sorted(summary["segment_language_mode_counts"].items()):
            print(f"    {code}: {count}")
    if summary["speaker_count_distribution"]:
        print("\n  Speaker counts per segment:")
        for count, occurrences in sorted(summary["speaker_count_distribution"].items(), key=lambda item: int(item[0])):
            print(f"    {count} speakers: {occurrences}")
        print(f"    mixed speaker segments: {summary['mixed_speaker_segment_count']}")
        print(f"    segments with switches : {summary['speaker_switch_segment_count']}")
        print(f"    total switches         : {summary['total_speaker_switch_count']}")

    commits = result["layers"]["feed_commits"]
    if commits:
        print(f"\n  Feed Commits ({len(commits)}):")
        for item in commits[:10]:
            print(f"    [{item['elapsed_s']:5.1f}s] ES: {str(item['spanish'])[:90]}")
            print(f"            EN: {str(item['english'])[:90]}  ({item.get('source', 'unknown')})")

    revisions = result["layers"]["feed_revisions"]
    if revisions:
        print(f"\n  Feed Revisions ({len(revisions)}):")
        for item in revisions[:10]:
            print(f"    [{item['elapsed_s']:5.1f}s] [{item.get('reason', 'revision')}] {str(item['english'])[:110]}")

    print(f"\n  Wall time : {result['wall_time_s']}s")
    print(f"  Events    : {len(result['all_messages'])} total")
    print(divider)


async def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    run_id = generate_run_id(args.audio_dir, args.start_offset)
    audio_dir = ROOT / args.audio_dir
    duration_s = resolve_duration(args.duration, args.allow_long_duration)
    start_offset_s = args.start_offset
    church_id = resolve_run_namespace(args.audio_dir, start_offset_s, args.church_id or None)
    results_root = ROOT / args.results_root

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
    if args.diarization_enabled:
        stt_config["diarizationEnabled"] = True
        stt_config["diarizationMinSpeakers"] = args.diarization_min_speakers
        stt_config["diarizationMaxSpeakers"] = max(
            args.diarization_min_speakers,
            args.diarization_max_speakers,
        )

    audio_path = find_primary_audio_file(audio_dir)
    print(f"Audio : {audio_path.name}")
    if args.server_base_url:
        print(f"Remote: {args.server_base_url}")
    else:
        print(f"Port  : {args.port}")
    print(f"Client: {args.client_profile}")
    print(f"Scope : {church_id}")

    samples, sample_rate = clip_audio(audio_path, duration_s, start_offset_s=start_offset_s)

    proc = None
    try:
        if args.server_base_url:
            print("Target server ready path is managed externally.\n")
        else:
            print(f"Starting server on port {args.port}...")
            env = os.environ.copy()
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
                env=env,
            )
            await wait_for_server(args.port)
            print("Server ready.\n")

        print("Streaming audio through pipeline...")
        messages, wall_s, transport = await run_pipeline(
            samples,
            sample_rate,
            church_id,
            server_port=None if args.server_base_url else args.port,
            server_base_url=args.server_base_url,
            stt_config=stt_config,
            client_profile=args.client_profile,
        )
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    result = build_capture_result(
        run_id=run_id,
        messages=messages,
        wall_s=wall_s,
        audio_path=audio_path,
        duration_s=duration_s,
        start_offset_s=start_offset_s,
        audio_dir=args.audio_dir,
        note=args.note,
        stt_config=stt_config,
        transport=transport,
        client_profile=args.client_profile,
    )
    output = save_capture_result(result, results_root)
    print(f"\nSaved  : {output}")
    print_capture_report(result)


if __name__ == "__main__":
    asyncio.run(main())
