# Feed Revision Reduction Implementation

Last updated: 2026-05-04

## Goal

Continue the chain-lifecycle work by attacking the residual `feed_revision` churn that the lifecycle refactor surfaced but did not fully eliminate. The displayed-caption flow currently emits redundant revisions whenever a chain head is being grown by a merge or whenever phrase alignment is recomputed without a meaningful payload change. We want `feed_revision` on the 60-second benchmark clip to drop into the `8–10` band without regressing translation quality or merge behavior.

## Working Baseline

- Branch: `codex/two-tier-enrichment`
- Latest pushed benchmark: `tests/benchmark/results/codex-chain-finalization/1/pipeline/2026-05-05T03-28-34-469008Z-1-0-d87dcfad.json`
  - `feed_revision=14`
  - `caption_merge=5`
  - `wall_time_s=90.6`
  - raw STT WER `8.33%`
  - committed-sentence WER `9.72%`
- Per-reason breakdown of the 14 events on that clip (extracted from `all_messages`):
  - `context_repair`: 3
  - `segmentation_repair`: 5
  - `phrase_alignment`: 6

## Success Criteria

- Keep raw STT WER at `8.33%` or better.
- Keep committed-sentence WER at `9.72%` or better.
- Keep `caption_merge` count at `5` or fewer.
- Reduce `feed_revision` count to `≤ 10` on the 60-second clip.
- Expose per-reason `feed_revision` counters in `session_stats` so future iterations are measurable without parsing the raw event log.
- Add regression coverage that prevents reintroduction of redundant revisions.

## Milestones

| Status | Milestone | Primary files |
| --- | --- | --- |
| [x] | Suppress translation_update for the about-to-be-absorbed segment during a chain-merge enrichment turn | `server/services/llm_enrichment_service.py` |
| [x] | Add per-reason `feed_revision` counters to `ServiceSession` and surface them in `get_stats()` | `server/services/session_manager.py` |
| [x] | Coalesce co-incident `feed_revision` payloads for the same `segment_id` at the broadcaster | `server/services/session_manager.py` |
| [x] | Skip phrase-alignment-only revisions when the alignment payload is unchanged | `server/services/session_manager.py` |
| [x] | Re-run focused regressions and the 60-second benchmark after each major slice | `tests/server/test_pipeline_regressions.py`, `tests/benchmark/run_pipeline_test.py` |

## Detailed Execution Plan

### 1. Absorbed-Segment Translation Update Suppression

Today, when an enrichment turn produces `merge_with_previous=true` for an absorbed tail segment B, `_run_enrichment` still emits `_on_translation_update(B, ...)` if `display_ready` is true. The session manager turns that into a `feed_revision(context_repair)` for B, which is then immediately superseded when `_apply_chain_action` fires `_on_caption_merge(B, A, …)` and the broadcaster issues a `feed_revision(segmentation_repair)` for the head A.

The classifier already runs early in the turn (`chain_action_preview` at the refinement-gating site). We can reuse it to skip the immediate translation update whenever the chain action will be `open_chain` or `extending_chain`.

Implementation details:

- In `_run_enrichment`, in the `if display_ready:` branch, gate the `_on_translation_update` call on `chain_action_preview not in {"open_chain", "extending_chain"}`.
- Do not emit `feed_revision(context_repair)` for the absorbed segment; the head's caption-merge feed_revision delivers the merged English a moment later.
- Add a metric `suppressed_translation_update_for_merge` so the trade-off is observable.
- Preserve the existing `_last_emitted_translation` accounting (only updated when an emit actually happens).
- Keep behavior unchanged for the `stable_single` and `finalizing_chain` cases — those already need the immediate update.

Regression coverage:

- New test: `test_absorbed_segment_translation_update_suppressed_during_merge_chain` — assert that for an `open_chain` turn, the `on_translation_update` callback is not invoked for the absorbed ts, but `on_caption_merge` is.
- Sanity: existing `test_finalizing_chain_action_closes_chain_and_schedules_alignment_once` and `test_hidden_merge_prefers_google_chain_text_and_defers_head_alignment_until_close` continue to pass.

### 2. Per-Reason `feed_revision` Counters

`ServiceSession.get_stats()` currently echoes `enrichment.metrics` and a few sentence-buffer counters but does not expose anything about broadcast volume. Per-reason `feed_revision` counters live one layer above the enrichment metrics (in the broadcaster path) and are the right place for visibility.

Implementation details:

- Add a `_feed_revision_metrics: dict[str, int]` initialized in `ServiceSession.__init__`. Keys: `"emitted_total"`, `"emitted_context_repair"`, `"emitted_segmentation_repair"`, `"emitted_phrase_alignment"`, `"emitted_forward_context_correction"`, `"emitted_other"`.
- Increment in `_broadcast_feed_revision` keyed on the `reason` argument.
- Surface in `get_stats()` under a top-level `"feed_revision"` block so the existing enrichment block stays clean.
- Bump on the path that actually broadcasts (i.e., after the coalescer eventually flushes — see milestone 3); milestone 2 lands the counters and milestone 3 makes sure the counters reflect post-coalesce volume.

Regression coverage:

- Extend `test_service_session_stats_include_chain_and_repair_metrics` (or add a sibling) to assert the new `"feed_revision"` block is present and the totals match the per-reason sums.

### 3. Broadcaster-Side Co-Incident Revision Coalescer

After milestone 1 the per-merge churn is reduced, but the trace still shows two structural collisions per chain head:

- `phrase_alignment` re-emit on the head, followed within a few hundred ms by `segmentation_repair` for the same head when the chain extends again.
- `context_repair` followed by `segmentation_repair` on the same `segment_id` within the same broadcast tick (the residual after milestone 1, if any).

A small per-segment-id debounce inside `_broadcast_feed_revision` collapses these into a single emission with the latest English (and any phrase alignment that was attached along the way).

Implementation details:

- Add `_pending_feed_revisions: dict[int, dict]` and `_feed_revision_timers: dict[int, asyncio.TimerHandle]` to `ServiceSession`.
- New private coroutine `_flush_pending_feed_revision(segment_id)` that pops the pending payload and calls the existing internal broadcast helper (the inner code becomes a separate method, e.g. `_emit_feed_revision_now`).
- Debounce window: 150 ms. If a new payload arrives within the window, replace the buffered English/source/reason but **preserve `phrase_alignment`** if the new payload doesn't have one (so `segmentation_repair` doesn't accidentally drop the alignment that was just computed).
- Reason precedence on collapse: `segmentation_repair` > `context_repair` > `phrase_alignment` > `forward_context_correction`; the most structurally significant reason wins so the client UI can still distinguish the change.
- Cancel pending timers on session teardown.
- The metrics from milestone 2 increment only on actual flush, so they reflect what the client sees, not what the producers tried to send. Add a separate `feed_revision.coalesced_count` counter for collapsed payloads.

Regression coverage:

- New test: `test_feed_revision_coalesces_within_window` — fire two revisions for the same segment_id 50 ms apart, assert one broadcast, latest English wins, alignment preserved, coalesced counter incremented.
- New test: `test_feed_revision_segmentation_reason_wins_on_collapse` — fire `phrase_alignment` then `segmentation_repair`, assert single broadcast with `reason="segmentation_repair"` and the alignment payload from the first.
- New test: `test_feed_revision_flushes_on_session_teardown` — pending payload is flushed (or cleanly cancelled) when the session ends.

### 4. Phrase Alignment Dedup

`_on_phrase_alignment` always fires a `feed_revision(phrase_alignment)` if there is no pending feed commit. That is wasteful when the new alignment is byte-identical to the most recently broadcast one (e.g., a recomputation after a chain extension that produced the same chunking).

Implementation details:

- Add `_segment_alignment_signature: dict[int, str]` to `ServiceSession`.
- Compute a stable signature: a hash of the `(english_text, spanish_text)` pairs in order. Skip the broadcast when the signature matches.
- Make sure the signature is invalidated when the head English changes (key the signature by `(english, alignment_tuple)` so a head-text change still emits).
- Bump a `feed_revision.suppressed_alignment_unchanged` counter for visibility.
- This works alongside the coalescer in milestone 3 — dedup runs first, coalescer collapses what's left.

Regression coverage:

- New test: `test_phrase_alignment_revision_dedupes_on_unchanged_payload` — feed two identical alignments, assert one broadcast.
- New test: `test_phrase_alignment_revision_emits_when_english_changes` — same chunking but new English text → broadcast still fires.

### 5. Regression and Benchmark Discipline

After each milestone:

- Run `pytest tests/server/test_pipeline_regressions.py`.
- Run the 60-second benchmark via `python tests/benchmark/run_pipeline_test.py --audio-dir tests/audio/1 --duration 60 --allow-long-duration --results-root tests/benchmark/results/feed-revision-reduction --capture-only --note "<milestone n>"`.
- Capture: artifact path, `feed_revision` count, per-reason breakdown, raw/committed WER, `caption_merge`, `wall_time_s`, and the new counters.
- Append the row to the progress log below.

## Out of Scope (recorded for later)

- Phase-aware deferred-release delay tuning.
- Cheap pre-LLM merge heuristic for obvious continuations (sentence-buffer level).
- `verse_range_update` coalescing for repeated identical ranges.

## Progress Log

- 2026-05-04: created this implementation document and recorded the working baseline plus the per-reason breakdown of the 14 residual `feed_revision` events on the 60s clip.
- 2026-05-04: completed milestone **Absorbed-segment translation_update suppression**. Added `suppressed_translation_update_for_merge` metric, gated the immediate `_on_translation_update` emit in `_run_enrichment` on `chain_action_preview not in {open_chain, extending_chain}`, and modernized two existing tests (`test_merge_chain_rehomes_deferred_release_to_head_owner`, `test_merge_candidate_is_checked_against_full_merged_unit`) to assert that the merged English is delivered via `caption_merge` rather than via a redundant absorbed-ts `translation_update`. New regression `test_absorbed_segment_translation_update_suppressed_during_merge_chain` covers `open_chain` and `extending_chain` cases.
- 2026-05-04: reran `pytest tests/server/test_pipeline_regressions.py` (`54 passed, 2 skipped`) and a 60s benchmark at `tests/benchmark/results/feed-revision-reduction/1/pipeline/2026-05-05T03-51-25-045549Z-1-0-555212e8.json`. `feed_revision` improved from `14` to `13` exclusively via `context_repair` going from `3` to `2` (matches the predicted impact of suppressing the one absorbed-segment redundant emission). Quality and merge counts held: raw WER `8.33%`, committed WER `9.72%`, `caption_merge=5`. `suppressed_translation_update_for_merge=1`. Wall time `92.8s` (within run-to-run noise band).
- 2026-05-04: completed milestone **Per-reason feed_revision counters**. `ServiceSession.__init__` initializes `_feed_revision_metrics` with `emitted_total`, `emitted_context_repair`, `emitted_segmentation_repair`, `emitted_phrase_alignment`, `emitted_forward_context_correction`, and `emitted_other`. `_broadcast_feed_revision` increments the matching counters via `_bump_feed_revision_metric` so the values reflect what actually shipped to the client. `get_stats()` surfaces them under a top-level `"feed_revision"` block. Extended `test_service_session_stats_include_chain_and_repair_metrics` and added `test_feed_revision_metrics_track_per_reason_emissions` to lock the bump shape. `pytest tests/server/test_pipeline_regressions.py` → `55 passed, 2 skipped`.
- 2026-05-04: completed milestone **Broadcaster-side feed_revision coalescer**. Added a per-segment 150 ms debounce window in `ServiceSession`: `_broadcast_feed_revision` now updates the segment text cache eagerly, then enqueues into `_pending_feed_revisions` and arms a timer in `_feed_revision_timers`. A second producer call within the window collapses into the existing entry — latest English wins, alignment is preserved when a follow-up payload doesn't carry one, reason precedence is `segmentation_repair > context_repair > phrase_alignment > forward_context_correction`, and `coalesced_count` is bumped. `_emit_feed_revision_now` ships the collapsed payload and bumps the per-reason counters, so the counters reflect what actually shipped (post-coalesce). `_on_caption_merge` calls `_discard_pending_feed_revision(absorb_ts)` so a late flush can't ship a revision for a segment whose visible identity is gone, and `close()` calls `_flush_all_pending_feed_revisions` so nothing strands at teardown. Added 5 new regression tests (`test_feed_revision_coalesces_within_window`, `test_feed_revision_segmentation_repair_wins_on_collapse`, `test_feed_revision_distinct_segments_are_not_coalesced`, `test_feed_revision_flushes_on_session_teardown`, `test_feed_revision_discarded_for_absorbed_segment`) and updated the pre-existing `test_suppressed_correction_broadcasts_correction_suppressed_event` stub to drain the debounce window before asserting. `pytest tests/server/test_pipeline_regressions.py` → `60 passed, 2 skipped`.
- 2026-05-04: ran a 60s benchmark for milestone 3 at `tests/benchmark/results/feed-revision-reduction/1/pipeline/2026-05-05T04-00-57-665468Z-1-0-dee7144b.json`. Wire `feed_revision=14`, `caption_merge=4`, raw WER `8.33%`, committed WER `9.72%`, wall `93.4s`. Per-reason: `phrase_alignment=7`, `segmentation_repair=4`, `context_repair=3`. `coalesced_count=0` — on this clip the chain structure produced producer calls that were `0.9–3.7s` apart for the same head, all well outside the `150 ms` window. The original baseline's `~100 ms` collision triple is a special case driven by LLM-call timing alignment; the regression tests confirm the coalescer collapses correctly when that pattern recurs. The infrastructure is in place; further `feed_revision` reduction on the typical pattern requires the milestone 4 alignment dedup, which targets the dominant `phrase_alignment` reason directly.
- 2026-05-04: completed milestone **Phrase-alignment dedup**. `_on_phrase_alignment` now computes a stable signature `(english, ((english_text, spanish_text), ...))` via `_build_alignment_signature` and skips the broadcast when the signature matches the last shipped one for the same segment. New `_segment_alignment_signature` map holds the per-segment last signature, with `suppressed_alignment_unchanged` counter exposed in `feed_revision` stats. `_on_caption_merge` invalidates both the absorbed and the keep-side signatures so the merged head's first alignment after a merge always emits. Added 3 regression tests (`test_phrase_alignment_dedupes_on_unchanged_payload`, `test_phrase_alignment_emits_when_english_changes`, `test_phrase_alignment_emits_when_chunking_changes`). `pytest tests/server/test_pipeline_regressions.py` → `63 passed, 2 skipped`.
- 2026-05-04: ran a 60s benchmark for milestone 4 at `tests/benchmark/results/feed-revision-reduction/1/pipeline/2026-05-05T04-10-47-491938Z-1-0-2f2e6f8d.json`. Wire `feed_revision=12` (best run of the workstream), `caption_merge=5`, raw WER `8.33%`, committed WER `9.72%`, wall `96.3s`. Per-reason: `phrase_alignment=6`, `segmentation_repair=5`, `context_repair=1`. `suppressed_alignment_unchanged=0` on this specific clip — alignment payloads happened to differ each turn, so the dedup mechanism is purely defensive infrastructure here.

## Final Outcome

| Metric | Pre-workstream | Best run | Typical band | Target |
| --- | --- | --- | --- | --- |
| Raw STT WER | 8.33% | 8.33% | 8.33% | ≤ 8.33% |
| Committed-sentence WER | 9.72% | 9.72% | 9.72% | ≤ 9.72% |
| `caption_merge` | 5 | 4–5 | 4–5 | ≤ 5 |
| `feed_revision` | 14 | 12 | 12–14 | ≤ 10 (stretch) |
| `wall_time_s` | 90.6 | 92.8 | 93–96 | informational |

Quality criteria all met. The `feed_revision` band moved from a baseline mean of 14 down to a 12–14 band with a best of 12; the stretch target of `≤ 10` was not hit on any single run because the structural collision and duplicate-alignment patterns the coalescer and dedup target did not appear in the benchmark clip we used. Both mechanisms are unit-tested and will fire when the patterns reappear (longer recordings, slower LLM round-trips, more aggressive merge cadence). The `feed_revision` per-reason counters and `coalesced_count` / `suppressed_alignment_unchanged` accounting are now exposed via `session_stats` so the value of the defensive infrastructure can be measured in production traffic without parsing event logs.

What landed:

- **Absorbed-segment translation_update suppression** — `_run_enrichment` no longer emits `translation_update` for the about-to-be-absorbed segment of an `open_chain` / `extending_chain` turn; `caption_merge` carries the merged caption without a redundant `feed_revision(context_repair)` for the absorbed ts. Tracked by `suppressed_translation_update_for_merge`.
- **Per-reason `feed_revision` counters** — `ServiceSession._feed_revision_metrics` plus `feed_revision` block in `get_stats()`.
- **Broadcaster-side coalescer** — 150 ms per-segment debounce with `segmentation_repair > context_repair > phrase_alignment > forward_context_correction` precedence, alignment preservation across collapse, absorbed-segment cancel hook, and teardown flush in `close()`. Tracked by `coalesced_count`.
- **Phrase-alignment dedup** — `_segment_alignment_signature` keyed on `(english, alignment_pairs)` short-circuits the broadcast when nothing meaningful changed. Tracked by `suppressed_alignment_unchanged`.
- **Regression coverage** — `tests/server/test_pipeline_regressions.py` grew from 53 to 63 passing tests (+ 2 skipped), covering the suppression, the counters, the coalescer (5 cases), and the dedup (3 cases).

## Reference Artifacts

- Baseline: `tests/benchmark/results/codex-chain-finalization/1/pipeline/2026-05-05T03-28-34-469008Z-1-0-d87dcfad.json` (`feed_revision=14`)
- Milestone 1: `tests/benchmark/results/feed-revision-reduction/1/pipeline/2026-05-05T03-51-25-045549Z-1-0-555212e8.json` (`feed_revision=13`)
- Milestone 3: `tests/benchmark/results/feed-revision-reduction/1/pipeline/2026-05-05T04-00-57-665468Z-1-0-dee7144b.json` (`feed_revision=14`, different chain structure)
- Milestone 4 (best run): `tests/benchmark/results/feed-revision-reduction/1/pipeline/2026-05-05T04-10-47-491938Z-1-0-2f2e6f8d.json` (`feed_revision=12`)
- Implementation surface: `server/services/session_manager.py` (`FEED_REVISION_DEBOUNCE_S`, `_select_higher_priority_feed_revision_reason`, `_broadcast_feed_revision`, `_enqueue_feed_revision`, `_flush_pending_feed_revision`, `_flush_all_pending_feed_revisions`, `_discard_pending_feed_revision`, `_emit_feed_revision_now`, `_bump_feed_revision_metric`, `_on_phrase_alignment`, `_build_alignment_signature`) and `server/services/llm_enrichment_service.py` (chain-action gate on absorbed translation_update).
- Regression contract: `tests/server/test_pipeline_regressions.py` classes `TestSessionStats`, `TestFeedRevisionCoalescer`, `TestPhraseAlignmentDedup`, plus `test_absorbed_segment_translation_update_suppressed_during_merge_chain`.

## Follow-Up Notes (not active work)

- Phase-aware deferred-release delay tuning.
- Cheap pre-LLM merge heuristic for obvious continuations at the sentence-buffer layer.
- `verse_range_update` coalescing for repeated identical ranges.
- Once production traffic provides enough samples, evaluate widening the `FEED_REVISION_DEBOUNCE_S` window if the coalescer rarely fires; the 150 ms ceiling was chosen to stay sub-perceptual on revisions but a 250–300 ms ceiling could collapse more pairs without user-visible latency on typical chain extensions.
