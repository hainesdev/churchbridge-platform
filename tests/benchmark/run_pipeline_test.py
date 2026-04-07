#!/usr/bin/env python3
"""
ChurchBridge AI — Pipeline Regression Test
==========================================
Streams the first N seconds of a sermon audio file through the live server
WebSocket pipeline and captures every output event for WER scoring and
historical comparison.

Unlike run_benchmark.py (Deepgram pre-recorded API), this test exercises the
full production pipeline:
    audio chunks → DeepgramSession → _clean_stt() → SentenceBuffer
    → Google Translate → LLM enrichment → broadcaster → display WebSocket

All events received by the display WebSocket are stored in the result JSON,
including interim results, committed sentences, translations, LLM corrections,
verse detections, segment metadata, and mode changes.

WER is computed at two layers:
    Layer 1 — raw Deepgram finals (stt_final events)
    Layer 2 — committed sentences after cleaning + buffering (final_spanish events)

Usage:
    python tests/benchmark/run_pipeline_test.py
    python tests/benchmark/run_pipeline_test.py --note "After keyterm tuning"
    python tests/benchmark/run_pipeline_test.py --duration 120
    python tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/2

Requirements:
    pydub (added to requirements.txt) + ffmpeg on PATH for MP3 decoding.
    All other deps (websockets, httpx, numpy, uvicorn) already in requirements.txt.
"""

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

import httpx
import numpy as np
from dotenv import load_dotenv
from pydub import AudioSegment

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")

sys.path.insert(0, str(ROOT))
from tests.benchmark.run_benchmark import parse_srt, compute_wer  # noqa: E402
from tests.benchmark.orchestrator import run_evaluation_cycle      # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────

SERVER_PORT  = 8799          # test port — avoids colliding with dev server
CHURCH_ID    = "pipeline_test"
CHUNK_MS     = 100           # audio chunk duration sent per message (ms)
IDLE_DRAIN_S = 8.0           # stop collecting after this many seconds with no new events


# ── Audio helpers ─────────────────────────────────────────────────────────────

def find_audio_files(audio_dir: Path) -> tuple[Path, Path]:
    mp3s = list(audio_dir.glob("*.mp3"))
    srts = list(audio_dir.glob("*.srt"))
    if not mp3s:
        raise FileNotFoundError(f"No .mp3 found in {audio_dir}")
    if not srts:
        raise FileNotFoundError(f"No .srt found in {audio_dir}")
    return mp3s[0], srts[0]


def clip_audio(mp3_path: Path, duration_s: float) -> tuple[np.ndarray, int]:
    """Load MP3, clip to duration_s, return (float32 mono samples, sample_rate)."""
    print(f"Loading {mp3_path.name}...")
    audio = AudioSegment.from_mp3(str(mp3_path))
    clip  = audio[: int(duration_s * 1000)]
    sr    = clip.frame_rate

    samples = np.array(clip.get_array_of_samples(), dtype=np.float32)
    if clip.channels == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)
    samples /= 32768.0

    print(f"  Clipped to {duration_s}s  |  {sr} Hz  |  {len(samples):,} samples")
    return samples, sr


def filter_srt(segments: list[dict], duration_s: float) -> list[dict]:
    """Keep SRT segments whose start time falls within the clip."""
    return [s for s in segments if s["start"] < duration_s]


# ── Server helpers ─────────────────────────────────────────────────────────────

def get_git_commit() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT,
        )
        return r.stdout.strip()
    except Exception:
        return "unknown"


async def wait_for_server(timeout_s: float = 30.0):
    """Poll /docs until the server is accepting requests."""
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                r = await client.get(
                    f"http://localhost:{SERVER_PORT}/docs", timeout=1.0
                )
                if r.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
    raise RuntimeError(f"Server did not become ready within {timeout_s}s")


# ── Pipeline execution ─────────────────────────────────────────────────────────

async def run_pipeline(samples: np.ndarray, sample_rate: int) -> tuple[list[dict], float]:
    """
    Open a display listener and an audio streamer WebSocket against the live
    server, stream the audio at real-time pace, then drain until idle.

    Returns (all_messages, wall_time_s).
    Each message dict has an extra '_elapsed_s' field (seconds since test start).
    """
    import websockets  # noqa: PLC0415 — import here so test file is importable without ws

    display_url = f"ws://localhost:{SERVER_PORT}/api/display/v1?church_id={CHURCH_ID}"
    stream_url  = f"ws://localhost:{SERVER_PORT}/api/stream/v1?church_id={CHURCH_ID}"

    messages:       list[dict] = []
    test_start                 = time.monotonic()
    last_msg_at                = time.monotonic()
    audio_done                 = asyncio.Event()
    listener_ready             = asyncio.Event()
    stop_listener              = asyncio.Event()

    # ── Display listener ──────────────────────────────────────────────────────

    async def listen():
        nonlocal last_msg_at
        async with websockets.connect(display_url) as ws:
            listener_ready.set()
            while not stop_listener.is_set():
                try:
                    raw  = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(raw)
                    data["_elapsed_s"] = round(time.monotonic() - test_start, 3)
                    messages.append(data)
                    last_msg_at = time.monotonic()

                    kind    = data.get("type", "?")
                    preview = data.get("text") or data.get("spanish") or ""
                    print(f"  [{data['_elapsed_s']:6.1f}s] {kind:<25s} {str(preview)[:70]}")

                except asyncio.TimeoutError:
                    # After audio is done, stop once we've been idle long enough
                    if audio_done.is_set():
                        if time.monotonic() - last_msg_at >= IDLE_DRAIN_S:
                            stop_listener.set()
                except Exception:
                    stop_listener.set()
                    raise

    # ── Audio streamer ────────────────────────────────────────────────────────

    async def stream():
        async with websockets.connect(stream_url) as ws:
            await ws.send(json.dumps({"type": "session.start", "sampleRate": sample_rate}))
            resp = json.loads(await ws.recv())
            print(f"  Session started: {resp}")

            chunk_n = int(sample_rate * CHUNK_MS / 1000)
            for i in range(0, len(samples), chunk_n):
                chunk     = samples[i : i + chunk_n].astype("<f4")
                audio_b64 = base64.b64encode(chunk.tobytes()).decode()
                await ws.send(json.dumps({"type": "audio", "audio": audio_b64}))
                await asyncio.sleep(CHUNK_MS / 1000)

            await ws.send(json.dumps({"type": "session.stop"}))
            print("  Audio streaming complete — draining pipeline...")
            audio_done.set()

    # ── Run concurrently ──────────────────────────────────────────────────────

    listener_task = asyncio.create_task(listen())
    await listener_ready.wait()   # ensure subscriber is registered before audio starts

    await stream()
    await listener_task

    return messages, round(time.monotonic() - test_start, 1)


# ── Result building ────────────────────────────────────────────────────────────

def build_result(
    run_id:       str,
    messages:     list[dict],
    wall_s:       float,
    ref_text:     str,
    ref_segments: list[dict],
    mp3_path:     Path,
    srt_path:     Path,
    duration_s:   float,
    audio_dir:    str,
    note:         str,
) -> dict:
    # Partition messages by type
    stt_finals   = [m for m in messages if m["type"] == "stt_final"]
    committed    = [m for m in messages if m["type"] == "final_spanish"]
    translations = [m for m in messages if m["type"] == "translation"]
    corrections  = [m for m in messages if m["type"] in ("correction", "translation_update")]
    verses       = [m for m in messages if m["type"] in (
        "verse_detected", "verse_range_update", "verse_suggestion"
    )]
    seg_metadata = [m for m in messages if m["type"] == "segment_metadata"]
    mode_changes = [m for m in messages if m["type"] == "mode_change"]

    raw_text       = " ".join(m.get("text", "") for m in stt_finals)
    committed_text = " ".join(m.get("text", "") for m in committed)

    return {
        "run_id":          run_id,
        "git_commit":      get_git_commit(),
        "audio_dir":       audio_dir,
        "audio_file":      mp3_path.name,
        "srt_file":        srt_path.name,
        "clip_duration_s": duration_s,
        "note":            note,
        "wall_time_s":     wall_s,

        "srt_reference": {
            "segments":   len(ref_segments),
            "word_count": len(ref_text.split()),
            "text":       ref_text,
        },

        "layers": {
            "raw_deepgram": {
                "finals_count": len(stt_finals),
                "text":         raw_text,
                "wer":          compute_wer(ref_text, raw_text) if raw_text else None,
            },
            "committed_sentences": {
                "sentence_count": len(committed),
                "sentences":      [m.get("text", "") for m in committed],
                "text":           committed_text,
                "wer":            compute_wer(ref_text, committed_text) if committed_text else None,
            },
            "translations": [
                {
                    "spanish":   m.get("spanish"),
                    "english":   m.get("english"),
                    "ts":        m.get("ts"),
                    "elapsed_s": m.get("_elapsed_s"),
                }
                for m in translations
            ],
            "llm_corrections": [
                {
                    "type":      m["type"],
                    "english":   m.get("english"),
                    "ts":        m.get("ts"),
                    "elapsed_s": m.get("_elapsed_s"),
                }
                for m in corrections
            ],
            "verse_events":     verses,
            "segment_metadata": seg_metadata,
            "mode_changes":     mode_changes,
        },

        # Full ordered event log — every message the display WebSocket received
        "all_messages": messages,
    }


# ── Persistence ────────────────────────────────────────────────────────────────

def save_results(result: dict, audio_dir_name: str):
    out_dir = ROOT / "tests" / "benchmark" / "results" / audio_dir_name / "pipeline"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Full run JSON
    run_file = out_dir / f"{result['run_id']}.json"
    run_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved  : {run_file}")

    # Append summary row to history.json
    history_file = out_dir / "history.json"
    history = json.loads(history_file.read_text()) if history_file.exists() else []

    wer_raw       = result["layers"]["raw_deepgram"]["wer"]
    wer_committed = result["layers"]["committed_sentences"]["wer"]
    history.append({
        "run_id":              result["run_id"],
        "git_commit":          result["git_commit"],
        "note":                result["note"],
        "clip_duration_s":     result["clip_duration_s"],
        "wer_raw_pct":         wer_raw["score_pct"]       if wer_raw       else None,
        "wer_committed_pct":   wer_committed["score_pct"] if wer_committed else None,
        "sentence_count":      result["layers"]["committed_sentences"]["sentence_count"],
        "translation_count":   len(result["layers"]["translations"]),
        "llm_correction_count": len(result["layers"]["llm_corrections"]),
        "verse_event_count":   len(result["layers"]["verse_events"]),
        "wall_time_s":         result["wall_time_s"],
    })
    history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"History: {history_file}  ({len(history)} runs)")


# ── Report ─────────────────────────────────────────────────────────────────────

def _wer_bar(score_pct: float) -> str:
    n = min(int(score_pct), 60)
    return "█" * n + "░" * (60 - n)


def print_report(result: dict):
    div = "─" * 62
    print(f"\n{div}")
    print(f"  PIPELINE TEST — {result['run_id']}")
    print(div)
    print(f"  Git    : {result['git_commit']}")
    print(f"  Audio  : {result['audio_file']}  ({result['clip_duration_s']}s clip)")
    if result["note"]:
        print(f"  Note   : {result['note']}")

    ref = result["srt_reference"]
    print(f"\n  SRT reference: {ref['segments']} segments, {ref['word_count']} words")

    raw = result["layers"]["raw_deepgram"]
    print(f"\n  Layer 1 — Raw Deepgram STT  ({raw['finals_count']} finals)")
    if raw["wer"]:
        w = raw["wer"]
        print(f"    WER: {w['score_pct']:5.1f}%  [{_wer_bar(w['score_pct'])}]")
        print(f"         S:{w['substitutions']}  D:{w['deletions']}  I:{w['insertions']}")

    com = result["layers"]["committed_sentences"]
    print(f"\n  Layer 2 — Committed Sentences  ({com['sentence_count']} sentences)")
    if com["wer"]:
        w = com["wer"]
        print(f"    WER: {w['score_pct']:5.1f}%  [{_wer_bar(w['score_pct'])}]")
        print(f"         S:{w['substitutions']}  D:{w['deletions']}  I:{w['insertions']}")
    for i, s in enumerate(com["sentences"], 1):
        print(f"    {i}. {s}")

    trans = result["layers"]["translations"]
    if trans:
        print(f"\n  Translations  ({len(trans)}):")
        for t in trans:
            print(f"    [{t['elapsed_s']:5.1f}s] ES: {t['spanish']}")
            print(f"            EN: {t['english']}")

    llm = result["layers"]["llm_corrections"]
    if llm:
        print(f"\n  LLM Corrections  ({len(llm)}):")
        for c in llm:
            print(f"    [{c['elapsed_s']:5.1f}s] [{c['type']}] {c['english']}")

    verses = result["layers"]["verse_events"]
    if verses:
        print(f"\n  Verse Events  ({len(verses)}):")
        for v in verses:
            print(f"    {v.get('type')}: {v}")

    meta = result["layers"]["segment_metadata"]
    if meta:
        print(f"\n  Segment Metadata  ({len(meta)}):")
        for m in meta:
            items = {k: v for k, v in m.items() if not k.startswith("_") and k != "type"}
            print(f"    ts={m.get('ts')}: {items}")

    modes = result["layers"]["mode_changes"]
    if modes:
        changes = "  →  ".join(f"{m.get('from')} → {m.get('to')}" for m in modes)
        print(f"\n  Mode Changes: {changes}")

    print(f"\n  Wall time : {result['wall_time_s']}s")
    print(f"  Events    : {len(result['all_messages'])} total")
    print(div)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="ChurchBridge AI — Pipeline Regression Test")
    parser.add_argument("--audio-dir", default="tests/audio/1",
                        help="Directory containing .mp3 and .srt files")
    parser.add_argument("--duration",  type=float, default=85.0,
                        help="Seconds of audio to test (default: 85)")
    parser.add_argument("--note",      default="",
                        help="Free-text note recorded with this run")
    parser.add_argument("--no-llm",   action="store_true",
                        help="Skip LLM interpretation (deterministic evaluation only)")
    args = parser.parse_args()

    run_id    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    audio_dir = ROOT / args.audio_dir

    mp3_path, srt_path = find_audio_files(audio_dir)
    print(f"Audio : {mp3_path.name}")
    print(f"SRT   : {srt_path.name}")

    samples, sample_rate = clip_audio(mp3_path, args.duration)

    all_srt  = parse_srt(srt_path)
    ref_segs = filter_srt(all_srt, args.duration)
    ref_text = " ".join(s["text"] for s in ref_segs)
    print(f"SRT   : {len(ref_segs)} segments, {len(ref_text.split())} words in first {args.duration}s\n")

    # Start server subprocess
    print(f"Starting server on port {SERVER_PORT}...")
    env  = os.environ.copy()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "server.main:app",
            "--port", str(SERVER_PORT),
            "--log-level", "warning",
        ],
        cwd=ROOT, env=env,
    )
    try:
        await wait_for_server()
        print("Server ready.\n")

        print("Streaming audio through pipeline...")
        messages, wall_s = await run_pipeline(samples, sample_rate)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    result = build_result(
        run_id       = run_id,
        messages     = messages,
        wall_s       = wall_s,
        ref_text     = ref_text,
        ref_segments = ref_segs,
        mp3_path     = mp3_path,
        srt_path     = srt_path,
        duration_s   = args.duration,
        audio_dir    = args.audio_dir,
        note         = args.note,
    )

    audio_dir_name = Path(args.audio_dir).name
    save_results(result, audio_dir_name)
    print_report(result)

    # ── Closed-loop evaluation ─────────────────────────────────────────────────
    pipeline_dir = ROOT / "tests" / "benchmark" / "results" / audio_dir_name / "pipeline"
    run_evaluation_cycle(result, pipeline_dir, use_llm=not args.no_llm)


if __name__ == "__main__":
    asyncio.run(main())
