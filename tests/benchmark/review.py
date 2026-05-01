#!/usr/bin/env python3
"""
ChurchBridge AI — Run Review Generator
========================================
Reads a scorecard and the trajectory for its benchmark set, then writes a
structured markdown review with four sections:

  1. Run summary
  2. Primary findings
  3. Likely causes
  4. Action recommendation (exactly one)

Output path: tests/benchmark/results/<audio_dir>/pipeline/reviews/<run_id>.md

Action recommendations (mutually exclusive, in priority order):
    revert_or_fix         — Tier-1 guardrail has regressed
    investigate           — Tier-1 metric is degraded but needs more data
    collect_more_runs     — fewer than 3 comparable runs, or noisy signal
    propose_directive_update — repeated evidence reveals a directive gap
    promote               — guardrails hold, Tier-2 improved or flat

Usage (standalone):
    python tests/benchmark/review.py <scorecard_json> <trajectory_json>

Usage (from run_pipeline_test.py):
    from tests.benchmark.review import write_review
    write_review(scorecard, trajectory, out_path)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────────

TIER1_KEYS = [
    "out_of_order_event_count",
    "orphan_correction_count",
    "duplicate_commit_count",
    "stale_correction_suppression_count",
    "incorrect_merge_suspect_count",
    "display_ready_violation_count",
]

TIER2_KEYS = [
    "wer_committed_pct",
    "theological_term_recall",
    "theological_term_precision",
    "translation_count",
    "time_to_first_translation_s",
    "time_to_first_committed_sentence_s",
]

TIER3_KEYS = [
    "wer_raw_pct",
    "committed_sentence_count",
    "wall_time_s",
    "llm_correction_count",
    "mode_flip_count",
    "verse_event_count",
    "fragment_leak_count",
    "avg_translation_latency_s",
    "avg_llm_correction_latency_s",
    "client_visible_rewrite_count",
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def _flat(scorecard: dict) -> dict[str, Any]:
    """Flatten accuracy/latency/behavioral into a single dict."""
    out: dict[str, Any] = {}
    for group in ("accuracy", "latency", "behavioral"):
        out.update(scorecard.get(group, {}))
    return out


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def _signed(delta: float | None) -> str:
    if delta is None:
        return "n/a"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.2f}"


def _metric_entry(key: str, metrics: dict, flat: dict) -> str:
    """Format one metric line for the summary table."""
    entry   = metrics.get(key, {})
    current = flat.get(key)
    delta   = entry.get("delta_vs_prev")
    trend   = entry.get("trend", "n/a")
    suffix  = "%" if "pct" in key else ("s" if key.endswith("_s") else "")
    return (
        f"  - `{key}`: {_fmt(current, suffix)}  "
        f"(d:{_signed(delta)}{suffix}, trend: {trend})"
    )


# ── Decision logic ─────────────────────────────────────────────────────────────

def _action_recommendation(
    scorecard: dict,
    trajectory: dict,
    flat: dict,
    benchmark_context: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """
    Return (action_label, [reason_lines]).
    Applies rules in strict priority order per the evaluation plan.
    """
    metrics = trajectory.get("metrics", {})
    n_runs  = trajectory.get("n_runs", 0)
    coverage = trajectory.get("coverage", {})
    benchmark_context = benchmark_context or {}
    reasons: list[str] = []

    # ── Priority 1: Tier-1 guardrail regression ────────────────────────────────
    tier1_regressions = [
        k for k in TIER1_KEYS
        if metrics.get(k, {}).get("trend") == "regressed"
        and flat.get(k) is not None
        and flat.get(k, 0) > 0
    ]
    if tier1_regressions:
        for k in tier1_regressions:
            delta = metrics[k].get("delta_vs_prev")
            reasons.append(
                f"`{k}` regressed (current: {_fmt(flat.get(k))}, "
                f"d:{_signed(delta)})"
            )
        if n_runs >= 3:
            return "revert_or_fix", reasons
        else:
            return "investigate", reasons

    # ── Priority 2: Insufficient data ─────────────────────────────────────────
    if n_runs < 3:
        reasons.append(
            f"Only {n_runs} run(s) recorded for this benchmark set. "
            "Need at least 3 comparable runs before drawing conclusions."
        )
        return "collect_more_runs", reasons

    # ── Priority 3: Noisy signal ───────────────────────────────────────────────
    noisy_keys = [
        k for k in TIER2_KEYS
        if metrics.get(k, {}).get("trend") == "noisy"
    ]
    if len(noisy_keys) >= 2:
        reasons.append(
            f"High variance in Tier-2 metrics: {', '.join(f'`{k}`' for k in noisy_keys)}. "
            "Signal is unreliable."
        )
        return "collect_more_runs", reasons

    # ── Priority 4: Directive gap evidence ────────────────────────────────────
    # This fires only when a metric has been "regressed" or "noisy" across the
    # full long window — suggesting the directive may need updating.
    long_regressions = [
        k for k in TIER2_KEYS
        if metrics.get(k, {}).get("trend") == "regressed"
        and (metrics.get(k, {}).get("long") or {}).get("n", 0) >= 5
    ]
    if long_regressions:
        reasons.append(
            f"Sustained Tier-2 regression across long window "
            f"({', '.join(f'`{k}`' for k in long_regressions)}). "
            "Directive may lack explicit guardrail or ranking for this tradeoff."
        )
        coverage_reasons = _promotion_coverage_gaps(coverage, benchmark_context)
        if coverage_reasons:
            return "collect_more_runs", reasons + coverage_reasons
        return "propose_directive_update", reasons

    # ── Priority 5: Promote ────────────────────────────────────────────────────
    tier2_improved = [
        k for k in TIER2_KEYS
        if metrics.get(k, {}).get("trend") == "improved"
        and flat.get(k) is not None
    ]
    tier2_regressed = [
        k for k in TIER2_KEYS
        if metrics.get(k, {}).get("trend") == "regressed"
        and flat.get(k) is not None
    ]

    if tier2_improved and not tier2_regressed:
        reasons.append(
            f"Tier-1 guardrails hold. Tier-2 metrics improved: "
            f"{', '.join(f'`{k}`' for k in tier2_improved)}."
        )
        coverage_reasons = _promotion_coverage_gaps(coverage, benchmark_context)
        if coverage_reasons:
            return "collect_more_runs", reasons + coverage_reasons
        return "promote", reasons

    if not tier2_regressed:
        reasons.append("Tier-1 guardrails hold. No Tier-2 regressions detected.")
        coverage_reasons = _promotion_coverage_gaps(coverage, benchmark_context)
        if coverage_reasons:
            return "collect_more_runs", reasons + coverage_reasons
        return "promote", reasons

    # Mixed signals
    reasons.append(
        f"Tier-2 mixed: improved={[k for k in tier2_improved]}, "
        f"regressed={[k for k in tier2_regressed]}. "
        "Needs further investigation."
    )
    return "investigate", reasons


def _promotion_coverage_gaps(
    coverage: dict[str, Any],
    benchmark_context: dict[str, Any],
) -> list[str]:
    """Identify coverage gaps that should block promote-level conclusions."""
    reasons: list[str] = []
    results_root_name = benchmark_context.get("results_root_name")

    if results_root_name == "staggered":
        if not coverage.get("has_zero_offset_baseline"):
            reasons.append(
                "This staggered set has no `0s` baseline window yet. Add a clean baseline clip "
                "before treating offset-window results as promotion evidence."
            )
        if not coverage.get("has_nonzero_offset_window"):
            reasons.append(
                "This staggered set has no non-zero offset stress window yet. Add echo/noise-prone "
                "offset windows before treating the set as robust."
            )

        complete_sets = int(benchmark_context.get("staggered_complete_set_count", 0))
        total_sets = int(benchmark_context.get("staggered_set_count", 0))
        if total_sets < 2 or complete_sets < 2:
            reasons.append(
                "The staggered regime is not yet complete across benchmark sets. Wait until at least "
                "two sets each have comparable baseline and offset windows before promoting."
            )

    return reasons


# ── Cause inference ────────────────────────────────────────────────────────────

def _infer_causes(flat: dict, metrics: dict) -> list[str]:
    """
    Pattern-match metric combinations to generate human-readable cause hypotheses.
    Returns a list of bullet strings.
    """
    causes: list[str] = []

    wer_raw   = flat.get("wer_raw_pct")
    wer_comm  = flat.get("wer_committed_pct")
    raw_trend = (metrics.get("wer_raw_pct") or {}).get("trend")
    com_trend = (metrics.get("wer_committed_pct") or {}).get("trend")

    if raw_trend == "flat" and com_trend == "regressed":
        causes.append(
            "STT quality is unchanged but committed WER worsened — "
            "likely a regression in sentence buffering, cleaning, or flush logic."
        )

    if raw_trend == "improved" and com_trend != "improved":
        causes.append(
            "Raw STT improved but committed quality did not follow — "
            "the buffering or merge layer may be limiting gains."
        )

    corr_trend = (metrics.get("llm_correction_count") or {}).get("trend")
    verse_trend = (metrics.get("verse_event_count") or {}).get("trend")
    if corr_trend == "regressed" and com_trend not in ("improved", "flat"):
        causes.append(
            "LLM correction volume increased without visible quality gain — "
            "corrections may be churning or arriving stale."
        )

    if verse_trend == "regressed":
        causes.append(
            "Verse event count rose — check whether verse detection is "
            "over-triggering (precision may have fallen)."
        )

    orphan_trend = (metrics.get("orphan_correction_count") or {}).get("trend")
    if orphan_trend == "regressed":
        causes.append(
            "Orphan corrections increased — corrections arriving after "
            "their anchor sentence has been replaced or cleared."
        )

    oo_trend = (metrics.get("out_of_order_event_count") or {}).get("trend")
    if oo_trend == "regressed":
        causes.append(
            "Out-of-order events increased — async completion or reconnect "
            "may be violating display ordering guarantees."
        )

    frag_trend = (metrics.get("fragment_leak_count") or {}).get("trend")
    if frag_trend == "regressed":
        causes.append(
            "Fragment leaks increased — very short committed sentences suggest "
            "premature flushes from the sentence buffer."
        )

    if not causes:
        causes.append("No dominant cause pattern identified from available metrics.")

    return causes


# ── Findings ───────────────────────────────────────────────────────────────────

def _primary_findings(metrics: dict, flat: dict) -> tuple[list[str], list[str], list[str]]:
    """Return (improved, regressed, weak_or_noisy) lists of formatted strings."""
    improved, regressed, weak = [], [], []

    all_keys = TIER1_KEYS + TIER2_KEYS + TIER3_KEYS
    for k in all_keys:
        entry = metrics.get(k)
        if entry is None:
            continue
        trend   = entry.get("trend")
        current = flat.get(k)
        if current is None:
            continue
        suffix = "%" if "pct" in k else ("s" if k.endswith("_s") else "")
        delta  = entry.get("delta_vs_prev")
        tier   = entry.get("tier") or "?"
        line   = f"`{k}` = {_fmt(current, suffix)} (d:{_signed(delta)}{suffix}) [Tier {tier}]"
        if trend == "improved":
            improved.append(line)
        elif trend == "regressed":
            regressed.append(line)
        elif trend in ("noisy", "insufficient_data"):
            weak.append(f"`{k}` — {trend}")

    return improved, regressed, weak


# ── Markdown builder ───────────────────────────────────────────────────────────

def build_review(
    scorecard: dict,
    trajectory: dict,
    benchmark_context: dict[str, Any] | None = None,
) -> str:
    flat    = _flat(scorecard)
    metrics = trajectory.get("metrics", {})
    n_runs  = trajectory.get("n_runs", 0)
    conf    = trajectory.get("confidence", "unknown")

    action, reasons = _action_recommendation(scorecard, trajectory, flat, benchmark_context)
    improved, regressed, weak = _primary_findings(metrics, flat)
    causes = _infer_causes(flat, metrics)

    run_id     = scorecard.get("run_id", "unknown")
    git_commit = scorecard.get("git_commit", "unknown")
    audio_dir  = scorecard.get("audio_dir", "unknown")

    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────────────
    lines.append(f"# Benchmark Review — {run_id}")
    lines.append("")

    # ── Section 1: Run summary ─────────────────────────────────────────────────
    lines.append("## 1. Run Summary")
    lines.append("")
    lines.append(f"- **Run ID**: `{run_id}`")
    lines.append(f"- **Commit**: `{git_commit}`")
    lines.append(f"- **Benchmark set**: `{audio_dir}`")
    lines.append(f"- **Clip duration**: {_fmt(scorecard.get('clip_duration_s'), 's')}")
    lines.append(f"- **Runs in history**: {n_runs}  (confidence: {conf})")
    if scorecard.get("note"):
        lines.append(f"- **Note**: {scorecard['note']}")
    lines.append("")
    lines.append("**Key metric values:**")
    lines.append("")
    for k in ["wer_raw_pct", "wer_committed_pct", "committed_sentence_count",
              "translation_count", "llm_correction_count", "verse_event_count",
              "wall_time_s", "time_to_first_translation_s",
              "out_of_order_event_count", "orphan_correction_count",
              "fragment_leak_count", "duplicate_commit_count"]:
        lines.append(_metric_entry(k, metrics, flat))
    lines.append("")

    # ── Section 2: Primary findings ────────────────────────────────────────────
    lines.append("## 2. Primary Findings")
    lines.append("")

    if improved:
        lines.append("**Improved:**")
        for item in improved:
            lines.append(f"- {item}")
        lines.append("")
    else:
        lines.append("**Improved:** none detected")
        lines.append("")

    if regressed:
        lines.append("**Regressed:**")
        for item in regressed:
            lines.append(f"- {item}")
        lines.append("")
    else:
        lines.append("**Regressed:** none detected")
        lines.append("")

    if weak:
        lines.append("**Noisy or insufficient data:**")
        for item in weak:
            lines.append(f"- {item}")
        lines.append("")

    # ── Section 3: Likely causes ───────────────────────────────────────────────
    lines.append("## 3. Likely Causes")
    lines.append("")
    for cause in causes:
        lines.append(f"- {cause}")
    lines.append("")

    # ── Section 4: Action recommendation ──────────────────────────────────────
    lines.append("## 4. Action Recommendation")
    lines.append("")
    lines.append(f"**`{action}`**")
    lines.append("")
    for reason in reasons:
        lines.append(f"- {reason}")
    lines.append("")

    action_guidance = {
        "promote": (
            "The pipeline is in good shape for this benchmark set. "
            "Consider merging or tagging this commit as a verified improvement."
        ),
        "investigate": (
            "Do not promote yet. Inspect the event log for the regressed metrics. "
            "Check recent code changes for likely causes before the next run."
        ),
        "revert_or_fix": (
            "A Tier-1 guardrail has regressed. "
            "Either revert the offending commit or make a targeted fix before the next run. "
            "Do not promote until the guardrail is restored."
        ),
        "collect_more_runs": (
            "Run the benchmark at least one more time without code changes "
            "to separate real signal from noise."
        ),
        "propose_directive_update": (
            "Review `DIRECTIVE.md` against the repeated evidence above. "
            "Draft a directive change with the exact current wording, proposed wording, "
            "and evidence from at least 3 runs before applying any change."
        ),
    }
    lines.append(f"*{action_guidance.get(action, '')}*")
    lines.append("")

    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────

def write_review(
    scorecard: dict,
    trajectory: dict,
    out_path: Path,
    benchmark_context: dict[str, Any] | None = None,
) -> str:
    """Build and write the markdown review. Returns the markdown string."""
    md = build_review(scorecard, trajectory, benchmark_context)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"Review   : {out_path}")
    return md


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print("Usage: review.py <scorecard_json> <trajectory_json> [out_md]")
        sys.exit(1)

    sc_path   = Path(sys.argv[1])
    traj_path = Path(sys.argv[2])

    scorecard  = json.loads(sc_path.read_text(encoding="utf-8"))
    trajectory = json.loads(traj_path.read_text(encoding="utf-8"))

    if len(sys.argv) >= 4:
        out_path = Path(sys.argv[3])
    else:
        reviews_dir = sc_path.parent.parent / "reviews"
        out_path    = reviews_dir / f"{scorecard['run_id']}.md"

    md = write_review(scorecard, trajectory, out_path)
    print(md)


if __name__ == "__main__":
    main()
