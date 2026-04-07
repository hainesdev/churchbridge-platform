# Self-Improvement Loop Runbook

This is the quickest way for another agent to reproduce the same benchmark
procedure used in this workspace and then start the next self-improvement loop.

Read this together with:

- [DIRECTIVE.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\DIRECTIVE.md)
- [SELF_IMPROVEMENT_DIRECTIVE.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\SELF_IMPROVEMENT_DIRECTIVE.md)
- [TESTING_AND_BENCHMARKS.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\TESTING_AND_BENCHMARKS.md)
- [REVIEW_INSTRUCTIONS.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\REVIEW_INSTRUCTIONS.md)

## Goal

One full loop should do all of this:

1. start from a clean staggered benchmark regime
2. collect a fresh comparable batch of short runs
3. evaluate those runs sequentially
4. inspect the regenerated report and trajectories
5. choose one confirmed issue
6. run focused tests before edits
7. implement the smallest safe fix
8. rerun focused tests and the full server suite
9. optionally rerun a live verification benchmark if the change affects pipeline shutdown, timing, ordering, or emission behavior

## Working Directory

Run everything from:

```powershell
C:\Users\Dan\Desktop\Projects\churchbridge-ai
```

## Preconditions

Use the existing server virtual environment:

```powershell
server\.venv\Scripts\python.exe
```

Benchmark prerequisites:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
```

The repo root `.env` must contain:

- `DEEPGRAM_API_KEY`
- `GOOGLE_TRANSLATE_API_KEY`
- `ANTHROPIC_API_KEY`

## Part 1: Reset The Staggered Regime

Use this when you want a pristine rerun-only staggered dataset:

```powershell
if (Test-Path tests\benchmark\results\staggered) {
  Remove-Item -LiteralPath tests\benchmark\results\staggered -Recurse -Force
}
New-Item -ItemType Directory -Path tests\benchmark\results\staggered | Out-Null
```

This deletes prior staggered raw runs, scorecards, reviews, trajectories, cycle
log, and report. It does not touch the legacy lane under `tests/benchmark/results/1`
or `tests/benchmark/results/2`.

## Part 2: Capture The Same Fresh 6-Run Staggered Batch

These are the exact short-window captures used for the current clean regime:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'

server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/1 --duration 5 --start-offset 0  --port 8801 --church-id staggered-1-0  --results-root tests/benchmark/results/staggered --capture-only
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/1 --duration 5 --start-offset 30 --port 8802 --church-id staggered-1-30 --results-root tests/benchmark/results/staggered --capture-only
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/1 --duration 5 --start-offset 60 --port 8803 --church-id staggered-1-60 --results-root tests/benchmark/results/staggered --capture-only

server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/2 --duration 5 --start-offset 0  --port 8804 --church-id staggered-2-0  --results-root tests/benchmark/results/staggered --capture-only
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/2 --duration 5 --start-offset 30 --port 8805 --church-id staggered-2-30 --results-root tests/benchmark/results/staggered --capture-only
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/2 --duration 5 --start-offset 60 --port 8806 --church-id staggered-2-60 --results-root tests/benchmark/results/staggered --capture-only
```

Notes:

- Run them in parallel when convenient.
- Keep ports distinct.
- Keep `--capture-only` on for parallel capture.
- The runner now generates unique `run_id`s even when separate processes start at nearly the same moment.

**Important — Claude Code bash environment:** When running captures from Claude Code's
bash tool (not a native PowerShell terminal), Python's subprocess cannot see PATH
overrides set with bash's inline `PATH=` prefix. Wrap each capture command with
`powershell -NoProfile -Command` so ffmpeg is visible to pydub:

```bash
powershell -NoProfile -Command "\$env:PATH='C:\\Users\\Dan\\Desktop\\Projects\\Transcribe Video\\ffmpeg;' + \$env:PATH; \$env:PYTHONIOENCODING='utf-8'; server\\.venv\\Scripts\\python.exe tests\\benchmark\\run_pipeline_test.py --audio-dir tests/audio/1 --duration 5 --start-offset 0 --port 8801 --church-id staggered-1-0 --results-root tests/benchmark/results/staggered --capture-only"
```

Repeat for each of the 6 runs with the appropriate `--start-offset`, `--port`, and `--church-id` values. Background them with the Bash tool's `run_in_background` parameter to run all 6 in parallel.

## Part 3: Evaluate The Captured Runs Sequentially

```powershell
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\evaluate_captured_runs.py --results-root tests/benchmark/results/staggered --no-llm
```

This regenerates:

- `tests/benchmark/results/staggered/<audio_dir>/pipeline/history.json`
- `tests/benchmark/results/staggered/<audio_dir>/pipeline/scorecards/<run_id>.json`
- `tests/benchmark/results/staggered/<audio_dir>/pipeline/reviews/<run_id>.md`
- `tests/benchmark/results/staggered/<audio_dir>/pipeline/trajectory.json`
- `tests/benchmark/results/staggered/cycle_log.json`
- `tests/benchmark/results/staggered/SELF_IMPROVEMENT_REPORT.md`

The staggered report now summarizes all evaluated audio sets in the regime, not
just the last set processed.

## Part 4: Read The Fresh Artifacts

Start here:

- [tests/benchmark/results/staggered/SELF_IMPROVEMENT_REPORT.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\tests\benchmark\results\staggered\SELF_IMPROVEMENT_REPORT.md)
- [tests/benchmark/results/staggered/1/pipeline/trajectory.json](C:\Users\Dan\Desktop\Projects\churchbridge-ai\tests\benchmark\results\staggered\1\pipeline\trajectory.json)
- [tests/benchmark/results/staggered/2/pipeline/trajectory.json](C:\Users\Dan\Desktop\Projects\churchbridge-ai\tests\benchmark\results\staggered\2\pipeline\trajectory.json)
- [tests/benchmark/results/staggered/cycle_log.json](C:\Users\Dan\Desktop\Projects\churchbridge-ai\tests\benchmark\results\staggered\cycle_log.json)

When selecting the next issue, prefer:

- user-visible correctness problems
- ordering/emission integrity problems
- shutdown and lifecycle problems
- metric bugs that mislead the review loop

Avoid changing code only to make metrics look nicer if the pipeline behavior is
not actually wrong.

## Part 5: Baseline Tests Before Editing

Use the narrowest relevant suite first.

Common benchmark/evaluation suites:

```powershell
server\.venv\Scripts\python.exe -m pytest tests\benchmark\test_scorecard.py -q
server\.venv\Scripts\python.exe -m pytest tests\benchmark\test_run_pipeline_test.py -q
server\.venv\Scripts\python.exe -m pytest tests\benchmark\test_orchestrator.py -q
server\.venv\Scripts\python.exe -m pytest tests\benchmark -q
```

Common server-side regression suites:

```powershell
server\.venv\Scripts\python.exe -m pytest tests\server\test_pipeline_regressions.py -q
server\.venv\Scripts\python.exe -m pytest tests\server\test_precision_phase.py -q
server\.venv\Scripts\python.exe -m pytest tests\server\test_sentence_buffer.py -q
server\.venv\Scripts\python.exe -m pytest tests\server -q
```

## Part 6: Implement The Smallest Confirmed Fix

Before editing:

- identify the exact control flow that produces the bad artifact or broken event behavior
- write or update one focused regression test
- prefer deterministic fixes over prompt-only fixes

Recent examples of valid benchmark-loop fixes in this repo:

- scorecard metric accuracy (`client_visible_rewrite_count`)
- unique run identifiers for parallel captures
- staggered regime report aggregation across sets
- graceful shutdown draining of translation/enrichment tasks after `session_close`
- `deferred_release_count`, `deferred_release_timeout_count`, `caption_merge_count` — were always 0 because scorecard used wrong field names; fixed to use `pending_completion` and `caption_merge` events
- trajectory `LOWER_IS_BETTER` missing latency metrics — decreasing latency was labelled `regressed`; fixed
- clip-duration regime change detection — prevents false regressions when benchmark window length changes

## Part 7: Test After Editing

Minimum post-edit pass:

```powershell
server\.venv\Scripts\python.exe -m pytest <focused suite> -q
server\.venv\Scripts\python.exe -m pytest tests\benchmark -q
server\.venv\Scripts\python.exe -m pytest tests\server -q
```

If the fix affects live pipeline shutdown, async emission, ordering, or delayed
updates, run one live verification clip.

Example verification for the formerly broken close-time path:

```powershell
if (Test-Path tests\benchmark\results\verification-close) {
  Remove-Item -LiteralPath tests\benchmark\results\verification-close -Recurse -Force
}

$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'

server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py `
  --audio-dir tests/audio/1 `
  --duration 5 `
  --start-offset 60 `
  --port 8810 `
  --church-id verify-close-1-60 `
  --results-root tests/benchmark/results/verification-close `
  --capture-only
```

Inspect the saved run JSON or console output and confirm the `session_close`
path still emits the expected `translation` and any deferred `translation_update`
instead of failing with a closed client error.

## Part 8: Document What Changed

Update docs when any of these change:

- verified test counts
- benchmark workflow steps
- guaranteed behavior
- results-root conventions

At minimum, keep [TESTING_AND_BENCHMARKS.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\TESTING_AND_BENCHMARKS.md)
current.

## Current Known Good Validation Snapshot

Last verified: 2026-04-07

- `tests/benchmark`: `29 passed`
- `tests/server/test_pipeline_regressions.py`: `6 passed`
- `tests/server/test_precision_phase.py`: `39 passed`
- `tests/server`: `99 passed`

## Recommended Start Order For The Next Agent

1. Read `DIRECTIVE.md`.
2. Read `SELF_IMPROVEMENT_DIRECTIVE.md`.
3. Read `TESTING_AND_BENCHMARKS.md`.
4. Read `REVIEW_INSTRUCTIONS.md`.
5. Read the fresh staggered `SELF_IMPROVEMENT_REPORT.md`.
6. Choose one confirmed issue.
7. Run focused tests before editing.
8. Fix, retest, and report exact commands/results.
