# Testing and Benchmarks

This document is the quick runbook for validating the server test suite and the live pipeline benchmark from the repository root.

## Working Directory

Run all commands from:

```powershell
C:\Users\Dan\Desktop\Projects\churchbridge-ai
```

## Python Environment

Use the existing server virtual environment when available:

```powershell
server\.venv\Scripts\python.exe
```

If the environment does not exist yet:

```powershell
python -m venv server\.venv
server\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
server\.venv\Scripts\python.exe -m pip install pytest
```

## Server Tests

Focused regression suite:

```powershell
server\.venv\Scripts\python.exe -m pytest tests\server\test_pipeline_regressions.py -q
```

Full server suite:

```powershell
server\.venv\Scripts\python.exe -m pytest tests\server -q
```

Latest verified result in this workspace:

- `tests/server/test_pipeline_regressions.py`: `7 passed`
- `tests/server/test_precision_phase.py`: `39 passed`
- `tests/server`: `100 passed`

Benchmark/evaluation test suites:

```powershell
server\.venv\Scripts\python.exe -m pytest tests\benchmark -q
```

Latest verified benchmark/evaluation result in this workspace:

- `tests/benchmark`: `29 passed`

## Playwright Web App Tests

Run these from:

```powershell
C:\Users\Dan\Desktop\Projects\churchbridge-ai\client
```

These browser tests exercise the real Next.js client against the live backend
WebSocket pipeline. Unlike the Python replay benchmark, they validate browser
audio capture shims, stream socket reconnects, display/listener subscriptions,
and the actual UI pages.

### Prerequisites

- The backend server must already be running and reachable at `http://127.0.0.1:8000`
  unless you override `CHURCHBRIDGE_API_URL` / `CHURCHBRIDGE_WS_URL`
- `server\.venv` is set up and the root `.env` contains the required runtime keys
- Playwright browsers are installed for the client workspace

Start the backend from the repo root in a separate shell:

```powershell
server\.venv\Scripts\python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

Install client dependencies and Playwright browsers when needed:

```powershell
npm install
npx playwright install chromium
```

### Run All Playwright Tests

```powershell
npm run test:e2e
```

### Focused Playwright Runs

```powershell
npm run test:e2e:web-client-replay
npm run test:e2e:mobile-listener
```

Current Playwright coverage in this workspace:

- `client/e2e/web-client-replay.spec.ts`
  - 60-second browser replay through `/test/[churchId]`
  - forced stream-socket reconnect during active audio
  - forced display-socket reconnect during active audio
- `client/e2e/mobile-listener.spec.ts`
  - `/listen/[churchId]` receives `live_translation` and `feed_commit` events

## Pipeline Benchmark

This benchmark exercises the live server pipeline end to end after audio has
already been captured and prepared:

- Starts `uvicorn` on a caller-selected port
- Streams clipped sermon audio through the WebSocket pipeline
- Captures display events
- Computes WER against the paired SRT
- Writes a full run JSON and, unless `--capture-only` is used, updates history/evaluation artifacts

It is a replay benchmark, not a microphone-capture benchmark. It does not test
iPhone/browser acquisition, echo cancellation, AGC, device routing, or other
front-end audio-processing behavior.

## Benchmark Capture Retention

The live `/api/stream/v1` server path now accepts an optional `benchmarkCapture`
object inside `session.start`. When present, the backend can:

- honor an explicit per-session capture enable or disable request
- name saved WAV and JSONL artifacts with benchmark session and run labels
- persist a metadata sidecar path plus benchmark identifiers in `session_captures`

Expected fields inside `benchmarkCapture`:

- `enabled`
- `sessionId`
- `runId`
- `scenarioId`
- `pipelineId`
- `captureLabel`

Named benchmark artifacts are written under:

- `tests/audio/captured/benchmarks/<sessionId>/`
- `logs/sessions/benchmarks/<sessionId>/`

### Prerequisites

- `.env` present at the repo root with the required runtime keys
- `pydub` installed
- `ffmpeg` available on `PATH`

If `ffmpeg` is not globally installed on `PATH`, inject the known local folder first:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
```

Set UTF-8 output to avoid console encoding issues:

```powershell
$env:PYTHONIOENCODING='utf-8'
```

### Smoke Run

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --duration 5
```

### Standard Live Run

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py
```

This now uses the default 30-second clip so routine live evaluation stays fast.

### New Staggered Regime

Use a fresh results root so staggered offset runs do not mix with the legacy
history:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py `
  --audio-dir tests/audio/1 `
  --start-offset 30 `
  --port 8801 `
  --results-root tests/benchmark/results/staggered `
  --capture-only
```

Capture staggered runs in parallel with:

- distinct `--port` values
- distinct `--church-id` values or the default generated namespace
- explicit short durations such as `--duration 5`
- `--capture-only`

After the captures finish, evaluate them sequentially:

```powershell
server\.venv\Scripts\python.exe tests\benchmark\evaluate_captured_runs.py `
  --results-root tests/benchmark/results/staggered
```

For the exact clean-rerun procedure and next-loop workflow, see
`SELF_IMPROVEMENT_LOOP_RUNBOOK.md`.

### Explicit Long Run

Use a longer clip only when you intentionally want deeper benchmark coverage:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --duration 85 --allow-long-duration
```

### Benchmark Output

For `--audio-dir tests/audio/1`, results are written to:

- `tests/benchmark/results/1/pipeline/history.json`
- `tests/benchmark/results/1/pipeline/<run_id>.json`

For the staggered regime, capture artifacts are written under:

- `tests/benchmark/results/staggered/<audio_dir>/pipeline/`
- `tests/benchmark/results/staggered/cycle_log.json` after sequential evaluation
- `tests/benchmark/results/staggered/SELF_IMPROVEMENT_REPORT.md` after sequential evaluation

Current workspace note:

- Legacy lane:
  - `tests/audio/1` has 3 runs
  - `tests/audio/2` has 3 runs
- Staggered lane:
  - this workspace may contain capture-only run JSONs without a regime-local report if `evaluate_captured_runs.py` has not been run yet
  - verify the presence of `trajectory.json`, `cycle_log.json`, and `SELF_IMPROVEMENT_REPORT.md` before treating the staggered lane as evaluated

## Troubleshooting

### Port Already In Use

The benchmark starts its own server on the requested `--port`. If a previous run was interrupted, clean up orphaned benchmark and `uvicorn` processes before retrying:

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -like 'python*' -and (
      $_.CommandLine -like '*tests\\benchmark\\run_pipeline_test.py*' -or
      $_.CommandLine -like '*-m uvicorn server.main:app*88*'
    )
  } |
  Select-Object ProcessId, CommandLine
```

To stop only those stale benchmark-related processes:

```powershell
$targets = Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -like 'python*' -and (
      $_.CommandLine -like '*tests\\benchmark\\run_pipeline_test.py*' -or
      $_.CommandLine -like '*-m uvicorn server.main:app*88*'
    )
  } |
  Select-Object -ExpandProperty ProcessId

if ($targets) {
  Stop-Process -Id $targets -Force
}
```

Confirm the port is clear:

```powershell
netstat -ano | Select-String ':8801'
```

No `LISTENING` entry on the port you plan to use means the benchmark can start fresh. `TIME_WAIT` entries after shutdown are normal.

### FFmpeg Not Found

If `AudioSegment.from_mp3(...)` fails, verify the local FFmpeg folder exists:

```powershell
Get-ChildItem 'C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg'
```

Then prepend that folder to `PATH` before running the benchmark.

### Notes

- The pytest suites under `tests/server` do not require Redis or the live benchmark server.
- The pipeline benchmark is not part of pytest and takes about the clip duration plus pipeline drain time.
- Routine live runs are capped at 30 seconds unless `--allow-long-duration` is passed.
- Parallel staggered capture is safe only in `--capture-only` mode with distinct `--port` values, followed by sequential evaluation.
- The runner now includes audio/set context plus a unique suffix in `run_id`, so parallel staggered captures do not collide across sets.
- Session-close flushes now drain queued translation/enrichment work before shutdown, so short capture windows can still emit final translation updates.
- Use explicit short durations for staggered coverage windows so the harness does not benchmark a full sermon by mistake.
- `promote`-style conclusions are intentionally gated until staggered coverage includes comparable baseline and offset windows across at least two benchmark sets.
- Interrupting the benchmark can leave background Python processes behind; clean them up before retrying.

## Provider Comparison Benchmark

Phase 1 uses an STT comparison baseline before moving on to translation,
interpretation, and subjective analysis.

This benchmark compares provider performance directly across three controlled
conditions:

- `raw`
- `echo`
- `noise`

It reuses the clipped sermon benchmark audio, applies deterministic degradations,
then calls three providers directly:

- Deepgram `nova-3`
- Google Chirp 3
- OpenAI `gpt-realtime-translate`

Phase 1 framing:

- Deepgram and Chirp 3 are the primary STT baselines
- `gpt-realtime-translate` is included as a translation-model-under-STT-pressure probe
- all three are scored against the Spanish SRT reference in the selected clip window

Run it from the repo root:

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\run_provider_model_benchmark.py
```

Useful overrides:

```powershell
server\.venv\Scripts\python.exe tests\benchmark\run_provider_model_benchmark.py `
  --audio-dir tests/audio/2 `
  --duration 20 `
  --condition raw `
  --condition echo `
  --condition noise `
  --echo-profile medium `
  --noise-type hvac `
  --snr-db 10
```

Artifacts are written under:

- `tests/benchmark/results/provider-comparison/<audio_dir>/<run_id>/provider-benchmark.json`
