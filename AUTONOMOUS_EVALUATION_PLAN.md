# Autonomous Evaluation And Directive-Tuning Plan

This document extends the current self-improvement workflow into a multi-run
learning loop for the live pipeline benchmark.

It is a planning artifact, not yet an execution directive.

It builds on:

- [SELF_IMPROVEMENT_DIRECTIVE.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\SELF_IMPROVEMENT_DIRECTIVE.md)
- [DIRECTIVE.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\DIRECTIVE.md)
- [TESTING_AND_BENCHMARKS.md](C:\Users\Dan\Desktop\Projects\churchbridge-ai\TESTING_AND_BENCHMARKS.md)
- [tests/benchmark/run_pipeline_test.py](C:\Users\Dan\Desktop\Projects\churchbridge-ai\tests\benchmark\run_pipeline_test.py)

## Why This Exists

The current self-improvement loop is strong at single-cycle work:

- review the directive
- inspect the implementation
- confirm a bug or mismatch
- make a narrow fix
- test before and after
- publish the result

What it does not yet define is how an autonomous agent should:

- review live benchmark output after each run
- compare multiple runs over time
- separate real improvement from noise
- detect when a code change improves one metric by harming another
- decide when the directive itself should change

This document defines that next layer.

## Current State In This Repository

The pipeline benchmark already records useful artifacts.

From [tests/benchmark/run_pipeline_test.py](C:\Users\Dan\Desktop\Projects\churchbridge-ai\tests\benchmark\run_pipeline_test.py):

- each run writes a full JSON artifact to `tests/benchmark/results/<audio_dir>/pipeline/<run_id>.json`
- each run appends a summary row to `tests/benchmark/results/<audio_dir>/pipeline/history.json`
- the full run JSON already stores:
  - SRT reference text
  - raw Deepgram finals
  - committed Spanish sentences
  - translations
  - LLM corrections
  - verse events
  - segment metadata
  - mode changes
  - the ordered display event log

This means the repository already has the raw ingredients for a longitudinal
evaluator. The missing pieces are:

- a canonical scorecard schema
- a trajectory analyzer
- a decision policy
- a safe process for directive adjustment

## Target System

The autonomous learning loop should have six stages:

1. Run benchmark
2. Normalize results into a stable scorecard
3. Compare the scorecard against historical runs
4. Diagnose likely causes of change
5. Propose code changes and, separately, directive changes
6. Promote only evidence-backed improvements

The key design principle is separation of concerns:

- benchmark execution gathers facts
- evaluation interprets results
- self-improvement changes code
- directive tuning changes the standard only when repeated evidence justifies it

The agent must not silently redefine success because one run was noisy.

## Core Design Principles

### 1. Preserve directive authority

`DIRECTIVE.md` remains the source of intended behavior.

The evaluator may propose updates to the directive when repeated benchmark
evidence shows one of these conditions:

- the directive claims a guarantee the implementation cannot reliably uphold
- the directive omits a guardrail needed to prevent regressions
- the directive lacks measurable success criteria for a recurring failure mode
- the benchmark exposes a repeated tradeoff the directive should explicitly rank

The evaluator must not relax a guarantee just because the latest run regressed.

### 2. Optimize for trajectories, not isolated runs

One run is evidence.
Several runs with the same directional movement are a signal.

The evaluator should use rolling windows and classify results as:

- `improved`
- `regressed`
- `flat`
- `noisy`
- `inconclusive`

### 3. Guardrail metrics outrank convenience metrics

The system exists to provide trustworthy live translation.

A change must not be promoted just because it improves a secondary metric while
degrading user-visible correctness or theological fidelity.

### 4. Keep code changes and directive changes separate

A code fix can be merged without a directive change.
A directive change can be proposed without a code fix.

The system should never automatically rewrite both in the same reasoning step
without making the evidence explicit.

## Proposed Scorecard Schema

Each pipeline run should be normalized into a scorecard with three groups of
metrics.

### A. Accuracy

These describe whether the system said the right thing.

- `wer_raw_pct`
- `wer_committed_pct`
- `committed_sentence_count`
- `translation_count`
- `llm_correction_count`
- `verse_event_count`
- `scripture_reference_recall`
- `scripture_reference_precision`
- `theological_term_recall`
- `theological_term_precision`
- `merge_accuracy_rate`

The first six already exist or can be derived from the current run JSON.
The rest require additional evaluators over the event stream and reference text.

### B. Latency And Flow

These describe whether the system feels live and stable.

- `wall_time_s`
- `time_to_first_translation_s`
- `time_to_first_committed_sentence_s`
- `time_to_first_llm_correction_s`
- `avg_translation_latency_s`
- `avg_llm_correction_latency_s`
- `deferred_release_count`
- `deferred_release_timeout_count`
- `stale_correction_suppression_count`
- `caption_merge_count`

Some of these can already be inferred from `_elapsed_s` and event types in the
current output.

### C. Behavioral Integrity

These describe whether the pipeline obeys the architecture promises in
`DIRECTIVE.md`.

- `out_of_order_event_count`
- `orphan_correction_count`
- `fragment_leak_count`
- `incorrect_merge_suspect_count`
- `duplicate_commit_count`
- `mode_flip_count`
- `client_visible_rewrite_count`
- `display_ready_violation_count`

These metrics are especially important because they test guarantees that raw WER
cannot see.

## Scoring Policy

The evaluator should classify metrics into three priority levels.

### Tier 1: hard guardrails

- committed translation correctness
- scripture fidelity
- user-visible ordering integrity
- stale correction suppression
- incorrect merge avoidance

A regression in a Tier 1 metric blocks promotion unless the change is tiny and
repeated evidence shows a larger Tier 1 gain elsewhere.

### Tier 2: primary optimization targets

- `wer_committed_pct`
- theological term fidelity
- translation completeness
- latency to readable output

These are the main quality targets after guardrails hold.

### Tier 3: supportive diagnostics

- raw Deepgram WER
- count deltas
- wall-clock runtime
- LLM correction volume
- mode-change volume

These help explain changes but should not drive promotion by themselves.

## Historical Analysis Policy

The historian should work per benchmark set, not globally.

For example:

- `tests/audio/1` gets its own timeline
- future `tests/audio/2`, `tests/audio/3`, and so on each get their own timeline
- an aggregate report can summarize across all benchmark sets

For each benchmark set, the historian should compute:

- rolling mean over the last `N` runs
- rolling best and worst
- delta versus previous run
- delta versus rolling baseline
- commit-to-commit change points
- confidence tags such as `stable`, `volatile`, or `insufficient_data`

Recommended default windows:

- short window: last 3 runs
- medium window: last 5 runs
- long window: last 10 runs

The system should avoid overreacting until at least 3 comparable runs exist.

## Benchmark Coverage Policy

The current repository only has one benchmark audio set under
`tests/audio/1`.

That is a good starting point for regression detection, but it is too narrow for
autonomous optimization.

Before enabling automatic directive tuning, expand the benchmark set to include:

- scripture-heavy preaching
- fast emotional preaching
- noisy-room preaching
- quote-and-merge heavy preaching
- short pause / false-utterance-end edge cases

Without that expansion, the system may overfit to a single sermon clip.

## Diagnostic Review Loop

After each benchmark run, the evaluator agent should produce a structured review
with four sections:

### 1. Run summary

- run id
- commit
- benchmark set
- key metric values
- key deltas versus baseline

### 2. Primary findings

- what improved
- what regressed
- which changes appear statistically weak or noisy

### 3. Likely causes

Examples:

- STT changed but committed sentence quality did not
- committed WER worsened while raw WER stayed flat, suggesting buffering or cleaning regressions
- correction count rose but visible quality did not, suggesting churn without value
- verse events increased while precision fell, suggesting over-triggering

### 4. Action recommendation

Exactly one of:

- `promote`
- `investigate`
- `revert_or_fix`
- `collect_more_runs`
- `propose_directive_update`

## Directive Adjustment Policy

Directive changes should be proposed only when repeated evidence reveals a
problem in the directive itself.

Examples of valid directive changes:

- turning an implicit guardrail into an explicit one
- clarifying that a behavior is best-effort, not guaranteed
- adding a measurable benchmark target for a recurring failure mode
- ranking a tradeoff that the current directive leaves ambiguous

Examples of invalid directive changes:

- weakening a correctness goal after one poor run
- updating the directive to match a buggy implementation
- changing success criteria before enough comparative data exists

Each proposed directive change should include:

- the exact current wording
- the proposed wording
- the evidence from multiple runs
- the reason the current wording is insufficient
- the expected impact of the change

## Suggested File Outputs

To make the learning loop concrete, add these artifacts over time:

- `tests/benchmark/results/<audio_dir>/pipeline/history.json`
  already exists and should remain append-only
- `tests/benchmark/results/<audio_dir>/pipeline/scorecards/<run_id>.json`
  normalized per-run evaluation output
- `tests/benchmark/results/<audio_dir>/pipeline/reviews/<run_id>.md`
  human-readable benchmark review
- `tests/benchmark/results/aggregate/trajectory.json`
  cross-benchmark summary
- `SELF_IMPROVEMENT_REPORT.md`
  latest top-level repository summary for agent handoff

The normalized scorecard should be derived from the full run JSON, not replace
it.

## Suggested Agent Roles

The system can remain a single agent at first, but the logic is easier to keep
clean if we think in roles.

### Runner

- executes benchmark commands
- stores raw artifacts

### Normalizer

- converts full run JSON into a canonical scorecard
- computes derived metrics

### Historian

- updates trajectory summaries
- computes rolling deltas and trend labels

### Reviewer

- interprets the latest run in the context of history
- proposes code or directive actions

### Executor

- performs approved code changes under the self-improvement directive

## Minimal Implementation Sequence

The safest build order is:

1. Add a scorecard generator that reads existing pipeline result JSON files.
2. Add a trajectory analyzer that reads `history.json` plus scorecards.
3. Add a markdown review generator for each run.
4. Add explicit decision labels: `improved`, `regressed`, `noisy`, `inconclusive`.
5. Expand benchmark coverage beyond `tests/audio/1`.
6. Only then allow automated directive-adjustment proposals.

This sequence matters because the system should learn to measure before it
learns to rewrite its own instructions.

## First Practical Milestones

### Milestone 1: Canonical scorecard

Goal:
Turn each pipeline run into a stable machine-readable summary with enough detail
for multi-run comparison.

### Milestone 2: Trajectory report

Goal:
Compare the last several runs and output a plain-language assessment of whether
the system is actually improving.

### Milestone 3: Directive change proposal format

Goal:
Require evidence-backed directive diffs instead of ad hoc textual edits.

### Milestone 4: Multi-benchmark promotion rules

Goal:
Block overfitting to a single sermon clip before autonomous tuning expands.

## Open Questions

- Which theological terms should be scored explicitly rather than left inside WER?
- How should verse-detection precision and recall be labeled from the current benchmark artifacts?
- How many repeated runs should be required before a directive proposal is allowed?
- Should a directive proposal always require human review, even if code fixes remain autonomous?
- Which benchmark metrics are allowed to be noisy, and which are strict release gates?

## Recommendation

Treat the next phase as an evaluator project, not a fixer project.

The repository already has enough benchmark output to start building the
historian and reviewer layers. The first code should likely focus on:

- scorecard normalization
- trajectory computation
- run-review generation

Only after those are stable should the autonomous agent begin proposing edits to
`SELF_IMPROVEMENT_DIRECTIVE.md` or `DIRECTIVE.md`.
