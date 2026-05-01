#!/usr/bin/env python3
"""
ChurchBridge AI — Trajectory Analyzer
======================================
Reads history.json and the scorecards/ directory for a benchmark set and
produces a trajectory summary with rolling statistics, deltas, and trend labels.

Output path: tests/benchmark/results/<audio_dir>/pipeline/trajectory.json
             tests/benchmark/results/aggregate/trajectory.json  (cross-set)

Windows:
    short  = last 3 runs
    medium = last 5 runs
    long   = last 10 runs

Trend labels per metric:
    improved           — directional improvement over the short window
    regressed          — directional regression over the short window
    flat               — delta within noise threshold
    noisy              — high variance relative to mean
    insufficient_data  — fewer than 3 runs

Usage (standalone):
    python tests/benchmark/trajectory.py <audio_dir_name>
    python tests/benchmark/trajectory.py 1
    python tests/benchmark/trajectory.py 1 2      # aggregate across sets

Usage (from run_pipeline_test.py):
    from tests.benchmark.trajectory import compute_trajectory
    traj = compute_trajectory(results_pipeline_dir)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Any

ROOT = Path(__file__).parent.parent.parent

# ── Config ─────────────────────────────────────────────────────────────────────

SHORT_WINDOW  = 3
MEDIUM_WINDOW = 5
LONG_WINDOW   = 10

# Fraction of mean below which variance is considered "flat" noise
FLAT_THRESHOLD_FRACTION = 0.03

# Fraction of mean above which variance is considered "noisy"
NOISY_THRESHOLD_FRACTION = 0.15

# Metrics where lower is better (used to determine direction of improvement)
LOWER_IS_BETTER = {
    "wer_raw_pct",
    "wer_committed_pct",
    "wer_raw_substitutions",
    "wer_raw_deletions",
    "wer_raw_insertions",
    "wer_committed_substitutions",
    "wer_committed_deletions",
    "wer_committed_insertions",
    "wall_time_s",
    "time_to_first_translation_s",
    "time_to_first_committed_sentence_s",
    "time_to_first_llm_correction_s",
    "avg_translation_latency_s",
    "avg_llm_correction_latency_s",
    "deferred_release_count",
    "deferred_release_timeout_count",
    "stale_correction_suppression_count",
    "low_confidence_final_count",
    "low_confidence_final_rate",
    "out_of_order_event_count",
    "orphan_correction_count",
    "fragment_leak_count",
    "incorrect_merge_suspect_count",
    "duplicate_commit_count",
    "mode_flip_count",
    "display_ready_violation_count",
    # Translation quality — lower flagged/risk counts are better
    "translation_flagged_pair_count",
    "translation_reconstruction_risk_count",
    "translation_google_win_chunk_count",
}

# Metrics whose trend labels are unreliable when clip_duration_s changes significantly
# across runs. WER variance grows sharply with fewer committed sentences (shorter clips
# produce fewer reference words, so each error has proportionally more impact).
_CLIP_SENSITIVE_METRICS = {
    "wer_committed_pct",
    "wer_raw_pct",
    "committed_sentence_count",
    "translation_count",
    "llm_correction_count",
    "verse_event_count",
}

# If the latest run's clip_duration_s differs from the prior runs' mean by more than
# this fraction, WER and count metrics are marked "noisy" regardless of trend direction.
_CLIP_DURATION_CHANGE_THRESHOLD = 0.40

# Tier classification for each metric key (used by reviewer)
TIER = {
    # Tier 1 — hard guardrails
    "out_of_order_event_count":       1,
    "orphan_correction_count":        1,
    "duplicate_commit_count":         1,
    "stale_correction_suppression_count": 1,
    "incorrect_merge_suspect_count":  1,
    "display_ready_violation_count":  1,
    # Tier 2 — primary optimization targets
    "wer_committed_pct":              2,
    "theological_term_recall":        2,
    "theological_term_precision":     2,
    "translation_count":              2,
    "time_to_first_translation_s":    2,
    "time_to_first_committed_sentence_s": 2,
    # Tier 3 — supportive diagnostics
    "wer_raw_pct":                    3,
    "stt_final_count":                3,
    "avg_stt_confidence":             3,
    "min_stt_confidence":             3,
    "low_confidence_final_count":     3,
    "low_confidence_final_rate":      3,
    "committed_sentence_count":       3,
    "wall_time_s":                    3,
    "llm_correction_count":           3,
    "mode_flip_count":                3,
    "verse_event_count":              3,
    "fragment_leak_count":            3,
    "avg_translation_latency_s":      3,
    "avg_llm_correction_latency_s":   3,
    "client_visible_rewrite_count":   3,
    # Translation quality metrics (Tier 2 — primary optimization target for output quality)
    "translation_quality_rating":            2,
    "translation_flagged_pair_count":        2,
    "translation_reconstruction_risk_count": 2,
    "translation_llm_win_chunk_count":       3,
    "translation_google_win_chunk_count":    3,
    "translation_mixed_chunk_count":         3,
    "translation_pair_count":                3,
}


# ── Stats helpers ──────────────────────────────────────────────────────────────

def _window_stats(values: list[float], n: int) -> dict | None:
    """Return mean/min/max over the last n values, or None if not enough data."""
    window = [v for v in values[-n:] if v is not None]
    if not window:
        return None
    result: dict[str, Any] = {
        "n":    len(window),
        "mean": round(mean(window), 4),
        "min":  round(min(window), 4),
        "max":  round(max(window), 4),
    }
    if len(window) >= 2:
        result["stdev"] = round(stdev(window), 4)
    return result


def _trend_label(values: list[float], key: str) -> str:
    """
    Classify the trend of a metric given its recent value history.
    Requires at least 3 non-None values to label anything beyond insufficient_data.
    """
    clean = [v for v in values if v is not None]
    if len(clean) < 3:
        return "insufficient_data"

    window = clean[-SHORT_WINDOW:]
    m = mean(window)
    if m == 0:
        return "flat"

    # Variance check
    if len(window) >= 2:
        sd = stdev(window)
        if sd / abs(m) > NOISY_THRESHOLD_FRACTION:
            return "noisy"

    # Direction check: compare last value to the mean of the prior window
    prior = clean[-(SHORT_WINDOW + 1):-1]
    if not prior:
        return "insufficient_data"

    delta = clean[-1] - mean(prior)
    if abs(delta) / abs(m) < FLAT_THRESHOLD_FRACTION:
        return "flat"

    if key in LOWER_IS_BETTER:
        return "improved" if delta < 0 else "regressed"
    else:
        return "improved" if delta > 0 else "regressed"


def _delta(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None:
        return None
    return round(current - previous, 4)


# ── Scorecard loading ──────────────────────────────────────────────────────────

def _load_scorecards(pipeline_dir: Path) -> list[dict]:
    """
    Load all scorecard JSON files from pipeline_dir/scorecards/, sorted by
    run_id (which is an ISO timestamp, so lexicographic sort = chronological).
    """
    sc_dir = pipeline_dir / "scorecards"
    if not sc_dir.exists():
        return []
    files = sorted(sc_dir.glob("*.json"))
    cards = []
    for f in files:
        raw = f.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                cards.append(json.loads(raw.decode(enc)))
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
    return cards


def _extract_series(scorecards: list[dict]) -> dict[str, list[float | None]]:
    """
    Build {metric_key: [value, ...]} time series from an ordered list of scorecards.
    Flattens accuracy, latency, and behavioral sub-dicts.
    Also surfaces clip_duration_s from the scorecard top level so the trajectory
    can detect regime changes across runs.
    """
    series: dict[str, list[float | None]] = {}
    for sc in scorecards:
        for group in ("accuracy", "latency", "behavioral", "quality"):
            for k, v in sc.get(group, {}).items():
                series.setdefault(k, []).append(v if isinstance(v, (int, float)) else None)
        # Expose clip_duration_s at the top level so compute_trajectory can detect
        # when the benchmark regime changed (different clip length across runs).
        cd = sc.get("clip_duration_s")
        series.setdefault("clip_duration_s", []).append(
            cd if isinstance(cd, (int, float)) else None
        )
        offset = sc.get("clip_start_offset_s")
        series.setdefault("clip_start_offset_s", []).append(
            offset if isinstance(offset, (int, float)) else None
        )
    return series


def _clip_duration_regime_changed(clip_durations: list[float | None]) -> bool:
    """Return True if the latest run's clip duration differs substantially from prior runs.

    A 'substantial' change is defined as the latest value deviating from the prior
    runs' mean by more than _CLIP_DURATION_CHANGE_THRESHOLD (default 40%).
    When this is true, WER and sentence-count metrics should not be compared
    directly against earlier runs — they're not measuring the same thing.
    """
    clean = [v for v in clip_durations if v is not None]
    if len(clean) < 2:
        return False
    latest = clean[-1]
    prior_mean = mean(clean[:-1])
    if prior_mean == 0:
        return False
    return abs(latest - prior_mean) / prior_mean > _CLIP_DURATION_CHANGE_THRESHOLD


def _coverage_summary(scorecards: list[dict]) -> dict[str, Any]:
    """Summarize clip-window diversity for this benchmark set."""
    offsets = [
        round(float(sc["clip_start_offset_s"]), 3)
        for sc in scorecards
        if isinstance(sc.get("clip_start_offset_s"), (int, float))
    ]
    distinct_offsets = sorted(set(offsets))
    quality_eval_run_count = sum(
        1
        for sc in scorecards
        if isinstance(
            (sc.get("quality") or {}).get("translation_quality_rating"),
            (int, float),
        )
    )
    return {
        "distinct_clip_offsets_s": distinct_offsets,
        "distinct_clip_offset_count": len(distinct_offsets),
        "has_zero_offset_baseline": any(abs(offset) < 1e-9 for offset in distinct_offsets),
        "has_nonzero_offset_window": any(abs(offset) >= 1e-9 for offset in distinct_offsets),
        "quality_eval_run_count": quality_eval_run_count,
    }


# ── Per-set trajectory ─────────────────────────────────────────────────────────

def compute_trajectory(pipeline_dir: Path) -> dict:
    """
    Compute the trajectory for a single benchmark set.
    pipeline_dir should be the .../results/<audio_dir>/pipeline/ path.
    Returns the trajectory dict (also writes trajectory.json to pipeline_dir).
    """
    scorecards = _load_scorecards(pipeline_dir)

    run_ids = [sc["run_id"] for sc in scorecards]
    n_runs  = len(scorecards)

    series  = _extract_series(scorecards)

    # Detect clip-duration regime change before computing metric trends.
    # When the latest run used a significantly different clip duration, WER and
    # sentence-count metrics are not directly comparable to prior runs.
    clip_regime_changed = _clip_duration_regime_changed(series.get("clip_duration_s", []))

    metrics: dict[str, Any] = {}
    for key, values in series.items():
        current  = values[-1] if values else None
        previous = values[-2] if len(values) >= 2 else None

        trend = _trend_label(values, key)
        # If the clip duration changed substantially, override trend labels for
        # metrics that scale with clip length. Marking them "noisy" prevents the
        # review from treating measurement-regime artifacts as real regressions.
        if clip_regime_changed and key in _CLIP_SENSITIVE_METRICS:
            trend = "noisy"

        entry: dict[str, Any] = {
            "tier":           TIER.get(key),
            "current":        current,
            "delta_vs_prev":  _delta(current, previous),
            "trend":          trend,
            "short":          _window_stats(values, SHORT_WINDOW),
            "medium":         _window_stats(values, MEDIUM_WINDOW),
            "long":           _window_stats(values, LONG_WINDOW),
        }

        if len(values) >= MEDIUM_WINDOW:
            baseline_values = [v for v in values[:-SHORT_WINDOW] if v is not None]
            entry["delta_vs_baseline"] = (
                _delta(current, mean(baseline_values)) if baseline_values else None
            )
        else:
            entry["delta_vs_baseline"] = None

        metrics[key] = entry

    # Confidence tag for the overall set
    if n_runs < 3:
        confidence = "insufficient_data"
    elif n_runs < MEDIUM_WINDOW:
        confidence = "volatile"
    else:
        # Check if most metrics have low variance
        noisy_count = sum(
            1 for e in metrics.values() if e.get("trend") == "noisy"
        )
        confidence = "stable" if noisy_count <= 2 else "volatile"

    trajectory = {
        "audio_dir":          pipeline_dir.parent.name,
        "n_runs":             n_runs,
        "run_ids":            run_ids,
        "clip_regime_changed": clip_regime_changed,
        "coverage":           _coverage_summary(scorecards),
        "confidence":  confidence,
        "metrics":     metrics,
    }

    out_path = pipeline_dir / "trajectory.json"
    out_path.write_text(json.dumps(trajectory, indent=2, ensure_ascii=False))
    print(f"Trajectory: {out_path}  ({n_runs} runs, confidence={confidence})")
    return trajectory


# ── Aggregate trajectory ───────────────────────────────────────────────────────

def compute_aggregate(audio_dir_names: list[str]) -> dict:
    """
    Summarize trajectories across multiple benchmark sets.
    Writes to tests/benchmark/results/aggregate/trajectory.json.
    """
    results_root = ROOT / "tests" / "benchmark" / "results"
    per_set: dict[str, dict] = {}

    for name in audio_dir_names:
        pipeline_dir = results_root / name / "pipeline"
        if pipeline_dir.exists():
            traj = compute_trajectory(pipeline_dir)
            per_set[name] = {
                "n_runs":     traj["n_runs"],
                "confidence": traj["confidence"],
                # Surface only key Tier-2 metrics for the aggregate view
                "wer_committed_pct_trend": traj["metrics"].get(
                    "wer_committed_pct", {}
                ).get("trend"),
                "wer_committed_pct_current": traj["metrics"].get(
                    "wer_committed_pct", {}
                ).get("current"),
                "out_of_order_trend": traj["metrics"].get(
                    "out_of_order_event_count", {}
                ).get("trend"),
            }

    aggregate = {
        "sets":  audio_dir_names,
        "per_set": per_set,
    }

    agg_dir = results_root / "aggregate"
    agg_dir.mkdir(parents=True, exist_ok=True)
    out_path = agg_dir / "trajectory.json"
    out_path.write_text(json.dumps(aggregate, indent=2, ensure_ascii=False))
    print(f"Aggregate: {out_path}")
    return aggregate


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: trajectory.py <audio_dir_name> [audio_dir_name2 ...]")
        print("  audio_dir_name: name of the directory under tests/benchmark/results/")
        print("  Example: trajectory.py 1")
        print("  Example: trajectory.py 1 2")
        sys.exit(1)

    results_root = ROOT / "tests" / "benchmark" / "results"
    names = sys.argv[1:]

    if len(names) == 1:
        pipeline_dir = results_root / names[0] / "pipeline"
        traj = compute_trajectory(pipeline_dir)
        print(json.dumps(traj, indent=2, ensure_ascii=False))
    else:
        agg = compute_aggregate(names)
        print(json.dumps(agg, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
