# Self-Improvement Directive For Autonomous Code Agents

This document defines how an autonomous AI agent should review, improve, test,
and publish changes in this repository without drifting from the intended
system behavior.

It is meant to be used alongside [DIRECTIVE.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\DIRECTIVE.md)
and [TESTING_AND_BENCHMARKS.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\TESTING_AND_BENCHMARKS.md).

## Mission

Continuously improve the codebase by:

- reviewing `DIRECTIVE.md` for intended architecture and guarantees
- performing a critical analysis of the implementation against that directive
- identifying real bugs, regressions, mismatches, and missing safeguards
- making the smallest correct code changes needed to fix confirmed issues
- testing before and after changes
- keeping GitHub up to date with a branch, commit, push, and PR

## Core Rules

1. Treat `DIRECTIVE.md` as the source of intended behavior, not as proof that the code already does that behavior.
2. Prefer verified findings over speculative ones.
3. Do not change code just to “match the doc” unless the current behavior is clearly wrong, risky, or inconsistent with project goals.
4. Reproduce or validate the relevant behavior before editing when feasible.
5. Run focused tests before changes to establish a baseline.
6. Run focused tests and the full server suite after changes.
7. Update documentation when verified counts, workflows, or guarantees materially change.
8. Keep GitHub current after successful changes.

## Standard Workflow

### 1. Read the governing docs

Always read:

- `DIRECTIVE.md`
- `TESTING_AND_BENCHMARKS.md`

Use `DIRECTIVE.md` to understand:

- mission-critical behavior
- latency and accuracy tradeoffs
- architectural promises
- component responsibilities
- known limitations

Use `TESTING_AND_BENCHMARKS.md` to understand:

- approved test commands
- expected Python environment
- benchmark prerequisites
- latest verified suite counts

### 2. Perform critical analysis

Compare the directive against the codebase and look for:

- documented behavior not implemented
- concurrency hazards
- reconnect and lifecycle gaps
- stale or partial client event propagation
- missing structural guards
- mismatches between comments/docs and executable logic
- missing regression coverage for risky paths

When doing the review, prioritize:

- correctness
- behavioral regressions
- hidden failure modes
- missing tests

### 3. Convert findings into an action plan

For each confirmed issue:

- identify the exact files and control flow involved
- determine the smallest safe fix
- determine which tests should fail before the fix or at least prove the baseline behavior
- determine which tests should pass after the fix

Avoid broad refactors unless the bug cannot be fixed safely otherwise.

### 4. Test before changes

At minimum, run the most relevant focused suite first.

Typical commands from repo root:

```powershell
server\.venv\Scripts\python.exe -m pytest tests\server\test_pipeline_regressions.py -q
server\.venv\Scripts\python.exe -m pytest tests\server\test_sentence_buffer.py -q
server\.venv\Scripts\python.exe -m pytest tests\server -q
```

Use the narrowest meaningful command first, then expand.

Record:

- which command was run
- whether it passed before changes
- the count/result

### 5. Implement the fix

While changing code:

- preserve existing architecture unless it is the source of the bug
- prefer deterministic safeguards over prompt-only behavior when correctness matters
- add targeted tests for the exact regression
- keep comments short and useful

### 6. Test after changes

After editing:

1. rerun the focused suite(s) related to the change
2. rerun `tests\server -q`
3. run the benchmark only when the change affects live pipeline behavior enough to justify it

For parallel benchmark collection, use the staggered regime:

- capture with `tests/benchmark/run_pipeline_test.py` in `--capture-only` mode
- use explicit short durations and start offsets
- assign distinct `--port` values per live run
- evaluate afterward with `tests/benchmark/evaluate_captured_runs.py`

If counts change and the testing runbook lists verified totals, update `TESTING_AND_BENCHMARKS.md`.

### 7. Update GitHub

After successful tests:

1. create a branch using the `codex/` prefix unless told otherwise
2. stage only the intended files
3. commit with a precise message
4. push the branch
5. open a draft PR unless the work is explicitly ready for final review

The PR should include:

- summary of what changed
- why it changed
- exact test commands run
- exact test results

## Review Heuristics

When reviewing code against `DIRECTIVE.md`, ask:

- Does the implementation actually enforce the described flush/order/merge rules?
- Are “best effort” behaviors incorrectly documented as guaranteed?
- Can reconnects or async completion reorder state and silently violate user-visible correctness?
- Do all clients receive the final corrected output, or only an earlier approximation?
- Is there regression coverage for the failure mode being fixed?

## Required Output Behavior

When acting autonomously, the agent should:

- state what it is reviewing
- say which tests it is running before edits
- explain the exact fix it is making before file edits
- report test results after edits
- report branch, commit, push, and PR status

## Corrections To Prior Workflow

These points are important and should not be skipped:

- The file name is `DIRECTIVE.md`, not `Directive.md`.
- Testing should happen both before and after changes, not only after.
- The agent should consult `TESTING_AND_BENCHMARKS.md` before choosing commands.
- “Keep GitHub up to date” should include branch creation, commit, push, and PR creation after tests pass.
- If a documented test count changes, the testing runbook should be updated.

## Success Criteria

A self-improvement cycle is complete only when:

- the relevant directive has been reviewed
- the code has been critically analyzed
- at least one confirmed issue is fixed or explicitly ruled out
- regression coverage exists for the changed behavior
- focused and full server tests pass
- GitHub has the updated branch and PR
