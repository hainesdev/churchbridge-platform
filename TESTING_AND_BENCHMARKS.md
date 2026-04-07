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
- `tests/server`: `95 passed`

## Pipeline Benchmark

This benchmark exercises the live server pipeline end to end:

- Starts `uvicorn` on port `8799`
- Streams clipped sermon audio through the WebSocket pipeline
- Captures display events
- Computes WER against the paired SRT
- Writes a full run JSON plus summary history

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

### Full 85-Second Run

```powershell
$env:PATH='C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg;' + $env:PATH
$env:PYTHONIOENCODING='utf-8'
server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --duration 85
```

### Benchmark Output

For `--audio-dir tests/audio/1`, results are written to:

- `tests/benchmark/results/1/pipeline/history.json`
- `tests/benchmark/results/1/pipeline/<run_id>.json`

Latest verified benchmark run:

- Run id: `2026-04-07T15-30-27Z`
- File: `tests/benchmark/results/1/pipeline/2026-04-07T15-30-27Z.json`
- Raw WER: `12.94%`
- Committed WER: `14.93%`
- Committed sentences: `15`
- Translations: `15`
- LLM corrections: `9`
- Verse events: `13`
- Wall time: `105.8s`

Prior baseline already on disk:

- Run id: `2026-04-07T13-41-01Z`
- File: `tests/benchmark/results/1/pipeline/2026-04-07T13-41-01Z.json`
- Raw WER: `12.44%`
- Committed WER: `14.43%`

## Troubleshooting

### Port 8799 Already In Use

The benchmark starts its own server on port `8799`. If a previous run was interrupted, clean up orphaned benchmark and `uvicorn` processes before retrying:

```powershell
Get-CimInstance Win32_Process |
  Where-Object {
    $_.Name -like 'python*' -and (
      $_.CommandLine -like '*tests\\benchmark\\run_pipeline_test.py*' -or
      $_.CommandLine -like '*-m uvicorn server.main:app*8799*'
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
      $_.CommandLine -like '*-m uvicorn server.main:app*8799*'
    )
  } |
  Select-Object -ExpandProperty ProcessId

if ($targets) {
  Stop-Process -Id $targets -Force
}
```

Confirm the port is clear:

```powershell
netstat -ano | Select-String ':8799'
```

No `LISTENING` entry means the benchmark can start fresh. `TIME_WAIT` entries after shutdown are normal.

### FFmpeg Not Found

If `AudioSegment.from_mp3(...)` fails, verify the local FFmpeg folder exists:

```powershell
Get-ChildItem 'C:\Users\Dan\Desktop\Projects\Transcribe Video\ffmpeg'
```

Then prepend that folder to `PATH` before running the benchmark.

### Notes

- The pytest suites under `tests/server` do not require Redis or the live benchmark server.
- The pipeline benchmark is not part of pytest and takes about the clip duration plus pipeline drain time.
- Interrupting the benchmark can leave background Python processes behind; clean them up before retrying.
