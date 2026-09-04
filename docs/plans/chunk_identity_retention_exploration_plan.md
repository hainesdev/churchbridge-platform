# Chunk Identity Retention Exploration Plan

Last updated: 2026-05-15

## Purpose

This document turns the current chunk-identity work into a structured tuning
plan.

The goal is not only to preserve phrase-alignment metadata, but to decide which identity-retention strategy gives the best user-facing continuity without weakening alignment quality.

Status:

1. The core chunk-lineage payload is already implemented.
2. The display client already uses that payload for continuity-sensitive
   interaction.
3. This plan is now about improving reuse trustworthiness, not inventing the
   first version of the model.

## Current State

The current pipeline now emits:

1. `alignment_version`
2. `previous_alignment_version`
3. `root_segment_id`
4. `merged_from_segment_ids`
5. per-chunk `chunk_id`
6. per-chunk `english_span`
7. per-chunk `spanish_span`
8. per-chunk `derived_from_chunk_ids`

The client now:

1. parses the richer alignment payload
2. prefers span-based rendering over text-only rematching
3. resolves active hover and lock state through `chunk_id` and `derived_from_chunk_ids`
4. preserves locked state through safe adjacent-merge lineage remaps
5. drops locked-state transfer when ancestry is ambiguous instead of forcing
   false continuity

The system also now has targeted continuity coverage:

1. display interaction tests cover safe descendant transfer through
   `adjacent_merge`
2. display interaction tests cover ambiguous lineage that must not inherit
   active selection
3. replay coverage continues to exercise real websocket display behavior while
   richer alignment payloads are in use

## Latest Findings

From the current code and its supporting tests:

1. Phrase alignment is emitted after stable commit rather than during
   speculative live display.
2. All emitted chunks now include English and Spanish spans when alignment is
   accepted.
3. Unchanged chunks can retain identity across revisions.
4. Merge repairs preserve segment lineage even when phrase alignment is cleared
   and later regenerated.
5. Client continuity through lineage is now proven for at least:
   - unchanged chunks
   - adjacent-merge descendant remaps
   - ambiguity rejection when reuse is not trustworthy
6. Some merged or expanded chunks can still inherit ancestry a little too
   broadly, which is now the main tuning problem.

Interpretation:

1. Chunk identity retention is helping continuity more than alignment quality.
2. The next problem is not "more metadata."
3. The next problem is "more trustworthy reuse and lineage."

## Proven Baseline To Preserve

Any further tuning should preserve these behaviors:

1. chunk identity is a post-commit enhancement on top of stable display events
2. the display uses server-issued chunk identity, not raw text equality, as the
   primary continuity handle
3. safe descendant transfer is allowed only when the lineage shape is
   trustworthy
4. ambiguous lineage should degrade gracefully to no transfer rather than false
   certainty
5. merge-aware lineage at the segment level must survive phrase-alignment
   clearing and regeneration

## Problem Statement

Today the system is strong at:

1. producing phrase alignment after stable commit
2. preserving exact chunk identity through simple revisions
3. exposing enough metadata for the client to avoid brittle text-only matching

Today the system is weaker at:

1. deciding when a changed chunk is truly the same chunk
2. distinguishing one-to-one reshapes from ambiguous merge expansions
3. preventing over-eager ancestry when a new clause partially overlaps an older one

## Goals

1. Preserve `chunk_id` only when the mapping is genuinely stable.
2. Preserve lineage for useful continuity even when exact ID reuse is not safe.
3. Keep phrase-alignment quality at least as strong as the current implementation.
4. Improve highlight continuity across revisions and segmentation repairs.
5. Make ambiguity measurable so tuning decisions can be grounded in benchmark data.

## Non-Goals

1. Replacing the phrase-alignment generation model.
2. Rewriting the entire merge pipeline.
3. Building a token-level alignment engine unless simpler approaches fail.
4. Forcing highlight continuity in ambiguous cases where the mapping is not trustworthy.

## Candidate Solutions

### Solution A: Conservative Server-Side Reuse

Description:

Reuse a prior `chunk_id` only for very strong one-to-one matches.

Rules:

1. exact bilingual match -> reuse same `chunk_id`
2. exact span-preserving near-match -> reuse same `chunk_id`
3. split or merge -> fresh IDs with ancestry only
4. ambiguous overlap -> fresh ID

Pros:

1. simple to reason about
2. low risk of false continuity
3. easiest to benchmark and regress

Cons:

1. may give up continuity in borderline cases
2. can feel slightly conservative during mild clause reshapes

### Solution B: Structural Diff Remapper

Description:

Let the aligner produce a new chunk list, then run a dedicated remapping pass that compares old and new chunks using:

1. bilingual token overlap
2. span overlap
3. ordinal proximity
4. one-to-one vs one-to-many cardinality

Pros:

1. more nuanced than simple overlap thresholds
2. better suited to revision-heavy segments
3. can separate ID reuse from ancestry assignment

Cons:

1. more implementation complexity
2. more tuning surface

### Solution C: Prompt-Level Boundary Preservation Bias

Description:

Pass prior accepted alignment into the alignment prompt and tell the model to preserve boundaries when still valid.

Pros:

1. may improve both chunk quality and continuity
2. reduces post-hoc reshaping work when the model cooperates

Cons:

1. less deterministic than server-side logic
2. hard to tune safely in isolation

### Solution D: Client-Side Continuity Gate

Description:

Let the server emit lineage as it does now, but make the client more selective about transferring active highlight through ancestry.

Pros:

1. lowers UX risk even before server matching is perfect
2. cheap to iterate

Cons:

1. treats the symptom more than the source
2. does not improve identity quality in saved artifacts

### Solution E: Full Span-Anchor Identity Model

Description:

Move toward a more formal span-first identity model, where reuse is anchored by text offsets and structural edits rather than chunk text overlap.

Pros:

1. strongest long-term foundation
2. best fit for revision-aware rendering

Cons:

1. highest complexity
2. probably too expensive until simpler options plateau

## Recommended Exploration Order

Recommendation:

1. Solution A first
2. Solution B second
3. Solution D in parallel where cheap
4. Solution C only after measuring A and B
5. Solution E only if A through D still leave meaningful continuity problems

Why:

1. A gives the fastest improvement to trustworthiness.
2. B is the best follow-up if A proves too conservative.
3. D can improve the user experience without forcing a server rewrite.
4. C should be treated as a secondary assist, not the primary identity mechanism.
5. E should stay aspirational until the current proven baseline stops yielding
   useful incremental gains.

## Implementation Plan

### Phase 0: Instrumentation

Objective:

Measure reuse quality before changing reuse logic.

Server work:

1. add counters for:
   - `chunk_id_reused_count`
   - `chunk_lineage_only_count`
   - `chunk_ambiguous_match_count`
   - `chunk_fresh_after_merge_count`
2. add trace data for chunk remap decisions

Files:

1. `server/services/session_manager.py`

Success criteria:

1. benchmark artifacts can tell us not just that lineage exists, but how it was assigned

### Phase 1: Conservative Reuse Rules

Objective:

Tighten current reuse so exact and strong one-to-one mappings are preserved, and ambiguous cases fall back to fresh IDs.

Server work:

1. preserve ID on exact bilingual chunk match
2. preserve ID only when there is a single best prior candidate above a strong threshold
3. avoid reusing the same prior chunk for multiple materially different new chunks
4. keep ancestry when reuse is not safe

Files:

1. `server/services/session_manager.py`

Success criteria:

1. fewer questionable reuse cases in merge-heavy segments
2. unchanged chunks still retain IDs

### Phase 2: Structural Diff Remapper

Objective:

Improve the remap logic beyond exact match plus coarse overlap.

Server work:

1. score old-to-new chunk mappings using:
   - bilingual overlap
   - span overlap
   - ordinal distance
   - cardinality rules
2. allow one-to-one expansions or contractions to keep the same ID when clearly supported
3. keep split and merge cases as new IDs with ancestry

Files:

1. `server/services/session_manager.py`

Success criteria:

1. mild chunk reshapes preserve continuity more often
2. ambiguous many-to-one expansions stop reusing a single prior chunk too broadly

### Phase 3: Client Continuity Gate

Objective:

Make the browser preserve active highlight only when lineage is clear.

Client work:

1. prefer exact `chunk_id`
2. allow lineage-based remap only when there is one clear descendant
3. fall back to line-level highlight when ancestry is ambiguous

Files:

1. `client/lib/useTranslationFeed.ts`
2. `client/components/TranslationDisplay.tsx`

Success criteria:

1. user sees stable highlight continuity in simple repairs
2. user does not see misleading phrase carry-over in ambiguous repairs

### Phase 4: Prompt Bias Experiment

Objective:

Evaluate whether stronger boundary-preservation instructions improve the structural remap burden.

Server work:

1. adjust the alignment prompt to prefer prior accepted boundaries when still valid
2. measure whether this reduces chunk churn

Files:

1. `server/services/llm_enrichment_service.py`

Success criteria:

1. chunk reshapes become less noisy without lowering alignment quality

## Testing Plan

### Regression Tests

Add or extend tests in:

1. `tests/server/test_pipeline_regressions.py`

Required cases:

1. exact bilingual match keeps the same `chunk_id`
2. one-to-one expansion keeps the same `chunk_id` only when the mapping is clear
3. one old chunk splitting into two yields fresh IDs with ancestry
4. two old chunks merging into one does not blindly reuse one prior ID
5. ambiguous overlap produces fresh IDs and increments ambiguity counters
6. all emitted chunks still carry valid English and Spanish spans

### Client Interaction Tests

Add Playwright coverage for:

1. lock a phrase
2. inject a `feed_revision` with the same `chunk_id`
3. verify the active phrase remains highlighted
4. inject a `feed_revision` with a descendant chunk via `derived_from_chunk_ids`
5. verify remap only occurs when the descendant is unambiguous
6. verify fallback behavior when the mapping is ambiguous

Likely files:

1. `client/e2e/web-client-replay.spec.ts`
2. or a new dedicated interaction spec for display continuity

### Benchmark Plan

Run the same 60-second clip window used in the existing comparison set.

Comparison artifacts:

1. baseline:
   - `tests/benchmark/results/manual-60s/1/pipeline/2026-05-06T16-51-00-742086Z-1-0-62e6e0b8.json`
2. current chunk-identity implementation:
   - `tests/benchmark/results/chunk-identity-60s/1/pipeline/2026-05-07T15-34-10-817069Z-1-0-8e6f3037.json`

Recommended reruns:

1. rerun the same 60-second window after Phase 1
2. rerun the same 60-second window after Phase 2
3. if prompt changes are attempted, run a focused A/B pair with and without the prompt adjustment

### Metrics To Compare

Do not treat WER as the only outcome.

Track:

1. raw STT WER
2. committed sentence WER
3. phrase-alignment revision count
4. chunk count with spans
5. `chunk_id_reused_count`
6. `chunk_lineage_only_count`
7. `chunk_ambiguous_match_count`
8. segments with multiple alignment versions
9. user-visible highlight continuity in browser tests

## Evaluation Criteria

### Phase 1 Success

1. questionable ID reuse drops
2. exact-match continuity remains intact
3. no meaningful regression in alignment revision volume

### Phase 2 Success

1. mild clause reshapes preserve continuity better than Phase 1
2. ambiguous merge cases still avoid unsafe reuse

### Client Success

1. phrase highlight survives simple revisions
2. ambiguous cases degrade safely to less specific highlight behavior

## Risks

1. over-conservative reuse could reduce continuity more than users want
2. structural diff logic could become difficult to reason about if it grows too heuristic
3. prompt tuning could hide structural problems instead of fixing them
4. browser continuity tests may require deterministic mock revisions rather than only live replay

## Decision Gates

After Phase 1:

1. If ambiguity drops and continuity remains acceptable, stop there.
2. If continuity becomes too conservative, proceed to Phase 2.

After Phase 2:

1. If benchmark evidence shows better continuity without misleading reuse, keep the remapper.
2. If improvements are marginal, prefer the simpler Phase 1 approach.

After Phase 4:

1. Keep prompt bias only if it reduces churn measurably and does not lower alignment trustworthiness.

## Immediate Next Step

Start with Phase 0 and Phase 1 together:

1. add reuse and ambiguity counters
2. tighten same-ID reuse to strong one-to-one matches
3. add regression coverage for exact reuse, split, merge, and ambiguity
4. rerun the same 60-second benchmark window for direct comparison
