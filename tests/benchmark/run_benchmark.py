#!/usr/bin/env python3
"""
ChurchBridge AI — Benchmark Runner
====================================
Submits a sermon audio file to Deepgram's pre-recorded API, compares the
transcript against an SRT ground truth (WER), then runs the Spanish text
through Google Translate and reports on translation quality.

Results are saved to tests/benchmark/results/<audio_dir>/
The raw Deepgram JSON response is cached — re-runs skip transcription and
go straight to scoring, making iteration fast.

Usage:
    cd /path/to/churchbridge-ai
    python tests/benchmark/run_benchmark.py
    python tests/benchmark/run_benchmark.py --audio-dir tests/audio/2
    python tests/benchmark/run_benchmark.py --retranscribe   # force fresh Deepgram call

Requirements (all in requirements.txt):
    httpx, python-dotenv

Environment variables (from .env):
    DEEPGRAM_API_KEY
    GOOGLE_TRANSLATE_API_KEY
"""
import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── Environment ───────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent
load_dotenv(ROOT / ".env")

DEEPGRAM_API_KEY       = os.environ.get("DEEPGRAM_API_KEY", "")
GOOGLE_TRANSLATE_KEY   = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")

DEEPGRAM_PRERECORDED   = "https://api.deepgram.com/v1/listen"
GOOGLE_TRANSLATE_URL   = "https://translation.googleapis.com/language/translate/v2"

# Deepgram parameters — mirrors the streaming session as closely as possible
# so benchmark WER reflects real production conditions.
#
# keyterms are injected as repeated query parameters (list of tuples, not a dict)
# to match the production streaming session. The default glossary terms here
# mirror server/db/glossary.py DEFAULT_GLOSSARY so benchmark scores are
# comparable to live performance.
_DEEPGRAM_BASE_PARAMS = [
    ("model",        "nova-3"),
    ("language",     "es"),
    ("smart_format", "true"),
    ("punctuate",    "true"),
    ("paragraphs",   "true"),
    ("utterances",   "true"),
    ("diarize",      "false"),
]

_DEFAULT_KEYTERMS = [
    "Jesucristo",
    "Espíritu Santo",
    "Pentecostés",
    "evangelio",
    "salvación",
    "alabanza",
    "adoración",
]

DEEPGRAM_PARAMS = _DEEPGRAM_BASE_PARAMS + [("keyterms", t) for t in _DEFAULT_KEYTERMS]


# ── SRT parsing ───────────────────────────────────────────────────────────────

_SRT_TIMECODE = re.compile(r'^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$')
_SRT_INDEX    = re.compile(r'^\d+$')
_NOISE_TAG    = re.compile(r'\[.*?\]')   # "[Música]", "[Aplausos]", etc.


def _timecode_to_s(tc: str) -> float:
    """'00:01:23,456' → 83.456"""
    h, m, rest = tc.split(':')
    s, ms = rest.split(',')
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_srt(path: Path) -> list[dict]:
    """Parse an SRT file into a list of {index, start, end, text} dicts.

    Noise tags like [Música] are stripped. Segments with no remaining text
    (single filler words like standalone 'y') are kept — they are part of the
    ground truth and should influence WER.
    """
    segments: list[dict] = []
    current: dict | None = None
    lines: list[str] = []

    with open(path, encoding='utf-8') as f:
        raw = f.read()

    for line in raw.splitlines():
        line = line.strip()
        if _SRT_INDEX.match(line):
            if current and lines:
                current['text'] = ' '.join(lines)
                segments.append(current)
            current = {'index': int(line), 'start': 0.0, 'end': 0.0, 'text': ''}
            lines = []
        elif _SRT_TIMECODE.match(line) and current is not None:
            parts = line.split(' --> ')
            current['start'] = _timecode_to_s(parts[0])
            current['end']   = _timecode_to_s(parts[1])
        elif line and current is not None:
            cleaned = _NOISE_TAG.sub('', line).strip()
            if cleaned:
                lines.append(cleaned)

    if current and lines:
        current['text'] = ' '.join(lines)
        segments.append(current)

    return [s for s in segments if s['text'].strip()]


# ── Text normalization ─────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Used before WER."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


# ── WER (Word Error Rate) ─────────────────────────────────────────────────────

def compute_wer(reference: str, hypothesis: str) -> dict:
    """Compute WER using Levenshtein distance at the word level.

    WER = (S + D + I) / N
      S = substitutions
      D = deletions
      I = insertions
      N = reference word count

    Returns a dict with score, substitutions, deletions, insertions,
    reference_words, and hypothesis_words.
    """
    ref  = _normalize(reference).split()
    hyp  = _normalize(hypothesis).split()
    n, m = len(ref), len(hyp)

    # dp[i][j] = (cost, S, D, I)
    INF = float('inf')
    dp  = [[(INF, 0, 0, 0)] * (m + 1) for _ in range(n + 1)]
    dp[0][0] = (0, 0, 0, 0)
    for i in range(1, n + 1):
        dp[i][0] = (i, 0, i, 0)   # i deletions
    for j in range(1, m + 1):
        dp[0][j] = (j, 0, 0, j)   # j insertions

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i-1] == hyp[j-1]:
                cost, s, d, ins = dp[i-1][j-1]
                dp[i][j] = (cost, s, d, ins)
            else:
                # substitution
                c_sub, s_sub, d_sub, i_sub = dp[i-1][j-1]
                sub = (c_sub + 1, s_sub + 1, d_sub, i_sub)
                # deletion (ref word not in hyp)
                c_del, s_del, d_del, i_del = dp[i-1][j]
                delet = (c_del + 1, s_del, d_del + 1, i_del)
                # insertion (hyp word not in ref)
                c_ins, s_ins, d_ins, i_ins = dp[i][j-1]
                insert = (c_ins + 1, s_ins, d_ins, i_ins + 1)
                dp[i][j] = min(sub, delet, insert, key=lambda x: x[0])

    cost, subs, dels, inss = dp[n][m]
    wer_score = cost / n if n > 0 else 0.0
    return {
        "score":            round(wer_score, 4),
        "score_pct":        round(wer_score * 100, 2),
        "substitutions":    subs,
        "deletions":        dels,
        "insertions":       inss,
        "edit_distance":    cost,
        "reference_words":  n,
        "hypothesis_words": m,
    }


# ── Deepgram pre-recorded ─────────────────────────────────────────────────────

async def transcribe_deepgram(audio_path: Path) -> dict:
    """Submit audio to Deepgram pre-recorded API. Returns raw JSON response."""
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY not set")

    print(f"  Submitting {audio_path.name} to Deepgram ({audio_path.stat().st_size / 1e6:.1f} MB)...")
    t0 = time.monotonic()

    async with httpx.AsyncClient(timeout=300.0) as client:
        with open(audio_path, 'rb') as f:
            audio_bytes = f.read()

        resp = await client.post(
            DEEPGRAM_PRERECORDED,
            params=DEEPGRAM_PARAMS,
            content=audio_bytes,
            headers={
                "Authorization": f"Token {DEEPGRAM_API_KEY}",
                "Content-Type":  "audio/mp3",
            },
        )
        resp.raise_for_status()

    elapsed = time.monotonic() - t0
    result  = resp.json()
    result["_benchmark_latency_s"] = round(elapsed, 2)
    print(f"  Deepgram response received in {elapsed:.1f}s")
    return result


def extract_transcript(dg_response: dict) -> tuple[str, float, list[dict]]:
    """Extract full transcript, average word confidence, and utterances from Deepgram response."""
    try:
        alt = dg_response["results"]["channels"][0]["alternatives"][0]
        transcript = alt.get("transcript", "")

        words = alt.get("words", [])
        avg_conf = (
            sum(w.get("confidence", 0.0) for w in words) / len(words)
            if words else 0.0
        )

        utterances = dg_response["results"].get("utterances", [])
        return transcript, round(avg_conf, 4), utterances
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Deepgram response shape: {e}") from e


# ── Google Translate ───────────────────────────────────────────────────────────

_P_TAG = re.compile(r'<p>(.*?)</p>', re.DOTALL)


async def translate_google(text: str) -> tuple[str, float]:
    """Translate Spanish text to English via Google Cloud Translation v2.

    Splits on sentence boundaries and sends as HTML <p> blocks so Google
    preserves paragraph structure. Returns (translated_text, latency_s).
    """
    if not GOOGLE_TRANSLATE_KEY:
        print("  GOOGLE_TRANSLATE_API_KEY not set — skipping translation")
        return "", 0.0

    # Split at sentence endings for better Google context handling
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    payload = "".join(f"<p>{html.escape(s)}</p>" for s in sentences)

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            GOOGLE_TRANSLATE_URL,
            params={"key": GOOGLE_TRANSLATE_KEY},
            json={"q": payload, "source": "es", "target": "en", "format": "html"},
        )
        resp.raise_for_status()
    elapsed = time.monotonic() - t0

    raw = resp.json()["data"]["translations"][0]["translatedText"]
    parts = [html.unescape(m).strip() for m in _P_TAG.findall(raw)]
    translated = " ".join(parts) if parts else html.unescape(raw)
    return translated, round(elapsed, 2)


# ── Report helpers ─────────────────────────────────────────────────────────────

def print_divider(title: str = ""):
    if title:
        print(f"\n{'─' * 20} {title} {'─' * 20}")
    else:
        print("─" * 60)


def print_wer(wer_result: dict):
    score = wer_result['score_pct']
    bar_len = min(int(score), 60)
    bar = '█' * bar_len + '░' * (60 - bar_len)
    print(f"  WER:  {score:5.1f}%  [{bar}]")
    print(f"  Ref words : {wer_result['reference_words']}")
    print(f"  Hyp words : {wer_result['hypothesis_words']}")
    print(f"  Subs / Del / Ins : {wer_result['substitutions']} / "
          f"{wer_result['deletions']} / {wer_result['insertions']}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="ChurchBridge AI Benchmark")
    parser.add_argument(
        "--audio-dir",
        default="tests/audio/1",
        help="Directory containing the .mp3 and .srt files (default: tests/audio/1)",
    )
    parser.add_argument(
        "--retranscribe",
        action="store_true",
        help="Ignore cached Deepgram response and re-transcribe",
    )
    args = parser.parse_args()

    audio_dir = ROOT / args.audio_dir
    if not audio_dir.is_dir():
        print(f"ERROR: audio directory not found: {audio_dir}")
        sys.exit(1)

    # Locate audio and SRT files
    mp3_files = list(audio_dir.glob("*.mp3"))
    srt_files = list(audio_dir.glob("*.srt"))
    if not mp3_files:
        print(f"ERROR: no .mp3 file found in {audio_dir}")
        sys.exit(1)
    if not srt_files:
        print(f"ERROR: no .srt file found in {audio_dir}")
        sys.exit(1)

    audio_path = mp3_files[0]
    srt_path   = srt_files[0]

    # Results directory for this audio set
    result_dir = ROOT / "tests" / "benchmark" / "results" / audio_dir.name
    result_dir.mkdir(parents=True, exist_ok=True)
    cache_path = result_dir / "deepgram_response.json"

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    print_divider()
    print(f"  ChurchBridge AI — Benchmark Run {run_id}")
    print_divider()
    print(f"  Audio : {audio_path.name}")
    print(f"  SRT   : {srt_path.name}")

    # ── 1. Parse SRT ──────────────────────────────────────────────────────────
    print_divider("SRT Ground Truth")
    srt_segments = parse_srt(srt_path)
    srt_text     = " ".join(s['text'] for s in srt_segments)
    srt_words    = _normalize(srt_text).split()
    duration_s   = srt_segments[-1]['end'] if srt_segments else 0.0
    print(f"  Segments : {len(srt_segments)}")
    print(f"  Words    : {len(srt_words)}")
    print(f"  Duration : {duration_s/60:.1f} min")
    print(f"  Preview  : {srt_text[:120]}...")

    # ── 2. Deepgram transcription (cached) ────────────────────────────────────
    print_divider("Deepgram Transcription")
    print(f"  Keyterms  : {', '.join(_DEFAULT_KEYTERMS)}")
    if cache_path.exists() and not args.retranscribe:
        print(f"  Using cached response: {cache_path}")
        with open(cache_path) as f:
            dg_response = json.load(f)
    else:
        dg_response = await transcribe_deepgram(audio_path)
        with open(cache_path, 'w') as f:
            json.dump(dg_response, f, ensure_ascii=False, indent=2)
        print(f"  Cached to: {cache_path}")

    dg_transcript, avg_conf, utterances = extract_transcript(dg_response)
    dg_words = _normalize(dg_transcript).split()
    dg_latency = dg_response.get("_benchmark_latency_s", 0.0)

    print(f"  Words       : {len(dg_words)}")
    print(f"  Avg conf    : {avg_conf:.3f}")
    print(f"  Utterances  : {len(utterances)}")
    print(f"  API latency : {dg_latency:.1f}s  ({dg_latency/(duration_s/60):.1f}s per min of audio)")
    print(f"  Preview     : {dg_transcript[:120]}...")

    # ── 3. WER ────────────────────────────────────────────────────────────────
    print_divider("Word Error Rate  (Deepgram vs SRT)")
    print("  NOTE: SRT is auto-generated and may itself contain errors.")
    print("  WER here measures alignment, not absolute accuracy.\n")
    wer_result = compute_wer(srt_text, dg_transcript)
    print_wer(wer_result)

    # ── 4. Common error analysis ───────────────────────────────────────────────
    print_divider("Noise Word Analysis")
    noise_patterns = {
        "Pentecostés": re.compile(r'\bpentecost[eé]s\b', re.IGNORECASE),
        "Santo (interjection)": re.compile(r'\bsanto\b(?!\s+(?:espíritu|padre|hijo|tomás|domingo))', re.IGNORECASE),
        "Aleluya": re.compile(r'\baleluy[au]\b', re.IGNORECASE),
        "Gloria": re.compile(r'\bgloria\b', re.IGNORECASE),
        "Amén": re.compile(r'\bam[eé]n\b', re.IGNORECASE),
    }
    for label, pat in noise_patterns.items():
        srt_count = len(pat.findall(srt_text))
        dg_count  = len(pat.findall(dg_transcript))
        print(f"  {label:30s}  SRT: {srt_count:3d}   Deepgram: {dg_count:3d}")

    # ── 5. Google Translation ─────────────────────────────────────────────────
    print_divider("Google Translation  (Deepgram transcript → English)")
    english_text, google_latency = await translate_google(dg_transcript[:5000])
    # Translate first 5000 chars for speed — full file would be slow + costly
    if english_text:
        print(f"  Latency : {google_latency:.2f}s (first 5000 chars of transcript)")
        print(f"  Preview : {english_text[:300]}...")
    else:
        print("  Skipped")

    # ── 6. Save full results ──────────────────────────────────────────────────
    result = {
        "run_id":    run_id,
        "audio_file": str(audio_path.name),
        "srt_file":   str(srt_path.name),
        "deepgram": {
            "params":           dict(_DEEPGRAM_BASE_PARAMS),
            "keyterms":         _DEFAULT_KEYTERMS,
            "word_count":       len(dg_words),
            "avg_confidence":   avg_conf,
            "utterance_count":  len(utterances),
            "api_latency_s":    dg_latency,
            "transcript_preview": dg_transcript[:500],
        },
        "srt": {
            "segment_count":    len(srt_segments),
            "word_count":       len(srt_words),
            "duration_s":       duration_s,
            "text_preview":     srt_text[:500],
        },
        "wer":   wer_result,
        "noise": {
            label: {
                "srt_count": len(pat.findall(srt_text)),
                "dg_count":  len(pat.findall(dg_transcript)),
            }
            for label, pat in noise_patterns.items()
        },
        "translation": {
            "google": {
                "latency_s": google_latency,
                "preview":   english_text[:500],
            }
        },
    }

    result_path = result_dir / f"{run_id}.json"
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print_divider("Summary")
    print(f"  WER          : {wer_result['score_pct']:.1f}%")
    print(f"  Avg conf     : {avg_conf:.3f}")
    print(f"  Deepgram RTF : {dg_latency / (duration_s):.3f}  (API time / audio duration)")
    print(f"  Results saved: {result_path}")
    print_divider()


if __name__ == "__main__":
    asyncio.run(main())
