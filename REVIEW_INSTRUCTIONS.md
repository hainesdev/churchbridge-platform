# Review Instructions — Closed-Loop Evaluation System

**Status:** Merged to `main` at commit `2bc0f44` (2026-04-07)  
**PR:** hainesdev/churchbridge-ai#1 (closed)  
**For:** Any agent picking up this work — read this before touching the evaluation loop.

---

## What Is in Main

A closed-loop benchmark evaluation system on top of the existing pipeline
benchmark runner. Six Python modules, two planning documents, one generated
handoff document, and a second benchmark audio set.

### Evaluation modules (`tests/benchmark/`)

| File | Role |
|---|---|
| `scorecard.py` | Normalises a pipeline run JSON into a canonical scorecard: accuracy, latency, behavioral integrity |
| `trajectory.py` | Rolling stats (3/5/10-run windows), trend labels, confidence per metric |
| `review.py` | Deterministic 4-section markdown review — exactly one action recommendation via tier-priority rules |
| `llm_interpret.py` | Claude Opus 4.6 pattern analysis — interprets artifacts, proposes one targeted fix per cycle |
| `cycle_log.py` | Append-only `results/cycle_log.json` — the system's memory across all improvement cycles |
| `orchestrator.py` | Wires all six stages, writes `SELF_IMPROVEMENT_REPORT.md` as the agent handoff document |

### Planning documents (repo root)

| File | Role |
|---|---|
| `AUTONOMOUS_EVALUATION_PLAN.md` | Design specification for the multi-cycle evaluation loop — the authoritative intent document |
| `SELF_IMPROVEMENT_DIRECTIVE.md` | Single-cycle improvement workflow that the evaluation loop extends |
| `SELF_IMPROVEMENT_REPORT.md` | Auto-generated handoff doc — regenerated after every benchmark run |

### Modified file

`tests/benchmark/run_pipeline_test.py` — calls `orchestrator.run_evaluation_cycle()`
after each benchmark run. `--no-llm` flag added. File writes fixed to UTF-8.

### Artifact layout (runtime, gitignored)

```
tests/benchmark/results/
  cycle_log.json                          <- append-only cycle memory
  <audio_dir_name>/pipeline/
    <run_id>.json                         <- full run
    history.json                          <- summary rows
    scorecards/<run_id>.json              <- canonical scorecard
    trajectory.json                       <- rolling stats
    reviews/<run_id>.md                   <- markdown review
SELF_IMPROVEMENT_REPORT.md               <- agent handoff (repo root, committed)
```

---

## Current System State

| Item | Value |
|---|---|
| Benchmark set 1 (`tests/audio/1`) | 2 runs completed |
| Benchmark set 2 (`tests/audio/2`) | 0 runs — audio file present, never benchmarked |
| Current action | `collect_more_runs` — need 1 more run on set 1 to unlock trend labels |
| `out_of_order_event_count` | 23–25 per run — Tier-1 metric, flagged as known issue below |
| `avg_translation_latency_s` | `None` both runs — known issue below |
| LLM interpreter | Not yet exercised against real data (skipped while action = `collect_more_runs`) |

**The system unlocks progressively:**
- 3 runs → trend labels resolve from `insufficient_data`
- 5 runs → medium-window comparisons valid
- 5+ runs with sustained Tier-2 regression → `propose_directive_update` eligible

---

## Design Principles — Do Not Break These

These rules are load-bearing. Any future change to the evaluation loop must
preserve them.

1. **Deterministic code decides, LLM interprets.** `review.py` sets the action
   label via tier-priority rules. `llm_interpret.py` runs after and cannot
   change it. Order in `orchestrator.run_evaluation_cycle()`:
   scorecard → trajectory → review → LLM → cycle log → report.

2. **LLM does not call the API when action is `collect_more_runs`.** The
   early-return block in `llm_interpret.interpret_run()` returns a canned stub
   without making a network call.

3. **Directive changes require 5+ runs of sustained evidence.** The
   `propose_directive_update` action fires only when a Tier-2 metric regresses
   across the long window (`n >= 5` in `trajectory.py`). Do not lower this threshold.

4. **`cycle_log.json` is append-only.** `cycle_log.record_cycle()` always
   appends. It never overwrites or mutates prior entries.

5. **All file I/O uses explicit `encoding="utf-8"`.** Every `write_text` and
   `read_text` in all six modules specifies encoding. Reads use a fallback
   (utf-8 → latin-1) to handle the two legacy run JSONs that predate this work.

6. **`SELF_IMPROVEMENT_REPORT.md` is overwritten on every run, not appended.**

---

## Known Issues (open — not yet fixed)

### 1. `out_of_order_event_count` is inflated

Current value: 23–25 per run. The metric looks alarming but is misleading.

`_check_ts_ordering` in `scorecard.py` counts any decrease in the `ts` field
across all messages sorted by `_elapsed_s`. The problem: LLM correction events
(`translation_update`) share the same `ts` as their paired translation (they
reference the same sentence) but arrive later. When sorted by `_elapsed_s`,
their identical `ts` causes the sequence to appear to jump backward.

**Fix needed:** Restrict ordering checks to `final_spanish` events only, or
compare `_elapsed_s` instead of `ts`. This is a scorecard-level change in
`scorecard.py:_check_ts_ordering`.

### 2. `avg_translation_latency_s` and `avg_llm_correction_latency_s` are always `None`

The pairing functions `_translation_latencies` and `_correction_latencies` in
`scorecard.py` match committed sentences to translations by `ts`. The matching
works, but the `committed_msgs` list (sourced from `all_messages`) does not
carry the `_elapsed_s` field because `final_spanish` events in `all_messages`
store it as `_elapsed_s` while the pairing expects it on the `committed_msgs`
entry directly. Re-check against the actual event log schema before fixing.

---

## Verification Steps

Run from the project root using `server/.venv/Scripts/python.exe`.

### Smoke-test orchestrator (no API call)

```powershell
server\.venv\Scripts\python.exe tests\benchmark\orchestrator.py `
  tests\benchmark\results\1\pipeline\2026-04-07T15-30-27Z.json --no-llm
```

Expected: prints cycle header, scorecard/trajectory/review paths, "LLM interpreter
skipped", cycle log count increments, `Action: COLLECT_MORE_RUNS`, exit 0.

### Verify action decision logic

```powershell
server\.venv\Scripts\python.exe -c "
import json, sys
sys.path.insert(0, '.')
from tests.benchmark.scorecard import scorecard_from_file
from tests.benchmark.trajectory import compute_trajectory
from tests.benchmark.review import _action_recommendation, _flat
from pathlib import Path
sc = scorecard_from_file(Path('tests/benchmark/results/1/pipeline/2026-04-07T15-30-27Z.json'))
traj = json.loads(Path('tests/benchmark/results/1/pipeline/trajectory.json').read_text(encoding='utf-8'))
action, reasons = _action_recommendation(sc, traj, _flat(sc))
print('Action:', action)
# Expected: collect_more_runs
"
```

### Verify LLM short-circuit (no API call)

```powershell
server\.venv\Scripts\python.exe -c "
import sys; sys.path.insert(0, '.')
from tests.benchmark.llm_interpret import interpret_run
r = interpret_run({}, {'n_runs': 2}, {}, '', [], 'collect_more_runs')
print(r['confidence'], r['proposed_change']['type'])
# Expected: low none
"
```

### Run server test suite

```powershell
server\.venv\Scripts\python.exe -m pytest tests\server -q
```

---

## Next Steps

In priority order:

1. **Run benchmark on `tests/audio/2`** — the audio is present but never benchmarked.
   This gives the system a second independent trajectory and brings set 1 closer
   to the 3-run minimum for trend labels.

   ```powershell
   server\.venv\Scripts\python.exe tests\benchmark\run_pipeline_test.py --audio-dir tests/audio/2
   ```

2. **Run benchmark on `tests/audio/1` again** — one more run brings set 1 to 3,
   which unlocks `improved`/`regressed`/`flat` labels and the first real LLM
   interpretation cycle.

3. **Fix `out_of_order_event_count`** — see Known Issues #1. Fix in
   `scorecard.py:_check_ts_ordering` before the metric misleads the LLM interpreter.

4. **Investigate `avg_translation_latency_s`** — see Known Issues #2. Confirm
   the pairing logic against the actual event log schema for a recent run.

5. **Expand `DIRECTIVE.md` benchmark history table** — the table at the bottom of
   `DIRECTIVE.md` is still empty. Populate it with the two existing run results.

---

## Reference Documents

- `AUTONOMOUS_EVALUATION_PLAN.md` — authoritative design spec for this system
- `SELF_IMPROVEMENT_DIRECTIVE.md` — single-cycle improvement workflow
- `DIRECTIVE.md` — production pipeline architecture
- `TESTING_AND_BENCHMARKS.md` — approved test commands and verified suite counts
