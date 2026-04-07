from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parent.parent.parent
LEGACY_RESULTS_ROOT = ROOT / "tests" / "benchmark" / "results"
STAGGERED_RESULTS_ROOT = LEGACY_RESULTS_ROOT / "staggered"


def resolve_results_root(value: str | Path | None) -> Path:
    if value is None:
        return LEGACY_RESULTS_ROOT
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    return path


def pipeline_dir_for(audio_dir_name: str, results_root: Path) -> Path:
    return results_root / audio_dir_name / "pipeline"


def regime_cycle_log_path(results_root: Path) -> Path:
    return results_root / "cycle_log.json"


def regime_report_path(results_root: Path) -> Path:
    return results_root / "SELF_IMPROVEMENT_REPORT.md"


def load_json_with_fallback(path: Path) -> dict:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"Could not decode JSON from {path}")


def save_run_result(result: dict, pipeline_dir: Path) -> Path:
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    run_file = pipeline_dir / f"{result['run_id']}.json"
    run_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return run_file


def history_entry(result: dict) -> dict:
    wer_raw = result["layers"]["raw_deepgram"]["wer"]
    wer_committed = result["layers"]["committed_sentences"]["wer"]
    return {
        "run_id": result["run_id"],
        "git_commit": result["git_commit"],
        "note": result["note"],
        "clip_start_offset_s": result.get("clip_start_offset_s", 0.0),
        "clip_duration_s": result["clip_duration_s"],
        "wer_raw_pct": wer_raw["score_pct"] if wer_raw else None,
        "wer_committed_pct": wer_committed["score_pct"] if wer_committed else None,
        "sentence_count": result["layers"]["committed_sentences"]["sentence_count"],
        "translation_count": len(result["layers"]["translations"]),
        "llm_correction_count": len(result["layers"]["llm_corrections"]),
        "verse_event_count": len(result["layers"]["verse_events"]),
        "wall_time_s": result["wall_time_s"],
    }


def append_history_row(result: dict, pipeline_dir: Path) -> Path:
    history_file = pipeline_dir / "history.json"
    history = json.loads(history_file.read_text(encoding="utf-8")) if history_file.exists() else []
    entry = history_entry(result)
    if not any(item.get("run_id") == entry["run_id"] for item in history):
        history.append(entry)
        history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return history_file
