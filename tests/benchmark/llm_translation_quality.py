#!/usr/bin/env python3
"""
ChurchBridge AI — LLM Translation Quality Evaluator
====================================================
A long-running test that uses Claude Opus to evaluate the semantic and
theological quality of translation output across larger sentence chunks.

Unlike llm_interpret.py (which evaluates pipeline behavior — ordering, timing,
architectural correctness), this module evaluates whether the English output
actually conveys the right meaning:

  - Does the LLM-improved translation add value over Google Translate?
  - Is theological/Pentecostal sermon register preserved naturally?
  - Are any sentences mistranslated, hallucinated, or oddly phrased?
  - How does translation_deviation_score correlate with actual quality changes?

Additional signal provided per sentence pair:
  - translation_deviation_score  — word-level Jaccard similarity (0.0–1.0).
    Low score = LLM diverged significantly from Google baseline.
  - reconstruction_risk          — True when deviation < 0.35 AND source was noisy.
    Indicates the LLM may have hallucinated meaning from noisy STT input.
  - source_quality               — "clean" | "noisy" | "fragmented" (from segment_metadata).

Chunking strategy:
  Sentences are grouped into chunks of CHUNK_SIZE (default 5) so the LLM can
  evaluate translation quality with enough surrounding context to judge register
  consistency, theological coherence, and narrative flow — while staying within
  a manageable token budget per API call.

Output schema:
  {
    "run_id":                str,
    "audio_dir":             str,
    "model":                 str,
    "chunk_size":            int,
    "pair_count":            int,
    "overall_quality_rating": float,        -- 1.0 (very poor) to 5.0 (excellent)
    "overall_summary":       str,
    "confidence":            str,           -- "high" | "medium" | "low"
    "chunk_evaluations": [
      {
        "chunk_index":        int,
        "sentence_indices":   [int],        -- indices into the pairs list
        "quality_rating":     float,        -- 1.0–5.0
        "llm_vs_google_winner": str,        -- "llm" | "google" | "tie" | "mixed"
        "issues":             [str],
        "highlights":         [str],
        "notes":              str,
      }
    ],
    "flagged_pairs": [                      -- pairs with deviation_score < FLAG_DEVIATION_THRESHOLD
      {
        "index":            int,
        "spanish":          str,
        "google":           str,
        "llm":              str,
        "deviation_score":  float,
        "source_quality":   str,
        "reconstruction_risk": bool,
        "verdict":          str,
        "severity":         str,            -- "high" | "medium" | "low"
      }
    ],
    "recommendations": [str],
  }

Usage (from run_pipeline_test.py):
    from tests.benchmark.llm_translation_quality import evaluate_translation_quality
    quality = evaluate_translation_quality(result, pipeline_dir)

Usage (standalone):
    python tests/benchmark/llm_translation_quality.py <run_json_path> [--chunk-size N]
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from server.services.translation_deviation import translation_deviation_score


# ── Constants ──────────────────────────────────────────────────────────────────

# Jaccard threshold below which a pair is flagged for close review.
# Mirrors _RECONSTRUCTION_RISK_THRESHOLD in llm_enrichment_service.py (0.35)
# but set slightly higher here so we flag more edge cases for human inspection.
FLAG_DEVIATION_THRESHOLD = 0.50

# Threshold below which a noisy-source pair is considered reconstruction risk.
# Must match _RECONSTRUCTION_RISK_THRESHOLD in llm_enrichment_service.py.
RECONSTRUCTION_RISK_THRESHOLD = 0.35

DEFAULT_CHUNK_SIZE = 5
DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_PARSE_RETRIES = 2
MAX_PARSE_RETRY_TOKENS = 2048



# ── Deviation score (mirrors llm_enrichment_service._translation_deviation_score) ─

def _deviation_score(google: str, llm: str) -> float:
    """
    Word-level Jaccard similarity between two English translations.
    Returns 0.0 (completely different) to 1.0 (identical).

    Mirrors the private _translation_deviation_score() function in
    llm_enrichment_service.py so we can apply the same logic offline
    without importing the full service.
    """
    return translation_deviation_score(google, llm)


# ── Data extraction ────────────────────────────────────────────────────────────

def _build_enriched_pairs(result: dict) -> list[dict]:
    """
    Build aligned sentence pairs enriched with source_quality and deviation_score.

    Joins three event streams from all_messages by ts:
      - final_spanish       → committed Spanish text
      - translation         → Google English translation
      - correction /
        translation_update  → LLM-improved English translation
      - segment_metadata    → source_quality, translation_register

    Returns a list of dicts sorted by elapsed_s (chronological):
      {
        ts, spanish, google_english, llm_english,
        source_quality, translation_register,
        deviation_score, reconstruction_risk,
        elapsed_s,
      }
    """
    all_msgs = result.get("all_messages", [])

    # Build per-ts aggregation buckets
    by_ts: dict[int, dict] = {}

    for m in all_msgs:
        ts = m.get("ts")
        if ts is None:
            continue
        entry = by_ts.setdefault(ts, {"ts": ts})

        mtype = m.get("type")
        if mtype == "final_spanish":
            entry["spanish"]   = m.get("text", "")
            entry["elapsed_s"] = m.get("_elapsed_s")

        elif mtype == "translation":
            entry["google_english"]  = m.get("english", "")
            # translation events also carry spanish — use as fallback
            if "spanish" not in entry:
                entry["spanish"] = m.get("spanish", "")

        elif mtype in ("correction", "translation_update"):
            # Keep the last correction per ts (most recent LLM output)
            entry["llm_english"] = m.get("english", "")

        elif mtype == "segment_metadata":
            entry["source_quality"]        = m.get("source_quality", "clean")
            entry["translation_register"]  = m.get("translation_register", "")

    # Assemble final pairs — require at minimum a Spanish source and Google translation
    pairs: list[dict] = []
    for ts_val, entry in by_ts.items():
        if "spanish" not in entry or "google_english" not in entry:
            continue

        spanish       = entry["spanish"]
        google        = entry["google_english"]
        llm           = entry.get("llm_english", "")
        source_quality = entry.get("source_quality", "clean")

        # Compute deviation only when there is an LLM translation to compare
        if llm and llm != google:
            dev_score = round(_deviation_score(google, llm), 3)
        else:
            # No LLM improvement — treat as identical
            dev_score = 1.0

        reconstruction_risk = (
            source_quality == "noisy"
            and llm
            and llm != google
            and dev_score < RECONSTRUCTION_RISK_THRESHOLD
        )

        pairs.append({
            "ts":                  ts_val,
            "spanish":             spanish,
            "google_english":      google,
            "llm_english":         llm,
            "source_quality":      source_quality,
            "translation_register": entry.get("translation_register", ""),
            "deviation_score":     dev_score,
            "reconstruction_risk": reconstruction_risk,
            "elapsed_s":           entry.get("elapsed_s"),
        })

    pairs.sort(key=lambda p: (p["elapsed_s"] is None, p["elapsed_s"] or 0))
    return pairs


def _empty_segment(segment_id: int, ts: int) -> dict:
    """Create a mutable segment state entry for event-stream replay."""
    return {
        "id": segment_id,
        "ts": ts,
        "spanish": "",
        "google_english": "",
        "llm_english": "",
        "source_quality": "clean",
        "translation_register": "",
        "elapsed_s": None,
        "member_ids": [segment_id],
        "absorbed_by": None,
    }


def _source_quality_rank(value: str) -> int:
    return {"clean": 0, "noisy": 1, "fragmented": 2}.get(value or "clean", 0)


def _combine_source_quality(values: list[str]) -> str:
    """Use the worst source-quality label across a merged caption chain."""
    if not values:
        return "clean"
    return max(values, key=_source_quality_rank)


def _merge_google_baseline(states: dict[int, dict], member_ids: list[int]) -> str:
    """Reconstruct a merged Google baseline from the caption chain members."""
    parts: list[str] = []
    for member_id in member_ids:
        google = (states.get(member_id, {}).get("google_english") or "").strip()
        if google:
            parts.append(google)
    return " ".join(parts).strip()


def _active_segments_for_ts(states: dict[int, dict], ts: int) -> list[dict]:
    return [
        seg for seg in states.values()
        if seg.get("ts") == ts and seg.get("absorbed_by") is None
    ]


def _pick_segment_for_google(active: list[dict], spanish: str) -> dict | None:
    for seg in active:
        if not seg.get("google_english") and seg.get("spanish") == spanish:
            return seg
    for seg in active:
        if not seg.get("google_english"):
            return seg
    return None


def _pick_segment_for_update(active: list[dict], english: str) -> dict | None:
    if not active:
        return None

    exact_google = [seg for seg in active if seg.get("google_english") == english]
    if exact_google:
        return exact_google[-1]

    exact_llm = [seg for seg in active if seg.get("llm_english") == english]
    if exact_llm:
        return exact_llm[-1]

    with_google = [seg for seg in active if seg.get("google_english")]
    if len(with_google) == 1:
        return with_google[0]

    return active[-1]


def _pick_segment_for_merge(active: list[dict], merged_spanish: str, prefer_latest: bool = False) -> dict | None:
    if not active:
        return None

    candidates = [
        seg for seg in active
        if seg.get("spanish") and seg["spanish"] in merged_spanish
    ]
    if candidates:
        candidates.sort(key=lambda seg: len(seg.get("spanish", "")), reverse=not prefer_latest)
        return candidates[0]

    return active[-1] if prefer_latest else active[0]


def _build_enriched_pairs(result: dict) -> list[dict]:
    """
    Build aligned sentence pairs enriched with source_quality and deviation_score.

    Replays the visible caption event stream rather than joining solely by ts.
    This keeps head-anchored caption merges aligned with the text the user
    actually saw on screen.
    """
    all_msgs = result.get("all_messages", [])
    ordered_msgs = sorted(
        enumerate(all_msgs),
        key=lambda item: (
            item[1].get("_elapsed_s") is None,
            item[1].get("_elapsed_s") or 0,
            item[0],
        ),
    )

    states: dict[int, dict] = {}
    next_segment_id = 1

    for _, m in ordered_msgs:
        mtype = m.get("type")

        if mtype == "caption_merge":
            keep_ts = m.get("ts_keep")
            absorb_ts = m.get("ts_absorb")
            if keep_ts is None or absorb_ts is None:
                continue

            merged_spanish = m.get("spanish", "") or ""
            keep = _pick_segment_for_merge(
                _active_segments_for_ts(states, keep_ts),
                merged_spanish,
            )
            absorb = _pick_segment_for_merge(
                _active_segments_for_ts(states, absorb_ts),
                merged_spanish,
                prefer_latest=True,
            )
            if keep is None or absorb is None:
                continue

            merged_members: list[int] = []
            for member in keep.get("member_ids", [keep["id"]]) + absorb.get("member_ids", [absorb["id"]]):
                if member not in merged_members:
                    merged_members.append(member)

            keep["member_ids"] = merged_members
            keep["spanish"] = merged_spanish or keep.get("spanish", "")
            keep["llm_english"] = m.get("english", "") or keep.get("llm_english", "")

            merged_google = _merge_google_baseline(states, merged_members)
            if merged_google:
                keep["google_english"] = merged_google

            quality_values = [
                states.get(member, {}).get("source_quality", "clean")
                for member in merged_members
            ]
            keep["source_quality"] = _combine_source_quality(quality_values)

            if not keep.get("translation_register"):
                keep["translation_register"] = absorb.get("translation_register", "")

            absorb["absorbed_by"] = keep["id"]
            continue

        ts = m.get("ts")
        if ts is None:
            continue

        if mtype == "final_spanish":
            entry = _empty_segment(next_segment_id, ts)
            next_segment_id += 1
            entry["spanish"] = m.get("text", "")
            entry["elapsed_s"] = m.get("_elapsed_s")
            states[entry["id"]] = entry

        elif mtype == "translation":
            active = _active_segments_for_ts(states, ts)
            entry = _pick_segment_for_google(active, m.get("spanish", ""))
            if entry is None:
                entry = _empty_segment(next_segment_id, ts)
                next_segment_id += 1
                states[entry["id"]] = entry
            entry["google_english"] = m.get("english", "") or entry.get("google_english", "")
            if not entry.get("spanish"):
                entry["spanish"] = m.get("spanish", "")

        elif mtype in ("correction", "translation_update"):
            # Keep the latest visible update for this ts until/unless a caption_merge
            # later reassigns that English onto the chain head.
            active = _active_segments_for_ts(states, ts)
            entry = _pick_segment_for_update(active, m.get("english", ""))
            if entry is None:
                entry = _empty_segment(next_segment_id, ts)
                next_segment_id += 1
                states[entry["id"]] = entry
            entry["llm_english"] = m.get("english", "") or entry.get("llm_english", "")

        elif mtype == "segment_metadata":
            active = _active_segments_for_ts(states, ts)
            entry = active[-1] if active else None
            if entry is None:
                entry = _empty_segment(next_segment_id, ts)
                next_segment_id += 1
                states[entry["id"]] = entry
            entry["source_quality"] = m.get("source_quality", "clean")
            entry["translation_register"] = m.get("translation_register", "")

    pairs: list[dict] = []
    for _, entry in sorted(states.items(), key=lambda item: item[0]):
        if entry.get("absorbed_by") is not None:
            continue

        spanish = entry.get("spanish", "")
        google = entry.get("google_english", "")
        if not spanish or not google:
            continue

        llm = entry.get("llm_english", "")
        source_quality = entry.get("source_quality", "clean")

        if llm and llm != google:
            dev_score = round(_deviation_score(google, llm), 3)
        else:
            dev_score = 1.0

        reconstruction_risk = (
            source_quality == "noisy"
            and llm
            and llm != google
            and dev_score < RECONSTRUCTION_RISK_THRESHOLD
        )

        pairs.append({
            "ts":                  entry["ts"],
            "spanish":             spanish,
            "google_english":      google,
            "llm_english":         llm,
            "source_quality":      source_quality,
            "translation_register": entry.get("translation_register", ""),
            "deviation_score":     dev_score,
            "reconstruction_risk": reconstruction_risk,
            "elapsed_s":           entry.get("elapsed_s"),
        })

    pairs.sort(key=lambda p: (p["elapsed_s"] is None, p["elapsed_s"] or 0))
    return pairs


def _chunk_pairs(pairs: list[dict], chunk_size: int) -> list[list[dict]]:
    """Split pairs into chunks of chunk_size for contextual evaluation."""
    return [pairs[i : i + chunk_size] for i in range(0, len(pairs), chunk_size)]


# ── Prompt builder ─────────────────────────────────────────────────────────────

def _format_pair(index: int, p: dict) -> str:
    """Format a single sentence pair for inclusion in the LLM prompt."""
    lines = [
        f"  [{index}] ES: {p['spanish']}",
        f"       Google EN: {p['google_english']}",
    ]

    llm_en = p.get("llm_english", "")
    if llm_en and llm_en != p["google_english"]:
        dev = p["deviation_score"]
        risk = " ⚠ RECONSTRUCTION_RISK" if p["reconstruction_risk"] else ""
        lines.append(
            f"       LLM EN:    {llm_en}  "
            f"[deviation_score={dev:.2f}{risk}]"
        )
    else:
        lines.append(f"       LLM EN:    (no change)")

    quality = p.get("source_quality", "clean")
    if quality != "clean":
        lines.append(f"       source_quality: {quality}")

    return "\n".join(lines)


def _build_chunk_prompt(
    chunk: list[dict],
    chunk_index: int,
    total_chunks: int,
    audio_dir: str,
    all_pairs: list[dict],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> str:
    """
    Build the evaluation prompt for a single chunk of sentence pairs.

    Includes preceding context sentences (up to 3) so the LLM can judge
    register consistency and narrative flow across chunk boundaries.
    """
    # Use chunk_size (not len(chunk)) so the last partial chunk gets the correct
    # absolute start index into all_pairs instead of a spuriously lower value.
    start_index = chunk_index * chunk_size

    # Preceding context — up to 3 sentences before this chunk
    context_start = max(0, start_index - 3)
    context_pairs  = all_pairs[context_start:start_index]

    context_block = ""
    if context_pairs:
        ctx_lines = [_format_pair(context_start + i, p) for i, p in enumerate(context_pairs)]
        context_block = (
            "\n[PRECEDING CONTEXT — for register/flow only, not to be evaluated]\n"
            + "\n".join(ctx_lines)
            + "\n"
        )

    # The chunk to evaluate
    chunk_lines = [
        _format_pair(start_index + i, p)
        for i, p in enumerate(chunk)
    ]
    chunk_block = "\n".join(chunk_lines)

    # Flag any reconstruction-risk pairs
    risk_pairs = [
        (start_index + i, p)
        for i, p in enumerate(chunk)
        if p["reconstruction_risk"]
    ]
    risk_note = ""
    if risk_pairs:
        indices = ", ".join(str(i) for i, _ in risk_pairs)
        risk_note = (
            f"\n⚠ RECONSTRUCTION RISK flagged for pair(s) {indices}. "
            "The LLM's deviation from Google was high on noisy STT input. "
            "These pairs may contain hallucinated content — verify carefully.\n"
        )

    indices_list = list(range(start_index, start_index + len(chunk)))

    return f"""You are a specialist translation quality reviewer for ChurchBridge AI.

ChurchBridge AI provides real-time Spanish → English caption translation for live
Pentecostal church services. Audio is transcribed via Deepgram STT, then translated
by Google Translate, and optionally improved by an LLM (Claude Haiku).

Your task: evaluate the English translation quality for the sentences below.
Audio set: {audio_dir}  |  Chunk {chunk_index + 1} of {total_chunks}
Sentence indices in this chunk: {indices_list}
{context_block}
[SENTENCES TO EVALUATE]
{chunk_block}
{risk_note}
EVALUATION CRITERIA:
1. Theological accuracy — does it preserve biblical/Pentecostal meaning?
2. Natural English register — is it fluent sermon English (not awkward)?
3. LLM improvement value — did the LLM version actually improve on Google?
   Low deviation_score means the LLM diverged heavily; this may be good (creative
   improvement) or bad (hallucination). Reconstruction risk pairs deserve extra scrutiny.
4. Completeness — is any meaning dropped or added?

RATING SCALE:
  5.0 = Excellent: natural, accurate, appropriate register throughout
  4.0 = Good: minor awkwardness or style issues, no meaning errors
  3.0 = Acceptable: some issues but core meaning preserved
  2.0 = Poor: significant meaning loss, register problems, or noticeable errors
  1.0 = Very poor: mistranslations, hallucinations, or unintelligible output

Respond ONLY with a JSON object matching this schema exactly:
{{
  "chunk_index": {chunk_index},
  "sentence_indices": {indices_list},
  "quality_rating": <float 1.0–5.0>,
  "llm_vs_google_winner": "<llm|google|tie|mixed>",
  "issues": ["<specific issue 1>", "..."],
  "highlights": ["<positive observation 1>", "..."],
  "notes": "<brief overall note for this chunk>"
}}

If there are no issues, return an empty list for "issues".
If there are no highlights, return an empty list for "highlights".
"""


def _build_summary_prompt(
    chunk_results: list[dict],
    flagged_pairs: list[dict],
    pairs: list[dict],
    audio_dir: str,
    model: str,
) -> str:
    """
    Build the final synthesis prompt that produces the overall quality rating,
    summary, flagged pair verdicts, and recommendations.
    """
    chunk_summary_lines = []
    for c in chunk_results:
        issues_str = "; ".join(c.get("issues") or []) or "none"
        chunk_summary_lines.append(
            f"  Chunk {c['chunk_index']}: rating={c.get('quality_rating')}, "
            f"winner={c.get('llm_vs_google_winner')}, "
            f"issues=[{issues_str}]"
        )
    chunk_summary = "\n".join(chunk_summary_lines) or "  (none)"

    flagged_lines = []
    for p in flagged_pairs:
        flagged_lines.append(
            f"  [{p['index']}] deviation={p['deviation_score']:.2f} "
            f"source_quality={p['source_quality']} "
            f"reconstruction_risk={p['reconstruction_risk']}\n"
            f"    ES: {p['spanish']}\n"
            f"    Google: {p['google']}\n"
            f"    LLM:    {p['llm']}"
        )
    flagged_block = "\n".join(flagged_lines) or "  (none)"

    indices_list = list(range(len(flagged_pairs)))

    return f"""You are a specialist translation quality reviewer for ChurchBridge AI.

Audio set: {audio_dir}  |  Model: {model}
Total sentence pairs evaluated: {len(pairs)}
Total flagged pairs (deviation_score < {FLAG_DEVIATION_THRESHOLD}): {len(flagged_pairs)}

CHUNK-BY-CHUNK RESULTS:
{chunk_summary}

FLAGGED PAIRS (high deviation from Google baseline):
{flagged_block}

Your task:
1. Synthesize an overall quality rating (1.0–5.0) across all chunks.
2. Write a brief overall summary (2–4 sentences) for a developer reading a benchmark report.
3. For each flagged pair, deliver a verdict: was the LLM divergence an improvement or a problem?
4. List up to 5 concrete, actionable recommendations for improving translation quality.

Respond ONLY with a JSON object matching this schema exactly:
{{
  "overall_quality_rating": <float 1.0–5.0>,
  "overall_summary": "<2–4 sentence summary>",
  "confidence": "<high|medium|low>",
  "flagged_pair_verdicts": [
    {{
      "index": <original pair index>,
      "verdict": "<brief assessment of this divergence>",
      "severity": "<high|medium|low>"
    }}
  ],
  "recommendations": ["<recommendation 1>", "..."]
}}
"""


# ── API call helpers ───────────────────────────────────────────────────────────

def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    return "\n".join(line for line in lines if not line.startswith("```")).strip()


def _extract_first_json_object(text: str) -> str:
    """
    Extract the first balanced JSON object from a model response.

    This tolerates brief wrapper text or markdown fences while still requiring
    the core payload to be a valid JSON object.
    """
    text = _strip_code_fences(text)
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in Claude response")

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    raise ValueError("Unterminated JSON object in Claude response")


def _parse_claude_json(text: str) -> dict:
    parsed = json.loads(_extract_first_json_object(text))
    if not isinstance(parsed, dict):
        raise ValueError("Claude response JSON was not an object")
    return parsed


def _close_truncated_json_object(text: str) -> str | None:
    """
    Try to repair a truncated JSON object by closing an open string and any
    still-open containers.

    This is intentionally conservative: it does not invent new keys or values,
    it only closes already-started syntax so a response cut off by max_tokens
    can still be parsed.
    """
    candidate = _strip_code_fences(text)
    start = candidate.find("{")
    if start == -1:
        return None
    candidate = candidate[start:].rstrip()

    closers: list[str] = []
    in_string = False
    escape = False
    for char in candidate:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            closers.append("}")
        elif char == "[":
            closers.append("]")
        elif char in ("}", "]"):
            if not closers or closers[-1] != char:
                return None
            closers.pop()

    if escape:
        candidate = candidate[:-1]
        escape = False

    if in_string:
        candidate += '"'

    candidate = candidate.rstrip()
    while candidate.endswith((",", ":")):
        candidate = candidate[:-1].rstrip()

    return candidate + "".join(reversed(closers))


def _parse_claude_json_with_repair(text: str) -> dict:
    try:
        return _parse_claude_json(text)
    except (json.JSONDecodeError, ValueError) as original_error:
        repaired = _close_truncated_json_object(text)
        if not repaired:
            raise original_error
        parsed = json.loads(repaired)
        if not isinstance(parsed, dict):
            raise original_error
        return parsed


def _call_claude(
    prompt: str,
    model: str,
    max_tokens: int = 1024,
    parse_retries: int = DEFAULT_PARSE_RETRIES,
) -> dict:
    """Make a Claude API call and retry if the response JSON is malformed."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed — run: pip install anthropic")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

    client = anthropic.Anthropic(api_key=api_key)
    retry_prompt = prompt
    attempt_max_tokens = max_tokens
    last_error: Exception | None = None

    for attempt in range(parse_retries + 1):
        message = client.messages.create(
            model=model,
            max_tokens=attempt_max_tokens,
            messages=[{"role": "user", "content": retry_prompt}],
        )

        raw = "".join(
            block.text for block in message.content
            if getattr(block, "type", "") == "text"
        ).strip()
        try:
            return _parse_claude_json_with_repair(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt >= parse_retries:
                break
            stop_reason = getattr(message, "stop_reason", None)
            if stop_reason == "max_tokens":
                attempt_max_tokens = min(attempt_max_tokens * 2, MAX_PARSE_RETRY_TOKENS)
            retry_prompt = (
                f"{prompt}\n\n"
                "Your previous response was truncated or not valid JSON. Reply "
                "again with ONLY one valid JSON object and no surrounding commentary. "
                "Keep the issues/highlights concise if needed."
            )

    assert last_error is not None
    raise ValueError(
        f"Could not parse Claude JSON after {parse_retries + 1} attempt(s): {last_error}"
    )


# ── Main evaluator ─────────────────────────────────────────────────────────────

def evaluate_translation_quality(
    result:      dict,
    pipeline_dir: Path,
    model:       str  = DEFAULT_MODEL,
    chunk_size:  int  = DEFAULT_CHUNK_SIZE,
) -> dict:
    """
    Evaluate translation quality for a pipeline run using Claude Opus.

    Extracts aligned sentence pairs, enriches each with deviation_score and
    source_quality, evaluates in chunks, then synthesizes an overall report.
    Persists the report to pipeline_dir/translation_quality/<run_id>.json.

    Returns the quality report dict.
    """
    run_id    = result["run_id"]
    audio_dir = result.get("audio_dir", "unknown")

    print(f"\n{'─' * 60}")
    print(f"  TRANSLATION QUALITY EVALUATION — {run_id}")
    print(f"{'─' * 60}")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return _error(run_id, audio_dir, model, chunk_size, "ANTHROPIC_API_KEY not set")

    # ── 1. Build enriched pairs ───────────────────────────────────────────────
    pairs = _build_enriched_pairs(result)
    if not pairs:
        return _error(run_id, audio_dir, model, chunk_size, "No committed sentence pairs found in result")

    print(f"  Pairs found   : {len(pairs)}")

    # ── 2. Identify flagged pairs (high deviation) ────────────────────────────
    flagged: list[dict] = []
    for i, p in enumerate(pairs):
        if p["deviation_score"] < FLAG_DEVIATION_THRESHOLD and p.get("llm_english"):
            flagged.append({
                "index":               i,
                "spanish":             p["spanish"],
                "google":              p["google_english"],
                "llm":                 p["llm_english"],
                "deviation_score":     p["deviation_score"],
                "source_quality":      p["source_quality"],
                "reconstruction_risk": p["reconstruction_risk"],
            })

    print(f"  Flagged pairs : {len(flagged)} (deviation_score < {FLAG_DEVIATION_THRESHOLD})")

    # ── 3. Evaluate each chunk ────────────────────────────────────────────────
    chunks = _chunk_pairs(pairs, chunk_size)
    total_chunks = len(chunks)
    print(f"  Chunks        : {total_chunks} (chunk_size={chunk_size})")
    print(f"  Model         : {model}")
    print()

    chunk_results: list[dict] = []
    for chunk_index, chunk in enumerate(chunks):
        print(f"  Evaluating chunk {chunk_index + 1}/{total_chunks}...")
        prompt = _build_chunk_prompt(chunk, chunk_index, total_chunks, audio_dir, pairs, chunk_size)
        try:
            chunk_result = _call_claude(prompt, model, max_tokens=512)
            # Ensure required fields are present
            chunk_result.setdefault("chunk_index", chunk_index)
            chunk_result.setdefault("sentence_indices", list(range(
                chunk_index * chunk_size,
                chunk_index * chunk_size + len(chunk),
            )))
            chunk_result.setdefault("quality_rating", 3.0)
            chunk_result.setdefault("llm_vs_google_winner", "tie")
            chunk_result.setdefault("issues", [])
            chunk_result.setdefault("highlights", [])
            chunk_result.setdefault("notes", "")
            chunk_results.append(chunk_result)
        except Exception as e:
            print(f"  ⚠ Chunk {chunk_index + 1} evaluation failed: {e}")
            chunk_results.append({
                "chunk_index":        chunk_index,
                "sentence_indices":   list(range(
                    chunk_index * chunk_size,
                    chunk_index * chunk_size + len(chunk),
                )),
                "quality_rating":     None,
                "llm_vs_google_winner": "unknown",
                "issues":             [f"Evaluation error: {e}"],
                "highlights":         [],
                "notes":              "Error during evaluation",
            })

    # ── 4. Synthesize overall rating ──────────────────────────────────────────
    print(f"\n  Synthesizing overall quality rating...")
    summary_result: dict[str, Any] = {}
    try:
        summary_prompt = _build_summary_prompt(chunk_results, flagged, pairs, audio_dir, model)
        summary_result = _call_claude(summary_prompt, model, max_tokens=1024)
    except Exception as e:
        print(f"  ⚠ Summary synthesis failed: {e}")
        # Fallback: compute average from chunk ratings
        ratings = [c["quality_rating"] for c in chunk_results if c.get("quality_rating") is not None]
        avg_rating = round(sum(ratings) / len(ratings), 2) if ratings else None
        summary_result = {
            "overall_quality_rating": avg_rating,
            "overall_summary":        f"Summary synthesis failed: {e}",
            "confidence":             "low",
            "flagged_pair_verdicts":  [],
            "recommendations":        [],
        }

    # ── 5. Merge flagged pair verdicts into flagged list ──────────────────────
    verdicts_by_index: dict[int, dict] = {}
    for v in (summary_result.get("flagged_pair_verdicts") or []):
        idx = v.get("index")
        if idx is not None:
            verdicts_by_index[idx] = v

    for fp in flagged:
        verdict_entry = verdicts_by_index.get(fp["index"], {})
        fp["verdict"]  = verdict_entry.get("verdict", "")
        fp["severity"] = verdict_entry.get("severity", "low")

    # ── 6. Assemble final report ──────────────────────────────────────────────
    report: dict[str, Any] = {
        "run_id":                 run_id,
        "audio_dir":              audio_dir,
        "model":                  model,
        "chunk_size":             chunk_size,
        "pair_count":             len(pairs),
        "overall_quality_rating": summary_result.get("overall_quality_rating"),
        "overall_summary":        summary_result.get("overall_summary", ""),
        "confidence":             summary_result.get("confidence", "low"),
        "chunk_evaluations":      chunk_results,
        "flagged_pairs":          flagged,
        "recommendations":        summary_result.get("recommendations", []),
    }

    # ── 7. Persist quality report ─────────────────────────────────────────────
    out_dir = pipeline_dir / "translation_quality"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Saved: {out_path}")

    # ── 8. Patch scalar metrics back into the scorecard ───────────────────────
    # This is the integration point with the self-improvement loop.
    # trajectory.py reads scorecards/ and builds rolling time series for every
    # field it finds. By writing quality scalars into the scorecard's `quality`
    # section, they flow automatically into trajectory → review → llm_interpret
    # → SELF_IMPROVEMENT_REPORT without any further plumbing.
    _patch_scorecard(pipeline_dir, run_id, report)

    _print_quality_summary(report)

    return report


def _patch_scorecard(pipeline_dir: Path, run_id: str, report: dict) -> None:
    """
    Write quality scalar metrics into the existing scorecard for this run.

    trajectory.py flattens all scorecard sections via _extract_series(), so
    any field added here automatically gets trend detection, delta tracking,
    and tier-priority evaluation on the next cycle — no changes needed there.

    Fields written to scorecard["quality"]:
      translation_quality_rating     — overall LLM quality score (1.0–5.0)
      translation_quality_confidence — "high"|"medium"|"low"
      translation_flagged_pair_count — pairs with deviation_score < 0.50
      translation_reconstruction_risk_count — pairs with reconstruction risk
      translation_llm_win_chunk_count  — chunks where LLM beat Google
      translation_google_win_chunk_count — chunks where Google beat LLM
      translation_mixed_chunk_count    — chunks with mixed verdict
      translation_pair_count           — total pairs evaluated
    """
    scorecard_path = pipeline_dir / "scorecards" / f"{run_id}.json"
    if not scorecard_path.exists():
        print(f"  ⚠ Scorecard not found at {scorecard_path} — quality scalars not patched")
        return

    raw = scorecard_path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            scorecard = json.loads(raw.decode(enc))
            break
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    else:
        print(f"  ⚠ Could not decode scorecard — quality scalars not patched")
        return

    chunks = report.get("chunk_evaluations") or []
    winners = [c.get("llm_vs_google_winner") for c in chunks]
    flagged = report.get("flagged_pairs") or []
    risk_count = sum(1 for p in flagged if p.get("reconstruction_risk"))

    scorecard["quality"] = {
        "translation_quality_rating":          report.get("overall_quality_rating"),
        "translation_quality_confidence":      report.get("confidence"),
        "translation_flagged_pair_count":      len(flagged),
        "translation_reconstruction_risk_count": risk_count,
        "translation_llm_win_chunk_count":     winners.count("llm"),
        "translation_google_win_chunk_count":  winners.count("google"),
        "translation_mixed_chunk_count":       winners.count("mixed"),
        "translation_pair_count":              report.get("pair_count", 0),
    }

    scorecard_path.write_text(
        json.dumps(scorecard, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Scorecard patched with quality scalars: {scorecard_path}")


def _print_quality_summary(report: dict) -> None:
    """Print a human-readable quality summary to stdout."""
    rating    = report.get("overall_quality_rating")
    summary   = report.get("overall_summary", "")
    conf      = report.get("confidence", "?")
    flagged   = report.get("flagged_pairs") or []
    chunks    = report.get("chunk_evaluations") or []
    recs      = report.get("recommendations") or []

    rating_str = f"{rating:.1f}/5.0" if rating is not None else "n/a"
    print(f"\n  Overall quality : {rating_str}  (confidence: {conf})")
    if summary:
        print(f"  Summary         : {summary[:140]}")

    winners = [c.get("llm_vs_google_winner") for c in chunks]
    llm_wins = winners.count("llm")
    google_wins = winners.count("google")
    mixed    = winners.count("mixed")
    ties     = winners.count("tie")
    print(
        f"  LLM vs Google   : {llm_wins} LLM wins / {google_wins} Google wins / "
        f"{mixed} mixed / {ties} ties (across {len(chunks)} chunks)"
    )

    if flagged:
        high_risk = [f for f in flagged if f.get("severity") == "high"]
        print(f"  Flagged pairs   : {len(flagged)} total, {len(high_risk)} high-severity")

    if recs:
        print(f"\n  Recommendations:")
        for r in recs[:3]:
            print(f"    • {r}")


def _error(run_id: str, audio_dir: str, model: str, chunk_size: int, msg: str) -> dict:
    print(f"  ⚠ Translation quality evaluation skipped: {msg}")
    return {
        "run_id":                 run_id,
        "audio_dir":              audio_dir,
        "model":                  model,
        "chunk_size":             chunk_size,
        "pair_count":             0,
        "overall_quality_rating": None,
        "overall_summary":        f"[ERROR] {msg}",
        "confidence":             "low",
        "chunk_evaluations":      [],
        "flagged_pairs":          [],
        "recommendations":        [],
    }


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main():
    """
    Evaluate translation quality for an existing pipeline run JSON.
    Useful for running the quality evaluation without re-running the full pipeline.

    Usage:
        python tests/benchmark/llm_translation_quality.py <run_json_path> [--chunk-size N] [--model M]
    """
    import argparse

    parser = argparse.ArgumentParser(description="ChurchBridge AI — Translation Quality Evaluator")
    parser.add_argument("run_json", help="Path to the pipeline run JSON file")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f"Sentences per evaluation chunk (default: {DEFAULT_CHUNK_SIZE})")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help=f"Claude model to use (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    run_path = Path(args.run_json)
    if not run_path.exists():
        print(f"File not found: {run_path}")
        sys.exit(1)

    from tests.benchmark.storage import load_json_with_fallback
    result = load_json_with_fallback(run_path)

    # Place output alongside the run JSON in a translation_quality/ subdirectory
    pipeline_dir = run_path.parent

    report = evaluate_translation_quality(
        result,
        pipeline_dir,
        model=args.model,
        chunk_size=args.chunk_size,
    )

    print(f"\nFull report:\n{json.dumps(report, indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
