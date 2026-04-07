#!/usr/bin/env python3
"""
ChurchBridge AI — Scorecard Generator
======================================
Reads a full pipeline run JSON and produces a normalized scorecard with three
metric groups:

  A. Accuracy     — did the system say the right thing?
  B. Latency      — does it feel live and stable?
  C. Behavioral   — does it obey DIRECTIVE.md architecture promises?

Output path: tests/benchmark/results/<audio_dir>/pipeline/scorecards/<run_id>.json

Usage (standalone):
    python tests/benchmark/scorecard.py <run_json_path>
    python tests/benchmark/scorecard.py tests/benchmark/results/1/pipeline/2026-04-07T15-30-27Z.json

Usage (from run_pipeline_test.py):
    from tests.benchmark.scorecard import generate_scorecard
    scorecard = generate_scorecard(result_dict, out_path)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


# ── Helpers ────────────────────────────────────────────────────────────────────

def _first_elapsed(messages: list[dict], *types: str) -> float | None:
    """Return the earliest _elapsed_s among messages matching any of the given types."""
    matches = [
        m["_elapsed_s"]
        for m in messages
        if m.get("type") in types and "_elapsed_s" in m
    ]
    return round(min(matches), 3) if matches else None


def _avg_latency(pairs: list[tuple[float, float]]) -> float | None:
    """
    Given a list of (committed_elapsed_s, event_elapsed_s) pairs, return the
    mean lag between the committed sentence and its downstream event.
    """
    lags = [e - c for c, e in pairs if e >= c]
    if not lags:
        return None
    return round(sum(lags) / len(lags), 3)


def _event_elapsed(message: dict) -> float | None:
    """Read elapsed seconds from either normalized layer rows or raw event rows."""
    elapsed = message.get("_elapsed_s")
    if elapsed is None:
        elapsed = message.get("elapsed_s")
    return elapsed


def _check_ts_ordering(committed_sentences: list[dict]) -> int:
    """
    Count committed sentences whose ts field is strictly less than the previous
    committed-sentence ts — a proxy for out-of-order delivery.
    Restricting this to final_spanish events avoids false positives from
    downstream updates that intentionally reuse an earlier sentence ts.
    """
    ts_seq = [m["ts"] for m in committed_sentences if "ts" in m]
    violations = sum(1 for i in range(1, len(ts_seq)) if ts_seq[i] < ts_seq[i - 1])
    return violations


def _detect_duplicate_commits(committed_sentences: list[dict]) -> int:
    """
    Count committed sentences whose text is identical to an earlier one.
    Exact duplicates suggest a double-commit bug.
    """
    seen: set[str] = set()
    dupes = 0
    for m in committed_sentences:
        txt = (m.get("text") or "").strip()
        if txt in seen:
            dupes += 1
        seen.add(txt)
    return dupes


def _translation_latencies(
    committed: list[dict], translations: list[dict]
) -> list[tuple[float, float]]:
    """
    Pair each committed sentence with its nearest translation by ts, then
    return (committed_elapsed_s, translation_elapsed_s) pairs.
    """
    pairs: list[tuple[float, float]] = []
    for c in committed:
        c_ts = c.get("ts")
        c_elapsed = _event_elapsed(c)
        if c_ts is None or c_elapsed is None:
            continue
        # find the translation with the matching ts (same sentence ts)
        match = next((t for t in translations if t.get("ts") == c_ts), None)
        match_elapsed = _event_elapsed(match) if match else None
        if match_elapsed is not None:
            pairs.append((c_elapsed, match_elapsed))
    return pairs


def _correction_latencies(
    translations: list[dict], corrections: list[dict]
) -> list[tuple[float, float]]:
    """
    Pair each translation with its LLM correction by ts, then return
    (translation_elapsed_s, correction_elapsed_s) pairs.
    """
    pairs: list[tuple[float, float]] = []
    for t in translations:
        t_ts = t.get("ts")
        t_elapsed = _event_elapsed(t)
        if t_ts is None or t_elapsed is None:
            continue
        match = next((c for c in corrections if c.get("ts") == t_ts), None)
        match_elapsed = _event_elapsed(match) if match else None
        if match_elapsed is not None:
            pairs.append((t_elapsed, match_elapsed))
    return pairs


def _count_client_visible_rewrites(
    translations: list[dict], corrections: list[dict]
) -> int:
    """
    Count only downstream correction events that actually rewrite an existing
    translation. Matching ts is required, and identical English text is not a
    user-visible rewrite.
    """
    translation_by_ts = {
        t.get("ts"): (t.get("english") or "").strip()
        for t in translations
        if t.get("ts") is not None
    }
    rewrites = 0
    for correction in corrections:
        ts = correction.get("ts")
        if ts not in translation_by_ts:
            continue
        original = translation_by_ts[ts]
        updated = (correction.get("english") or "").strip()
        if updated and updated != original:
            rewrites += 1
    return rewrites


# ── Core builder ───────────────────────────────────────────────────────────────

def build_scorecard(result: dict) -> dict:
    """
    Derive a canonical scorecard from a full pipeline run result dict.
    All fields that cannot be computed are set to None rather than omitted,
    so the schema stays stable across runs.
    """
    layers   = result.get("layers", {})
    all_msgs = result.get("all_messages", [])

    raw_layer       = layers.get("raw_deepgram", {})
    committed_layer = layers.get("committed_sentences", {})
    translations    = layers.get("translations", [])      # list of dicts
    corrections     = layers.get("llm_corrections", [])   # list of dicts
    verse_events    = layers.get("verse_events", [])
    mode_changes    = layers.get("mode_changes", [])
    seg_metadata    = layers.get("segment_metadata", [])

    # Rebuild message lists from all_messages (needed for _elapsed_s on committed)
    committed_msgs   = [m for m in all_msgs if m.get("type") == "final_spanish"]
    translation_msgs = [m for m in all_msgs if m.get("type") == "translation"]
    correction_msgs  = [m for m in all_msgs if m.get("type") in ("correction", "translation_update")]

    # ── A. Accuracy ────────────────────────────────────────────────────────────

    wer_raw       = raw_layer.get("wer") or {}
    wer_committed = committed_layer.get("wer") or {}

    accuracy: dict[str, Any] = {
        "wer_raw_pct":              wer_raw.get("score_pct"),
        "wer_raw_substitutions":    wer_raw.get("substitutions"),
        "wer_raw_deletions":        wer_raw.get("deletions"),
        "wer_raw_insertions":       wer_raw.get("insertions"),
        "wer_committed_pct":        wer_committed.get("score_pct"),
        "wer_committed_substitutions": wer_committed.get("substitutions"),
        "wer_committed_deletions":  wer_committed.get("deletions"),
        "wer_committed_insertions": wer_committed.get("insertions"),
        "committed_sentence_count": committed_layer.get("sentence_count", 0),
        "translation_count":        len(translations),
        "llm_correction_count":     len(corrections),
        "verse_event_count":        len(verse_events),
        # Placeholders for evaluators that require reference NLP (future work)
        "scripture_reference_recall":    None,
        "scripture_reference_precision": None,
        "theological_term_recall":       None,
        "theological_term_precision":    None,
        "merge_accuracy_rate":           None,
    }

    # ── B. Latency and Flow ────────────────────────────────────────────────────

    time_to_first_translation = _first_elapsed(all_msgs, "translation")
    time_to_first_committed   = _first_elapsed(all_msgs, "final_spanish")
    time_to_first_correction  = _first_elapsed(all_msgs, "correction", "translation_update")

    trans_lat_pairs  = _translation_latencies(committed_msgs, translation_msgs)
    corr_lat_pairs   = _correction_latencies(translation_msgs, correction_msgs)

    # deferred releases: sentences suppressed because display_ready=false.
    # The pipeline emits pending_completion=True in segment_metadata for these.
    deferred_pending_ts = {
        m.get("ts") for m in seg_metadata if m.get("pending_completion")
    }

    # deferred timeouts: suppressed sentences where the 6-second timeout fired.
    # Identified by a translation_update arriving for the ts without a prior
    # caption_merge absorbing it (when a merge absorbs the ts, the deferred
    # task is cancelled and no translation_update is emitted for that ts).
    caption_merge_absorbed_ts = {
        m.get("ts_absorb") for m in all_msgs if m.get("type") == "caption_merge"
    }
    correction_ts = {m.get("ts") for m in correction_msgs}
    deferred_timeout_ts = {
        ts for ts in deferred_pending_ts
        if ts in correction_ts and ts not in caption_merge_absorbed_ts
    }

    # stale correction suppression — look for suppressed events in all_messages
    stale_suppressions = len([
        m for m in all_msgs if m.get("type") == "correction_suppressed"
    ])

    # caption merges — caption_merge events in all_messages
    caption_merges = len([m for m in all_msgs if m.get("type") == "caption_merge"])

    latency: dict[str, Any] = {
        "wall_time_s":                       result.get("wall_time_s"),
        "clip_duration_s":                   result.get("clip_duration_s"),
        "time_to_first_translation_s":       time_to_first_translation,
        "time_to_first_committed_sentence_s": time_to_first_committed,
        "time_to_first_llm_correction_s":    time_to_first_correction,
        "avg_translation_latency_s":         _avg_latency(trans_lat_pairs),
        "avg_llm_correction_latency_s":      _avg_latency(corr_lat_pairs),
        "deferred_release_count":            len(deferred_pending_ts),
        "deferred_release_timeout_count":    len(deferred_timeout_ts),
        "stale_correction_suppression_count": stale_suppressions,
        "caption_merge_count":               caption_merges,
    }

    # ── C. Behavioral Integrity ────────────────────────────────────────────────

    out_of_order   = _check_ts_ordering(committed_msgs)
    duplicate_commits = _detect_duplicate_commits(committed_msgs)

    # orphan corrections — corrections whose ts doesn't match any translation
    translation_ts_set = {t.get("ts") for t in translation_msgs}
    orphan_corrections = sum(
        1 for c in correction_msgs
        if c.get("ts") not in translation_ts_set
    )

    # fragment leaks — committed sentences that are very short (< 3 words),
    # suggesting a flushed fragment rather than a complete sentence
    fragment_leaks = sum(
        1 for m in committed_msgs
        if len((m.get("text") or "").split()) < 3
    )

    # client-visible rewrites — corrections that differ from the original
    # translation for the same ts (already covered by llm_correction_count,
    # but here we distinguish rewrites from additions)
    client_visible_rewrites = _count_client_visible_rewrites(
        translation_msgs, correction_msgs
    )

    behavioral: dict[str, Any] = {
        "out_of_order_event_count":       out_of_order,
        "orphan_correction_count":        orphan_corrections,
        "fragment_leak_count":            fragment_leaks,
        "incorrect_merge_suspect_count":  None,   # requires NLP comparison (future)
        "duplicate_commit_count":         duplicate_commits,
        "mode_flip_count":                len(mode_changes),
        "client_visible_rewrite_count":   client_visible_rewrites,
        "display_ready_violation_count":  None,   # requires display-ready flag in events (future)
    }

    # ── Assemble ───────────────────────────────────────────────────────────────

    return {
        "run_id":      result["run_id"],
        "git_commit":  result.get("git_commit"),
        "audio_dir":   result.get("audio_dir"),
        "audio_file":  result.get("audio_file"),
        "clip_duration_s": result.get("clip_duration_s"),
        "note":        result.get("note", ""),
        "scorecard_version": "1",
        "accuracy":   accuracy,
        "latency":    latency,
        "behavioral": behavioral,
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_scorecard(result: dict, out_path: Path) -> dict:
    """
    Build and persist a scorecard for the given result dict.
    Returns the scorecard dict.
    """
    scorecard = build_scorecard(result)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(scorecard, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Scorecard: {out_path}")
    return scorecard


def _read_json(path: Path) -> dict:
    """Read a JSON file, trying UTF-8 first then falling back to latin-1."""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"Could not decode {path}")


def scorecard_from_file(run_json_path: Path) -> dict:
    """Load a run JSON file and return its scorecard (does not write to disk)."""
    result = _read_json(run_json_path)
    return build_scorecard(result)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: scorecard.py <run_json_path> [out_path]")
        sys.exit(1)

    run_path = Path(sys.argv[1])
    if not run_path.exists():
        print(f"File not found: {run_path}")
        sys.exit(1)

    result = _read_json(run_path)

    if len(sys.argv) >= 3:
        out_path = Path(sys.argv[2])
    else:
        # Default: place scorecard alongside the run JSON in scorecards/
        out_path = run_path.parent / "scorecards" / f"{result['run_id']}.json"

    scorecard = generate_scorecard(result, out_path)
    print(json.dumps(scorecard, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
