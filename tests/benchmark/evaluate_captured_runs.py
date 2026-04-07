#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from tests.benchmark.orchestrator import run_evaluation_cycle
from tests.benchmark.storage import (
    STAGGERED_RESULTS_ROOT,
    append_history_row,
    load_json_with_fallback,
    pipeline_dir_for,
    regime_cycle_log_path,
    regime_report_path,
    resolve_results_root,
)


def _candidate_run_files(pipeline_dir: Path) -> list[Path]:
    return sorted(
        path for path in pipeline_dir.glob("*.json")
        if path.name not in {"history.json", "trajectory.json"}
    )


def evaluate_pipeline_dir(pipeline_dir: Path, use_llm: bool, force: bool, results_root: Path) -> int:
    scorecards_dir = pipeline_dir / "scorecards"
    scorecards_dir.mkdir(parents=True, exist_ok=True)
    processed = 0

    for run_path in _candidate_run_files(pipeline_dir):
        scorecard_path = scorecards_dir / run_path.name
        if scorecard_path.exists() and not force:
            continue

        result = load_json_with_fallback(run_path)
        append_history_row(result, pipeline_dir)
        run_evaluation_cycle(
            result,
            pipeline_dir,
            use_llm=use_llm,
            cycle_log_path=regime_cycle_log_path(results_root),
            report_path=regime_report_path(results_root),
        )
        processed += 1

    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate captured staggered benchmark runs sequentially")
    parser.add_argument("--results-root", default=str(STAGGERED_RESULTS_ROOT),
                        help="Results root containing captured staggered runs")
    parser.add_argument("--audio-dir", action="append",
                        help="Audio set name to evaluate (repeatable). Defaults to all sets under results root")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM interpretation during evaluation")
    parser.add_argument("--force", action="store_true",
                        help="Re-evaluate runs even if a scorecard already exists")
    args = parser.parse_args()

    results_root = resolve_results_root(args.results_root)
    if args.audio_dir:
        pipeline_dirs = [pipeline_dir_for(Path(item).name, results_root) for item in args.audio_dir]
    else:
        pipeline_dirs = sorted(
            path / "pipeline"
            for path in results_root.iterdir()
            if path.is_dir() and (path / "pipeline").exists()
        )

    total = 0
    for pipeline_dir in pipeline_dirs:
        if not pipeline_dir.exists():
            print(f"Skipping missing pipeline dir: {pipeline_dir}")
            continue
        count = evaluate_pipeline_dir(
            pipeline_dir,
            use_llm=not args.no_llm,
            force=args.force,
            results_root=results_root,
        )
        print(f"Evaluated {count} run(s) in {pipeline_dir}")
        total += count

    print(f"Done. Evaluated {total} run(s).")


if __name__ == "__main__":
    main()
