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

- `tests/server/test_pipeline_regressions.py`: `4 passed`
- `tests/server`: `97 passed`

## Pipeline Benchmark

This benchmark exercises the live server pipeline end to end:

- Starts `uvicorn` on a caller-selected port
- Streams clipped sermon audio through the WebSocket pipeline
- Captures display events
- Computes WER against the paired SRT
- Writes a full run JSON and, unless `--capture-only` is used, updates history/evaluation artifacts

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

For the staggered regime, results are written under:

- `tests/benchmark/results/staggered/<audio_dir>/pipeline/`
- `tests/benchmark/results/staggered/cycle_log.json`
- `tests/benchmark/results/staggered/SELF_IMPROVEMENT_REPORT.md`

Current verified state:

- Legacy lane:
  - `tests/audio/1` has 3 runs
  - `tests/audio/2` has 3 runs
- Staggered lane:
  - `tests/audio/1` has 2 evaluated 5-second runs at offsets `0s` and `30s`
  - `tests/audio/2` has 2 evaluated 5-second runs at offsets `0s` and `30s`

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
- Use explicit short durations for staggered coverage windows so the harness does not benchmark a full sermon by mistake.
- Interrupting the benchmark can leave background Python processes behind; clean them up before retrying.
