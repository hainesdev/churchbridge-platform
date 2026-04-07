#!/usr/bin/env python3
"""
ChurchBridge AI — Cycle Log
============================
Tracks each self-improvement cycle across benchmark runs.

A "cycle" is one iteration of the loop:
  run benchmark → evaluate → (LLM interpret) → (propose change) → outcome

The cycle log is the system's memory across many runs. Without it, each LLM
interpretation session starts from scratch. With it, the interpreter can see
patterns like "we fixed fragment_leak in cycle 3 and it caused more
out_of_order events in cycle 4."

Stored at: tests/benchmark/results/cycle_log.json

Each entry:
{
  "cycle_id":               ISO timestamp string
  "benchmark_run_id":       run_id from the benchmark run
  "audio_dir":              e.g. "tests/audio/1"
  "audio_dir_name":         e.g. "1"
  "git_commit":             short commit hash
  "scorecard_version":      "1"

  "review_action":          promote | investigate | revert_or_fix |
                            collect_more_runs | propose_directive_update
  "n_runs_at_time":         how many runs existed when this cycle ran

  "llm_analysis": {
    "pattern_analysis":      str
    "root_cause_hypothesis": str
    "confidence":            high | medium | low
    "proposed_change": {
      "type":         code | directive | none
      "target":       file:function or DIRECTIVE.md section or none
      "description":  str
      "current_text": str | null
      "proposed_text": str | null
    }
    "open_questions": [str]
  } | null,                 -- null when collect_more_runs or API unavailable

  "change_applied": {       -- filled in after the human/agent acts on the proposal
    "type":        code | directive | none
    "description": str
    "commit":      str | null
    "pr":          str | null
  } | null,

  "outcome": "pending" | "improved" | "regressed" | "neutral" | "superseded"
  "outcome_notes": str | null
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT        = Path(__file__).parent.parent.parent
CYCLE_LOG   = ROOT / "tests" / "benchmark" / "results" / "cycle_log.json"


# ── Load / save ────────────────────────────────────────────────────────────────

def load_cycle_log() -> list[dict]:
    """Return the full cycle log, or [] if it does not exist yet."""
    if not CYCLE_LOG.exists():
        return []
    raw = CYCLE_LOG.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return []


def _save(log: list[dict]) -> None:
    CYCLE_LOG.parent.mkdir(parents=True, exist_ok=True)
    CYCLE_LOG.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Write ──────────────────────────────────────────────────────────────────────

def record_cycle(
    benchmark_run_id: str,
    scorecard:        dict,
    trajectory:       dict,
    review_action:    str,
    llm_analysis:     dict | None = None,
) -> dict:
    """
    Append a new cycle entry and return it.
    llm_analysis may be None if the LLM was not called (e.g. collect_more_runs
    without API key).
    """
    log     = load_cycle_log()
    cycle_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

    entry: dict[str, Any] = {
        "cycle_id":           cycle_id,
        "benchmark_run_id":   benchmark_run_id,
        "audio_dir":          scorecard.get("audio_dir", "unknown"),
        "audio_dir_name":     Path(scorecard.get("audio_dir", "")).name,
        "git_commit":         scorecard.get("git_commit", "unknown"),
        "scorecard_version":  scorecard.get("scorecard_version", "1"),

        "review_action":      review_action,
        "n_runs_at_time":     trajectory.get("n_runs", 0),

        "llm_analysis":       llm_analysis,
        "change_applied":     None,
        "outcome":            "pending",
        "outcome_notes":      None,
    }

    log.append(entry)
    _save(log)
    print(f"Cycle log: {CYCLE_LOG}  ({len(log)} cycles)")
    return entry


def update_outcome(
    cycle_id:     str,
    outcome:      str,
    notes:        str | None            = None,
    change:       dict | None           = None,
) -> None:
    """
    Update a cycle entry after the human/agent has acted on the proposal and
    the next benchmark run has revealed the outcome.

    outcome: "improved" | "regressed" | "neutral" | "superseded"
    change:  {"type": ..., "description": ..., "commit": ..., "pr": ...}
    """
    log = load_cycle_log()
    for entry in log:
        if entry.get("cycle_id") == cycle_id:
            entry["outcome"]       = outcome
            entry["outcome_notes"] = notes
            if change:
                entry["change_applied"] = change
            _save(log)
            return
    print(f"Warning: cycle_id {cycle_id} not found in cycle log")


# ── Query helpers ──────────────────────────────────────────────────────────────

def cycles_for_audio_dir(audio_dir_name: str) -> list[dict]:
    """Return all cycles for a specific benchmark audio set."""
    return [
        c for c in load_cycle_log()
        if c.get("audio_dir_name") == audio_dir_name
    ]


def last_n_cycles(n: int = 10) -> list[dict]:
    """Return the most recent N cycles across all audio sets."""
    return load_cycle_log()[-n:]


def open_proposals() -> list[dict]:
    """
    Return cycles where a change was proposed but not yet applied.
    These are action items for the next agent session.
    """
    log = load_cycle_log()
    return [
        c for c in log
        if c.get("outcome") == "pending"
        and c.get("llm_analysis") is not None
        and (c.get("llm_analysis") or {}).get("proposed_change", {}).get("type") != "none"
        and c.get("change_applied") is None
    ]


def pending_directive_proposals() -> list[dict]:
    """Return cycles with a directive change proposed but not yet applied."""
    return [
        c for c in open_proposals()
        if (c.get("llm_analysis") or {}).get("proposed_change", {}).get("type") == "directive"
    ]


# ── Summary ────────────────────────────────────────────────────────────────────

def cycle_summary_text(cycles: list[dict]) -> str:
    """Return a compact multi-line summary of a list of cycles for display."""
    if not cycles:
        return "  (no cycles recorded)"
    lines = []
    for c in cycles:
        proposal = "(no proposal)"
        analysis = c.get("llm_analysis") or {}
        change   = analysis.get("proposed_change") or {}
        if change.get("type") not in (None, "none"):
            proposal = f"{change['type']} → {change.get('target', '?')}"

        lines.append(
            f"  {c['cycle_id']}  commit={c.get('git_commit', '?')}"
            f"  action={c.get('review_action', '?')}  outcome={c.get('outcome', '?')}"
            f"  [{proposal}]"
        )
    return "\n".join(lines)
