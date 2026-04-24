#!/usr/bin/env python3
"""
ChurchBridge AI — Pipeline Regression Test
==========================================
Streams a clipped sermon audio window through the live server WebSocket
pipeline and captures every output event for WER scoring and historical
comparison.

Unlike run_benchmark.py (Deepgram pre-recorded API), this test exercises the
full production pipeline:
    audio chunks -> DeepgramSession -> _clean_stt() -> SentenceBuffer
    -> Google Translate -> LLM enrichment -> broadcaster -> display WebSocket

All events received by the display WebSocket are stored in the result JSON,
including live dock updates, committed sentences, feed commits, feed revisions,
verse detections, segment metadata, and mode changes.

Concurrent runs must be isolated. If you run multiple pipeline benchmarks at
the same time, give each process its own --port and preferably its own
--church-id / --results-root as well. Sequential runs are the safest default.

The source recordings are also intentionally long. Use --start-offset plus a
bounded --duration to probe multiple windows from the same file, with cleaner
audio windows serving as optimization targets and clipped windows serving as
stress tests.

Translation quality is evaluated alongside pipeline behavior, but the loop
should still distinguish evaluator uncertainty from true regressions and should
read clipped/noisy windows as resilience checks rather than as the main clean
audio optimization target.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import numpy as np
from dotenv import load_dotenv
from pydub import AudioSegment

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from tests.benchmark.orchestrator import run_evaluation_cycle  # noqa: E402
from tests.benchmark.llm_translation_quality import evaluate_translation_quality  # noqa: E402
from tests.benchmark.scorecard import generate_scorecard  # noqa: E402
from tests.benchmark.run_benchmark import compute_wer, parse_srt  # noqa: E402
from tests.benchmark.storage import (  # noqa: E402
    append_history_row,
    pipeline_dir_for,
    regime_cycle_log_path,
    regime_report_path,
    resolve_results_root,
    save_run_result,
)

SERVER_PORT = 8799
CHUNK_MS = 100
IDLE_DRAIN_S = 8.0
DEFAULT_DURATION_S = 30.0
MAX_DEFAULT_DURATION_S = 30.0


def find_audio_files(audio_dir: Path) -> tuple[Path, Path]:
    mp3s = list(audio_dir.glob("*.mp3"))
    wavs = list(audio_dir.glob("*.wav"))
    srts = list(audio_dir.glob("*.srt"))
    audio_files = mp3s or wavs  # prefer MP3 if both present
    if not audio_files:
        raise FileNotFoundError(f"No .mp3 or .wav found in {audio_dir}")
    if not srts:
        raise FileNotFoundError(f"No .srt found in {audio_dir}")
    return audio_files[0], srts[0]


def clip_audio(mp3_path: Path, duration_s: float, start_offset_s: float = 0.0) -> tuple[np.ndarray, int]:
    """Load an MP3 or WAV window and return (float32 mono samples, sample_rate)."""
    print(f"Loading {mp3_path.name}...")
    if mp3_path.suffix.lower() == ".wav":
        audio = AudioSegment.from_wav(str(mp3_path))
    else:
        audio = AudioSegment.from_mp3(str(mp3_path))
    total_duration_s = len(audio) / 1000
    if start_offset_s < 0:
        raise ValueError("start_offset_s must be >= 0")
    if start_offset_s >= total_duration_s:
        raise ValueError(
            f"Requested start offset {start_offset_s}s exceeds audio length {total_duration_s:.3f}s"
        )

    clip_start_ms = int(start_offset_s * 1000)
    clip_end_ms = int(min(total_duration_s, start_offset_s + duration_s) * 1000)
    clip = audio[clip_start_ms:clip_end_ms]
    sr = clip.frame_rate

    samples = np.array(clip.get_array_of_samples(), dtype=np.float32)
    if clip.channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    samples /= 32768.0

    actual_duration_s = round(len(clip) / 1000, 3)
    print(
        f"  Clipped to {actual_duration_s}s from {start_offset_s}s  |  {sr} Hz  |  {len(samples):,} samples"
    )
    return samples, sr


def filter_srt(segments: list[dict], duration_s: float, start_offset_s: float = 0.0) -> list[dict]:
    """Keep SRT segments whose start time falls within the clip window."""
    clip_end_s = start_offset_s + duration_s
    return [s for s in segments if start_offset_s <= s["start"] < clip_end_s]


def resolve_duration(requested_duration_s: float, allow_long_duration: bool) -> float:
    if requested_duration_s <= MAX_DEFAULT_DURATION_S or allow_long_duration:
        return requested_duration_s
    raise ValueError(
        f"Requested duration {requested_duration_s}s exceeds the default limit of "
        f"{MAX_DEFAULT_DURATION_S}s. Re-run with --allow-long-duration to opt in."
    )


def resolve_run_namespace(audio_dir: str, start_offset_s: float, church_id: str | None) -> str:
    if church_id:
        return church_id
    audio_name = Path(audio_dir).name.replace(" ", "_")
    offset_ms = int(round(start_offset_s * 1000))
    return f"pipeline_test_{audio_name}_{offset_ms}"


def generate_run_id(audio_dir: str, start_offset_s: float) -> str:
    """
    Create a traceable run id that stays distinct across parallel staggered
    captures, even when separate processes start within the same microsecond.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    audio_name = Path(audio_dir).name.replace(" ", "_")
    offset_ms = int(round(start_offset_s * 1000))
    return f"{timestamp}-{audio_name}-{offset_ms}-{uuid4().hex[:8]}"


def get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


async def wait_for_server(server_port: int, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"http://localhost:{server_port}/docs", timeout=1.0)
                if response.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Server did not become ready within {timeout_s}s")


async def run_pipeline(
    samples: np.ndarray,
    sample_rate: int,
    server_port: int,
    church_id: str,
) -> tuple[list[dict], float]:
    """Stream audio through the live pipeline and return (all_messages, wall_time_s)."""
    import websockets  # noqa: PLC0415

    display_url = f"ws://localhost:{server_port}/api/display/v1?church_id={church_id}"
    stream_url = f"ws://localhost:{server_port}/api/stream/v1?church_id={church_id}"

    messages: list[dict] = []
    test_start = time.monotonic()
    last_msg_at = time.monotonic()
    audio_done = asyncio.Event()
    listener_ready = asyncio.Event()
    stop_listener = asyncio.Event()

    async def listen() -> None:
        nonlocal last_msg_at
        async with websockets.connect(display_url) as ws:
            listener_ready.set()
            while not stop_listener.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(raw)
                    data["_elapsed_s"] = round(time.monotonic() - test_start, 3)
                    messages.append(data)
                    last_msg_at = time.monotonic()

                    kind = data.get("type", "?")
                    preview = data.get("text") or data.get("spanish") or ""
                    print(f"  [{data['_elapsed_s']:6.1f}s] {kind:<25s} {str(preview)[:70]}")

                except asyncio.TimeoutError:
                    if audio_done.is_set() and time.monotonic() - last_msg_at >= IDLE_DRAIN_S:
                        stop_listener.set()
                except Exception:
                    stop_listener.set()
                    raise

    async def stream() -> None:
        async with websockets.connect(stream_url) as ws:
            await ws.send(json.dumps({"type": "session.start", "sampleRate": sample_rate}))
            response = json.loads(await ws.recv())
            print(f"  Session started: {response}")

            chunk_n = int(sample_rate * CHUNK_MS / 1000)
            for i in range(0, len(samples), chunk_n):
                chunk = samples[i : i + chunk_n].astype("<f4")
                audio_b64 = base64.b64encode(chunk.tobytes()).decode()
                await ws.send(json.dumps({"type": "audio", "audio": audio_b64}))
                await asyncio.sleep(CHUNK_MS / 1000)

            await ws.send(json.dumps({"type": "session.stop"}))
            print("  Audio streaming complete — draining pipeline...")
            audio_done.set()

    listener_task = asyncio.create_task(listen())
    await listener_ready.wait()

    await stream()
    await listener_task
    return messages, round(time.monotonic() - test_start, 1)


def build_result(
    run_id: str,
    messages: list[dict],
    wall_s: float,
    ref_text: str,
    ref_segments: list[dict],
    mp3_path: Path,
    srt_path: Path,
    duration_s: float,
    start_offset_s: float,
    audio_dir: str,
    note: str,
) -> dict:
    stt_finals = [m for m in messages if m["type"] == "stt_final"]
    committed = [m for m in messages if m["type"] == "final_spanish"]
    translations = [m for m in messages if m["type"] == "feed_commit"]
    corrections = [m for m in messages if m["type"] == "feed_revision"]
    verses = [m for m in messages if m["type"] in ("verse_detected", "verse_range_update", "verse_suggestion")]
    seg_metadata = [m for m in messages if m["type"] == "segment_metadata"]
    mode_changes = [m for m in messages if m["type"] == "mode_change"]

    raw_text = " ".join(m.get("text", "") for m in stt_finals)
    committed_text = " ".join(m.get("text", "") for m in committed)

    return {
        "run_id": run_id,
        "git_commit": get_git_commit(),
        "audio_dir": audio_dir,
        "audio_file": mp3_path.name,
        "srt_file": srt_path.name,
        "clip_start_offset_s": start_offset_s,
        "clip_duration_s": duration_s,
        "note": note,
        "wall_time_s": wall_s,
        "srt_reference": {
            "segments": len(ref_segments),
            "word_count": len(ref_text.split()),
            "text": ref_text,
        },
        "layers": {
            "raw_deepgram": {
                "finals_count": len(stt_finals),
                "text": raw_text,
                "wer": compute_wer(ref_text, raw_text) if raw_text else None,
            },
            "committed_sentences": {
                "sentence_count": len(committed),
                "sentences": [m.get("text", "") for m in committed],
                "text": committed_text,
                "wer": compute_wer(ref_text, committed_text) if committed_text else None,
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
            for m in translations
        ],
        "feed_revisions": [
            {
                "type": m["type"],
                "english": m.get("english"),
                "spanish": m.get("spanish"),
                "segment_id": m.get("segment_id"),
                "ts": m.get("ts"),
                "source": m.get("source"),
                "reason": m.get("reason"),
                "elapsed_s": m.get("_elapsed_s"),
            }
            for m in corrections
        ],
            "verse_events": verses,
            "segment_metadata": seg_metadata,
            "mode_changes": mode_changes,
        },
        "all_messages": messages,
    }


def _wer_bar(score_pct: float) -> str:
    n = min(int(score_pct), 60)
    return "█" * n + "░" * (60 - n)


def print_report(result: dict) -> None:
    divider = "─" * 62
    print(f"\n{divider}")
    print(f"  PIPELINE TEST — {result['run_id']}")
    print(divider)
    print(f"  Git    : {result['git_commit']}")
    print(
        f"  Audio  : {result['audio_file']}  "
        f"(offset={result.get('clip_start_offset_s', 0.0)}s, {result['clip_duration_s']}s clip)"
    )
    if result["note"]:
        print(f"  Note   : {result['note']}")

    ref = result["srt_reference"]
    print(f"\n  SRT reference: {ref['segments']} segments, {ref['word_count']} words")

    raw = result["layers"]["raw_deepgram"]
    print(f"\n  Layer 1 — Raw Deepgram STT  ({raw['finals_count']} finals)")
    if raw["wer"]:
        wer = raw["wer"]
        print(f"    WER: {wer['score_pct']:5.1f}%  [{_wer_bar(wer['score_pct'])}]")
        print(f"         S:{wer['substitutions']}  D:{wer['deletions']}  I:{wer['insertions']}")

    committed = result["layers"]["committed_sentences"]
    print(f"\n  Layer 2 — Committed Sentences  ({committed['sentence_count']} sentences)")
    if committed["wer"]:
        wer = committed["wer"]
        print(f"    WER: {wer['score_pct']:5.1f}%  [{_wer_bar(wer['score_pct'])}]")
        print(f"         S:{wer['substitutions']}  D:{wer['deletions']}  I:{wer['insertions']}")
    for i, sentence in enumerate(committed["sentences"], 1):
        print(f"    {i}. {sentence}")

    translations = result["layers"]["feed_commits"]
    if translations:
        print(f"\n  Feed Commits  ({len(translations)}):")
        for item in translations:
            print(f"    [{item['elapsed_s']:5.1f}s] ES: {item['spanish']}")
            print(f"            EN: {item['english']}  ({item.get('source', 'unknown')})")

    llm = result["layers"]["feed_revisions"]
    if llm:
        print(f"\n  Feed Revisions  ({len(llm)}):")
        for item in llm:
            print(
                f"    [{item['elapsed_s']:5.1f}s] "
                f"[{item.get('reason', item['type'])}] {item['english']}"
            )

    verses = result["layers"]["verse_events"]
    if verses:
        print(f"\n  Verse Events  ({len(verses)}):")
        for item in verses:
            print(f"    {item.get('type')}: {item}")

    meta = result["layers"]["segment_metadata"]
    if meta:
        print(f"\n  Segment Metadata  ({len(meta)}):")
        for item in meta:
            filtered = {k: v for k, v in item.items() if not k.startswith("_") and k != "type"}
            print(f"    ts={item.get('ts')}: {filtered}")

    modes = result["layers"]["mode_changes"]
    if modes:
        changes = "  ->  ".join(f"{m.get('from')} -> {m.get('to')}" for m in modes)
        print(f"\n  Mode Changes: {changes}")

    print(f"\n  Wall time : {result['wall_time_s']}s")
    print(f"  Events    : {len(result['all_messages'])} total")
    print(divider)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ChurchBridge AI â€” Pipeline Regression Test",
        epilog=(
            "Concurrent runs: use a unique --port for each process, and prefer "
            "isolated --church-id / --results-root values. Reusing the default "
            "port across concurrent runs can cause bind failures and noisy logs."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--audio-dir", default="tests/audio/1",
                        help="Directory containing .mp3 and .srt files")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                        help=f"Seconds of audio to test (default: {DEFAULT_DURATION_S:g})")
    parser.add_argument("--start-offset", type=float, default=0.0,
                        help="Start offset in seconds within the source audio")
    parser.add_argument("--allow-long-duration", action="store_true",
                        help="Allow durations above the default live-test limit")
    parser.add_argument("--port", type=int, default=SERVER_PORT,
                        help=f"Server port for this benchmark run (default: {SERVER_PORT}); concurrent runs must use different ports")
    parser.add_argument("--church-id", default="",
                        help="Church/session namespace for this run; defaults to an isolated generated value, but set it explicitly for concurrent runs when needed")
    parser.add_argument("--results-root", default="tests/benchmark/results",
                        help="Results root for artifacts. Use tests/benchmark/results/staggered for the new regime or a separate root to isolate concurrent runs")
    parser.add_argument("--capture-only", action="store_true",
                        help="Save the raw run JSON only; skip history, review, trajectory, cycle log, and report")
    parser.add_argument("--note", default="",
                        help="Free-text note recorded with this run")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM interpretation (deterministic evaluation only)")
    parser.add_argument("--translation-quality", action="store_true",
                        help="Run LLM translation quality evaluation after the benchmark cycle")
    parser.add_argument("--tq-chunk-size", type=int, default=5,
                        help="Sentences per chunk for translation quality evaluation (default: 5)")
    return parser


async def main() -> None:
    parser = argparse.ArgumentParser(description="ChurchBridge AI — Pipeline Regression Test")
    parser.add_argument("--audio-dir", default="tests/audio/1",
                        help="Directory containing .mp3 and .srt files")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION_S,
                        help=f"Seconds of audio to test (default: {DEFAULT_DURATION_S:g})")
    parser.add_argument("--start-offset", type=float, default=0.0,
                        help="Start offset in seconds within the source audio")
    parser.add_argument("--allow-long-duration", action="store_true",
                        help="Allow durations above the default live-test limit")
    parser.add_argument("--port", type=int, default=SERVER_PORT,
                        help=f"Server port for this benchmark run (default: {SERVER_PORT})")
    parser.add_argument("--church-id", default="",
                        help="Church/session namespace for this run; defaults to an isolated generated value")
    parser.add_argument("--results-root", default="tests/benchmark/results",
                        help="Results root for artifacts. Use tests/benchmark/results/staggered for the new regime")
    parser.add_argument("--capture-only", action="store_true",
                        help="Save the raw run JSON only; skip history, review, trajectory, cycle log, and report")
    parser.add_argument("--note", default="",
                        help="Free-text note recorded with this run")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM interpretation (deterministic evaluation only)")
    parser.add_argument("--translation-quality", action="store_true",
                        help="Run LLM translation quality evaluation after the benchmark cycle")
    parser.add_argument("--tq-chunk-size", type=int, default=5,
                        help="Sentences per chunk for translation quality evaluation (default: 5)")
    args = parser.parse_args()

    run_id = generate_run_id(args.audio_dir, args.start_offset)
    audio_dir = ROOT / args.audio_dir
    duration_s = resolve_duration(args.duration, args.allow_long_duration)
    start_offset_s = args.start_offset
    results_root = resolve_results_root(args.results_root)
    church_id = resolve_run_namespace(args.audio_dir, start_offset_s, args.church_id or None)

    mp3_path, srt_path = find_audio_files(audio_dir)
    print(f"Audio : {mp3_path.name}")
    print(f"SRT   : {srt_path.name}")
    print(f"Port  : {args.port}")
    print(f"Scope : {church_id}")

    samples, sample_rate = clip_audio(mp3_path, duration_s, start_offset_s=start_offset_s)

    all_srt = parse_srt(srt_path)
    ref_segs = filter_srt(all_srt, duration_s, start_offset_s=start_offset_s)
    ref_text = " ".join(s["text"] for s in ref_segs)
    print(
        f"SRT   : {len(ref_segs)} segments, {len(ref_text.split())} words in "
        f"window {start_offset_s}s–{start_offset_s + duration_s}s\n"
    )

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
    try:
        await wait_for_server(args.port)
        print("Server ready.\n")

        print("Streaming audio through pipeline...")
        messages, wall_s = await run_pipeline(samples, sample_rate, args.port, church_id)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

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
        audio_dir=args.audio_dir,
        note=args.note,
    )

    audio_dir_name = Path(args.audio_dir).name
    pipeline_dir = pipeline_dir_for(audio_dir_name, results_root)
    run_file = save_run_result(result, pipeline_dir)
    print(f"\nSaved  : {run_file}")
    print_report(result)

    if args.capture_only:
        print("Capture-only mode: skipping history append and evaluation.")
        return

    history_file = append_history_row(result, pipeline_dir)
    history = json.loads(history_file.read_text(encoding="utf-8"))
    print(f"History: {history_file}  ({len(history)} runs)")

    # Generate scorecard early so the quality evaluator can patch it before
    # the evaluation cycle runs trajectory / review / LLM interpretation.
    scorecard_path = pipeline_dir / "scorecards" / f"{run_id}.json"
    scorecard = generate_scorecard(result, scorecard_path)

    if args.translation_quality:
        # Patch quality scalars into the scorecard file before the cycle so that
        # trajectory, action recommendation, and the LLM interpreter all see the
        # quality signal for this run rather than the next one.
        evaluate_translation_quality(result, pipeline_dir, chunk_size=args.tq_chunk_size)
        # Reload from disk — _patch_scorecard() wrote the quality section in place.
        scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))

    run_evaluation_cycle(
        result,
        pipeline_dir,
        use_llm=not args.no_llm,
        cycle_log_path=regime_cycle_log_path(results_root),
        report_path=regime_report_path(results_root),
        precomputed_scorecard=scorecard,
    )


if __name__ == "__main__":
    asyncio.run(main())
