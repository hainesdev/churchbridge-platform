# Review Instructions — Closed-Loop Evaluation System

**Status:** Main contains the original evaluation loop plus the staggered benchmark regime added on 2026-04-07.  
**For:** Any agent picking up benchmark or self-improvement work.

---

## What Is In Main

The repository now has two benchmark workflows:

1. **Legacy live benchmark lane**
   - Results root: `tests/benchmark/results/`
   - Typical use: single-run/manual evaluation
   - Shared report: `SELF_IMPROVEMENT_REPORT.md`

2. **Staggered benchmark lane**
   - Results root: `tests/benchmark/results/staggered/`
   - Typical use: short offset windows, parallel capture, sequential evaluation
   - Regime-local report: `tests/benchmark/results/staggered/SELF_IMPROVEMENT_REPORT.md`

### Evaluation modules (`tests/benchmark/`)

| File | Role |
|---|---|
| `scorecard.py` | Normalizes a pipeline run JSON into a canonical scorecard |
| `trajectory.py` | Rolling stats, trend labels, confidence per metric |
| `review.py` | Deterministic markdown review with one action recommendation |
| `llm_interpret.py` | Interprets artifacts after deterministic review |
| `cycle_log.py` | Append-only cycle memory, now parameterized per results root |
| `orchestrator.py` | Runs scorecard -> trajectory -> review -> LLM -> cycle log -> report |
| `storage.py` | Shared helpers for results roots, pipeline directories, report/cycle-log paths |
| `evaluate_captured_runs.py` | Sequential evaluator for previously captured staggered runs |
| `run_pipeline_test.py` | Live benchmark runner with `--start-offset`, `--port`, `--church-id`, `--results-root`, and `--capture-only` |

---

## Current State

### Legacy lane (`tests/benchmark/results/`)

| Set | Status |
|---|---|
| `tests/audio/1` | 3 runs recorded; latest review action is `investigate` |
| `tests/audio/2` | 3 runs recorded; latest review action is `promote` |

Important:
- The shared root-level `SELF_IMPROVEMENT_REPORT.md` reflects only the most recently evaluated legacy run.
- Do not assume that file summarizes both audio sets at once.

### Staggered lane (`tests/benchmark/results/staggered/`)

| Set | Status |
|---|---|
| `tests/audio/1` | 2 staggered runs captured/evaluated at offsets `0s` and `30s`, each with `--duration 5` |
| `tests/audio/2` | 2 staggered runs captured/evaluated at offsets `0s` and `30s`, each with `--duration 5` |

The staggered lane is a fresh regime. Its histories were intentionally reset so
old 85-second and 30-second legacy runs do not pollute trend analysis.

Current staggered action:
- `collect_more_runs`

Why:
- Each staggered set still has only 2 comparable runs.
- Trend labels unlock at 3 runs per set.

---

## Design Principles — Do Not Break These

1. **Deterministic code decides, LLM interprets.**
   `review.py` chooses the action label first. `llm_interpret.py` cannot override it.

2. **Parallel capture must not mean parallel evaluation.**
   Staggered live runs may be captured in parallel only in `--capture-only` mode.
   Evaluation must run afterward, sequentially, via `evaluate_captured_runs.py`.

3. **Do not mix regimes.**
   Legacy runs and staggered runs are not directly comparable. Keep their
   histories, cycle logs, and reports separate.

4. **Directive changes still require 5+ runs of sustained evidence.**
   Do not lower the threshold for `propose_directive_update`.

5. **Cycle logs are append-only.**
   If a staggered regime needs a clean slate, reset the results root before
   running it. Do not edit prior cycle entries in place.

6. **All file I/O uses explicit UTF-8.**

---

## Important Fixes Already Landed

These are no longer open issues:

1. `out_of_order_event_count` false positives were fixed in `scorecard.py`.
   Ordering now checks committed `final_spanish` events only.

2. `avg_translation_latency_s` and `avg_llm_correction_latency_s` now resolve
   correctly from raw event logs.

3. `time_to_first_translation_s`, `time_to_first_committed_sentence_s`, and
   `time_to_first_llm_correction_s` were missing from `LOWER_IS_BETTER` in
   `trajectory.py`. Decreasing latency was incorrectly labeled `regressed`.
   Fixed in PR #2.

4. Clip-duration regime change detection added to `trajectory.py`. When
   `clip_duration_s` changes by >40% across runs, WER and sentence-count
   metrics are marked `noisy` to prevent measurement-regime artifacts from
   triggering false `investigate` actions. Fixed in PR #2.

Do not reopen those as active benchmark bugs unless new evidence appears.

---

## Standard Staggered Workflow

### 1. Reset the staggered regime if you want a fresh comparable batch

```powershell
if (Test-Path tests\benchmark\results\staggered) {
  Remove-Item tests\benchmark\results\staggered -Recurse -Force
}
New-Item -ItemType Directory -Path tests\benchmark\results\staggered | Out-Null
```

### 2. Capture short windows in parallel

Use:
- distinct `--port` values
- either distinct `--church-id` values or the generated default
- `--capture-only`
- explicit short durations such as `--duration 5`

Example:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'

server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py `
  --audio-dir tests/audio/1 `
  --duration 5 `
  --start-offset 60 `
  --port 8805 `
  --results-root tests/benchmark/results/staggered `
  --capture-only
```

### 3. Evaluate captured runs sequentially

```powershell
server\.venv\Scripts\python.exe tests\benchmark\evaluate_captured_runs.py `
  --results-root tests/benchmark/results/staggered
```

### 4. Only then review trajectory/review/report artifacts

Primary files:
- `tests/benchmark/results/staggered/<audio_dir>/pipeline/history.json`
- `tests/benchmark/results/staggered/<audio_dir>/pipeline/trajectory.json`
- `tests/benchmark/results/staggered/<audio_dir>/pipeline/reviews/<run_id>.md`
- `tests/benchmark/results/staggered/SELF_IMPROVEMENT_REPORT.md`

---

## Recommended Next Step

Run one more staggered offset for each audio set so both sets reach 3 runs.

Suggested next offsets:
- `tests/audio/1` at `60s`
- `tests/audio/2` at `60s`

Keep them at `--duration 5` unless you intentionally want a different regime.

---

## Reference Documents

- `AUTONOMOUS_EVALUATION_PLAN.md`
- `SELF_IMPROVEMENT_DIRECTIVE.md`
- `DIRECTIVE.md`
- `TESTING_AND_BENCHMARKS.md`
