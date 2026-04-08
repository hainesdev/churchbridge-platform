#!/usr/bin/env python3
"""
ChurchBridge AI — Evaluation Loop Orchestrator
===============================================
Coordinates a single closed-loop evaluation cycle after a benchmark run:

  result dict
    → scorecard      (deterministic normalisation)
    → trajectory     (deterministic rolling stats)
    → review         (deterministic action recommendation)
    → LLM interpret  (pattern analysis, one proposed change)
    → cycle log      (append entry for long-term memory)
    → SELF_IMPROVEMENT_REPORT.md (agent handoff document)

Separation of concerns:
  - scorecard / trajectory / review  → facts and tier-priority decisions only
  - llm_interpret                    → interpretation and targeted proposals only
  - cycle_log                        → memory across many cycles
  - orchestrator                     → wires them together, nothing more

The orchestrator NEVER:
  - overrides the deterministic action recommendation
  - applies code or directive changes autonomously
  - proposes directive changes on a single run

It ALWAYS:
  - records every cycle in the log, even when collect_more_runs fires
  - writes SELF_IMPROVEMENT_REPORT.md so the next agent session has context
  - returns a structured dict that run_pipeline_test.py can log or display

Usage (called from run_pipeline_test.py):
    from tests.benchmark.orchestrator import run_evaluation_cycle
    cycle = run_evaluation_cycle(result, pipeline_dir)

Usage (standalone — re-evaluate an existing run without re-running the benchmark):
    python tests/benchmark/orchestrator.py <run_json_path>
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from tests.benchmark.scorecard    import generate_scorecard
from tests.benchmark.trajectory   import compute_trajectory
from tests.benchmark.review       import write_review, build_review, _action_recommendation, _flat
from tests.benchmark.llm_interpret import interpret_run
from tests.benchmark.cycle_log    import (
    record_cycle, load_cycle_log, open_proposals,
    pending_directive_proposals, cycle_summary_text, cycles_for_audio_dir,
)
from tests.benchmark.storage import load_json_with_fallback

REPORT_PATH = ROOT / "SELF_IMPROVEMENT_REPORT.md"


# ── Action label deciding whether to call the LLM ─────────────────────────────

# LLM adds value for all actions except collect_more_runs — but we still call
# it for collect_more_runs so the "open_questions" field is always populated.
# For collect_more_runs the interpreter returns a canned stub (no API call made).
_LLM_ACTIONS = {
    "collect_more_runs",
    "investigate",
    "revert_or_fix",
    "promote",
    "propose_directive_update",
}


# ── Orchestrator ───────────────────────────────────────────────────────────────

def run_evaluation_cycle(
    result:       dict,
    pipeline_dir: Path,
    use_llm:      bool = True,
    cycle_log_path: Path | None = None,
    report_path: Path | None = None,
) -> dict:
    """
    Run a full evaluation cycle for one benchmark result.

    pipeline_dir is the .../results/<audio_dir_name>/pipeline/ directory.
    Returns a cycle summary dict.
    """
    run_id         = result["run_id"]
    audio_dir_name = Path(result.get("audio_dir", "")).name or pipeline_dir.parent.name

    print(f"\n{'-' * 60}")
    print(f"  EVALUATION CYCLE -- {run_id}")
    print(f"{'-' * 60}")

    # ── 1. Scorecard ──────────────────────────────────────────────────────────
    scorecard = generate_scorecard(
        result,
        pipeline_dir / "scorecards" / f"{run_id}.json",
    )

    # ── 2. Trajectory ─────────────────────────────────────────────────────────
    trajectory = compute_trajectory(pipeline_dir)

    # ── 3. Review (deterministic) ─────────────────────────────────────────────
    flat    = _flat(scorecard)
    metrics = trajectory.get("metrics", {})
    action, reasons = _action_recommendation(scorecard, trajectory, flat)

    review_md   = build_review(scorecard, trajectory)
    review_path = pipeline_dir / "reviews" / f"{run_id}.md"
    write_review(scorecard, trajectory, review_path)

    # ── 4. LLM interpretation ─────────────────────────────────────────────────
    llm_analysis: dict | None = None

    if use_llm and action in _LLM_ACTIONS:
        cycle_history = load_cycle_log(cycle_log_path)
        llm_analysis  = interpret_run(
            scorecard, trajectory, result, review_md, cycle_history, action
        )
        _print_llm_summary(llm_analysis)
    else:
        print(f"  LLM interpreter skipped (use_llm={use_llm}, action={action})")

    # ── 5. Cycle log ──────────────────────────────────────────────────────────
    cycle_entry = record_cycle(
        benchmark_run_id = run_id,
        scorecard        = scorecard,
        trajectory       = trajectory,
        review_action    = action,
        llm_analysis     = llm_analysis,
        cycle_log_path   = cycle_log_path,
    )

    # ── 6. SELF_IMPROVEMENT_REPORT.md ─────────────────────────────────────────
    _write_report(
        audio_dir_name,
        trajectory,
        cycle_entry,
        action,
        llm_analysis,
        cycle_log_path=cycle_log_path,
        report_path=report_path,
    )

    print(f"\n  Action: {action.upper()}")
    if llm_analysis and llm_analysis.get("proposed_change", {}).get("type") not in (None, "none"):
        change = llm_analysis["proposed_change"]
        print(f"  Proposal: [{change['type']}] {change.get('target')} - {change.get('description', '')[:80]}")

    return {
        "cycle_id":      cycle_entry["cycle_id"],
        "run_id":        run_id,
        "action":        action,
        "reasons":       reasons,
        "llm_analysis":  llm_analysis,
        "scorecard":     scorecard,
        "trajectory":    trajectory,
        "review_path":   str(review_path),
        "report_path":   str(report_path or REPORT_PATH),
    }


# ── Report helpers ─────────────────────────────────────────────────────────────

def _report_benchmark_sections(
    current_audio_dir_name: str,
    current_trajectory: dict,
    report_path: Path | None,
) -> list[tuple[str, dict]]:
    """
    For the staggered regime, summarize every benchmark set under the results
    root so the report reflects the whole comparable batch. Legacy reports keep
    the historical single-set behavior.
    """
    target_report = report_path or REPORT_PATH
    results_root = target_report.parent

    if results_root.name != "staggered":
        return [(current_audio_dir_name, current_trajectory)]

    sections: list[tuple[str, dict]] = []
    for candidate in sorted(results_root.iterdir()):
        trajectory_path = candidate / "pipeline" / "trajectory.json"
        if not trajectory_path.exists():
            continue
        sections.append((candidate.name, load_json_with_fallback(trajectory_path)))

    if not sections:
        return [(current_audio_dir_name, current_trajectory)]

    sections.sort(key=lambda item: (item[0] != current_audio_dir_name, item[0]))
    return sections


def _aggregate_report_action(
    default_action: str,
    benchmark_sections: list[tuple[str, dict]],
    cycle_log_path: Path | None,
) -> str:
    """
    Compute a conservative report action across all benchmark sets in the regime.
    Falls back to the current run action when per-set history is unavailable.
    """
    action_priority = {
        "revert_or_fix": 0,
        "investigate": 1,
        "collect_more_runs": 2,
        "propose_directive_update": 3,
        "promote": 4,
    }
    section_names = {name for name, _ in benchmark_sections}
    if not section_names:
        return default_action

    latest_by_set: dict[str, str] = {}
    for cycle in load_cycle_log(cycle_log_path):
        set_name = str(cycle.get("audio_dir_name", ""))
        action = cycle.get("review_action")
        if set_name in section_names and isinstance(action, str):
            latest_by_set[set_name] = action

    if not latest_by_set:
        return default_action

    aggregate_action = default_action
    aggregate_rank = action_priority.get(default_action, 999)
    for action in latest_by_set.values():
        rank = action_priority.get(action, 999)
        if rank < aggregate_rank:
            aggregate_action = action
            aggregate_rank = rank
    return aggregate_action


# ── SELF_IMPROVEMENT_REPORT.md ─────────────────────────────────────────────────

def _write_report(
    audio_dir_name: str,
    trajectory:     dict,
    cycle_entry:    dict,
    action:         str,
    llm_analysis:   dict | None,
    cycle_log_path: Path | None = None,
    report_path: Path | None = None,
) -> None:
    """
    Write SELF_IMPROVEMENT_REPORT.md — the handoff document for the next
    agent session. It answers: "where does the system stand right now?"
    """
    ts    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = []

    benchmark_sections = _report_benchmark_sections(
        audio_dir_name, trajectory, report_path
    )
    report_action = _aggregate_report_action(action, benchmark_sections, cycle_log_path)

    lines += [
        f"# Self-Improvement Report",
        f"",
        f"Generated: {ts}",
        f"",
        f"This document is auto-generated after each benchmark run. It gives",
        f"an autonomous agent the context needed to start the next improvement",
        f"cycle without re-reading every artifact from scratch.",
        f"",
        f"---",
        f"",
        f"## Current Action",
        f"",
        f"**`{report_action}`**",
        f"",
    ]

    # Action guidance
    guidance = {
        "promote": (
            "The pipeline is stable and improving. Consider merging or tagging "
            "this commit, then identify the next target from the roadmap."
        ),
        "investigate": (
            "A metric needs investigation. Read the LLM analysis below, then "
            "inspect the specific event log entries named as evidence."
        ),
        "revert_or_fix": (
            "A Tier-1 guardrail has regressed. Do not promote. Either revert "
            "the last code change or apply the targeted fix below."
        ),
        "collect_more_runs": (
            "Insufficient data to draw conclusions. Run the benchmark at least "
            "once more, ideally on both audio sets, before interpreting trends."
        ),
        "propose_directive_update": (
            "Repeated evidence suggests a gap in DIRECTIVE.md. Review the "
            "proposed diff below. Require human sign-off before applying."
        ),
    }
    lines += [
        f"*{guidance.get(report_action, '')}*",
        f"",
        f"---",
        f"",
    ]

    snapshot_keys = [
        "wer_committed_pct", "wer_raw_pct",
        "out_of_order_event_count", "orphan_correction_count",
        "fragment_leak_count", "duplicate_commit_count",
        "time_to_first_translation_s", "wall_time_s",
    ]

    if len(benchmark_sections) == 1:
        lines += [
            f"## Benchmark Set: {benchmark_sections[0][0]}",
            f"",
        ]
    else:
        lines += [
            "## Benchmark Sets",
            "",
        ]

    for index, (section_name, section_trajectory) in enumerate(benchmark_sections):
        metrics = section_trajectory.get("metrics", {})
        if len(benchmark_sections) > 1:
            lines += [
                f"### Set {section_name}",
                "",
            ]
        lines += [
            f"**Runs in history:** {section_trajectory.get('n_runs', 0)}  "
            f"(confidence: {section_trajectory.get('confidence', 'unknown')})",
            "",
            "**Key Metrics (current run):**",
            "",
        ]
        for k in snapshot_keys:
            entry   = metrics.get(k, {})
            current = entry.get("current")
            trend   = entry.get("trend", "n/a")
            delta   = entry.get("delta_vs_prev")
            suffix  = "%" if "pct" in k else ("s" if k.endswith("_s") else "")
            if current is not None:
                sign = "+" if (delta or 0) >= 0 else ""
                d_str = f"{sign}{delta:.2f}{suffix}" if delta is not None else "n/a"
                lines.append(f"- `{k}`: {current}{suffix}  (d:{d_str}, {trend})")
        lines.append("")
        if index != len(benchmark_sections) - 1:
            lines += ["---", ""]

    lines += ["---", ""]

    # LLM analysis section
    if llm_analysis:
        lines += [
            "## LLM Pattern Analysis",
            "",
            f"**Confidence:** {llm_analysis.get('confidence', 'n/a')}",
            "",
            "**Pattern analysis:**",
            "",
            llm_analysis.get("pattern_analysis", "n/a"),
            "",
            "**Root cause hypothesis:**",
            "",
            llm_analysis.get("root_cause_hypothesis", "n/a"),
            "",
        ]

        quotes = llm_analysis.get("evidence_quotes") or []
        if quotes:
            lines += ["**Evidence:**", ""]
            for q in quotes:
                lines.append(f"> {q}")
            lines += [""]

        change = llm_analysis.get("proposed_change") or {}
        if change.get("type") not in (None, "none"):
            lines += [
                "## Proposed Change",
                "",
                f"**Type:** {change.get('type')}",
                f"**Target:** `{change.get('target')}`",
                "",
                change.get("description", ""),
                "",
            ]
            if change.get("current_text"):
                lines += [
                    "**Current wording:**",
                    "```",
                    change["current_text"],
                    "```",
                    "",
                ]
            if change.get("proposed_text"):
                lines += [
                    "**Proposed wording:**",
                    "```",
                    change["proposed_text"],
                    "```",
                    "",
                ]

        questions = llm_analysis.get("open_questions") or []
        if questions:
            lines += ["## Open Questions for Next Run", ""]
            for q in questions:
                lines.append(f"- {q}")
            lines += [""]

    lines += ["---", ""]

    # Open proposals from all prior cycles
    proposals = open_proposals(cycle_log_path)
    if proposals:
        lines += [
            "## Unresolved Proposals",
            "",
            f"{len(proposals)} proposal(s) from prior cycles have not yet been applied:",
            "",
        ]
        for p in proposals:
            analysis = p.get("llm_analysis") or {}
            change   = analysis.get("proposed_change") or {}
            lines.append(
                f"- Cycle `{p['cycle_id']}` (commit `{p.get('git_commit', '?')}`) — "
                f"[{change.get('type', '?')}] `{change.get('target', '?')}`: "
                f"{change.get('description', '')[:80]}"
            )
        lines += [""]

    # Pending directive proposals
    directive_proposals = pending_directive_proposals(cycle_log_path)
    if directive_proposals:
        lines += [
            "## Pending Directive Proposals (require human review)",
            "",
        ]
        for p in directive_proposals:
            analysis = p.get("llm_analysis") or {}
            change   = analysis.get("proposed_change") or {}
            lines += [
                f"### Cycle `{p['cycle_id']}`",
                "",
                f"**Target:** `{change.get('target', '?')}`",
                "",
                change.get("description", ""),
                "",
            ]
            if change.get("current_text"):
                lines += ["**Current:**", "```", change["current_text"], "```", ""]
            if change.get("proposed_text"):
                lines += ["**Proposed:**", "```", change["proposed_text"], "```", ""]

    # Recent cycle history
    all_cycles    = load_cycle_log(cycle_log_path)
    recent_cycles = all_cycles[-8:]
    if recent_cycles:
        lines += [
            "## Recent Cycle History",
            "",
            "```",
            cycle_summary_text(recent_cycles),
            "```",
            "",
        ]

    lines += [
        "---",
        "",
        "_Generated by `tests/benchmark/orchestrator.py`. Do not edit manually._",
    ]

    target_report = report_path or REPORT_PATH
    target_report.parent.mkdir(parents=True, exist_ok=True)
    target_report.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report   : {target_report}")


# ── LLM summary printer ────────────────────────────────────────────────────────

def _print_llm_summary(analysis: dict) -> None:
    conf = analysis.get("confidence", "?")
    print(f"  LLM confidence: {conf}")

    hyp = analysis.get("root_cause_hypothesis", "")
    if hyp:
        print(f"  Hypothesis: {hyp[:120]}")

    change = analysis.get("proposed_change") or {}
    if change.get("type") not in (None, "none"):
        print(f"  Proposed [{change['type']}]: {change.get('target')} - {change.get('description', '')[:80]}")


# ── Standalone CLI ─────────────────────────────────────────────────────────────

def main():
    """
    Re-run the evaluation cycle for an existing benchmark run JSON.
    Useful for regenerating the report or testing the LLM interpreter
    without re-running the full pipeline.
    """
    if len(sys.argv) < 2:
        print("Usage: orchestrator.py <run_json_path> [--no-llm]")
        print("  re-runs the evaluation cycle for an existing result file")
        sys.exit(1)

    run_path = Path(sys.argv[1])
    use_llm  = "--no-llm" not in sys.argv

    result = load_json_with_fallback(run_path)

    pipeline_dir = run_path.parent
    cycle = run_evaluation_cycle(result, pipeline_dir, use_llm=use_llm)
    print(f"\nCycle complete. Report at: {cycle['report_path']}")


if __name__ == "__main__":
    main()
