# Bilingual Display And Pair Generation Plan

## Goals

1. Make English the primary reading surface in the live display.
2. Keep Spanish visible enough to support bilingual comprehension and confidence.
3. Improve English/Spanish phrase-pair quality so linked highlighting feels trustworthy.
4. Reduce unstable or low-value pair generation on partial, merged, or still-changing segments.

## Product Direction

### Display Principles

1. English is the dominant line.
2. Spanish remains visible as the supporting line.
3. Hover and tap interactions should link corresponding English and Spanish phrases in both directions.
4. When phrase-level alignment is unavailable or weak, the UI should fall back to whole-line paired highlighting.
5. Live, in-progress text should optimize for readability first and detailed pair inspection second.

### Why This Direction

Recent committed segment data shows three important patterns:

1. Many committed English/Spanish sentence pairs are close enough for strong linked highlighting.
2. Some segments are long paraphrastic sermon sentences where phrase alignment needs to be conservative.
3. Some transient segments are clearly incomplete fragments, such as short bridges or truncated repairs, and should not generate phrase pairs early.

This means phrase alignment should be treated as a stable post-commit enhancement, not a speculative live-text feature.

## Pair Generation Principles

### Quality Goals

Phrase pairs should be:

1. Ordered left to right in both languages.
2. Grounded in actual source wording, not inferred paraphrase.
3. Readable enough for UI highlighting.
4. Stable enough that a user can trust the highlight relationship.

### Anti-Goals

We should avoid:

1. Generating pairs for incomplete fragments.
2. Generating pairs before segmentation and repair logic settle.
3. Returning overly tiny function-word fragments.
4. Preserving alignments that no longer match the final committed text.

## Technical Direction

### Current Problems

The current alignment flow can run before final segment commitment. That creates several risks:

1. It spends work on text that may later merge or revise.
2. It can generate pairs for unstable fragments.
3. It increases downstream revision churn.
4. It makes it harder to reason about pair quality in the UI.

### New Direction

Phase 1 should move phrase-alignment requests to the stable commit path.

Desired flow:

1. Google and LLM translation continue to update live English as they do now.
2. A committed segment is broadcast as soon as the final English and Spanish pair is ready.
3. Phrase alignment is requested only after that committed pair exists.
4. The resulting alignment arrives as a follow-up enhancement revision.

This keeps the display fast while making alignment quality more trustworthy.

## Phases

### Phase 0: Documentation And Guardrails

Deliverables:

1. This design document.
2. Clear quality goals for display and pair generation.
3. A phased implementation sequence that minimizes UI churn.

### Phase 1: Post-Commit Pair Generation

Objective:

Generate phrase pairs only after a segment is committed.

Changes:

1. Stop scheduling phrase alignment during speculative enrichment stages.
2. Request phrase alignment from the commit path using the final committed English and Spanish.
3. Use cached metadata such as source quality, translation register, discourse tag, and verse context when available.
4. Keep alignment asynchronous so commit speed does not regress.

Success criteria:

1. No phrase-alignment work is triggered for transient pre-commit fragments.
2. Stable committed segments still receive follow-up phrase alignment.
3. Existing feed revision logic continues to deliver alignment as a non-blocking enhancement.

### Phase 2: Stronger Alignment Validation

Objective:

Make alignment acceptance stricter and more bilingual.

Changes:

1. Add Spanish-side ordered coverage validation.
2. Reject alignments that skip around or over-fragment meaning.
3. Suppress low-information chunks dominated by stopwords or punctuation.
4. Tighten fragment rejection rules for short or incomplete committed segments.

Success criteria:

1. Phrase pairs are more trustworthy for linked highlighting.
2. Fewer low-quality alignments reach the UI.

### Phase 3: Display Upgrade

Objective:

Use improved pair quality to support the new bilingual display interaction.

Changes:

1. Keep English and Spanish visible together in the display.
2. Make English visually dominant.
3. Render aligned spans when available.
4. Support hover and tap cross-highlighting in both directions.
5. Fall back to whole-line highlighting when phrase alignment is missing.

Success criteria:

1. Users can quickly see correspondence between the two languages.
2. The UI remains readable on both desktop and touch devices.

### Phase 4: Observability And Tuning

Objective:

Measure pair quality using real session output.

Changes:

1. Persist or log emitted phrase-alignment payloads for later inspection.
2. Add metrics for alignment request volume, suppression reasons, and successful emissions.
3. Review recent sessions to tune thresholds and prompt rules.

Success criteria:

1. Alignment quality can be evaluated from production-like data, not just code inspection.
2. Future UI work can rely on observed quality rather than assumptions.

## Implementation Notes

### Commit-Time Inputs

The post-commit alignment request should use:

1. Final committed Spanish.
2. Final committed English.
3. Original Google English baseline when available.
4. A vetted interim English hint when it clearly extends the same sentence.
5. Source quality metadata.
6. Translation register metadata.
7. Discourse tag metadata.
8. Verse context when available.

### Interim English Evaluation

Interim English is useful, but only in a narrow way.

Recommendation:

1. Do not let interim English replace the committed English for pair generation.
2. Cache the strongest interim English hypothesis seen during the active sentence window.
3. Pass that interim text into phrase alignment only when it is clearly related to the final committed sentence and meaningfully extends it.
4. Treat interim English as a secondary hint for clause boundaries or dropped words, not as the alignment source of truth.

This improves pairing for cases where the final displayed English is slightly shortened or repair-shaped, while avoiding noisy speculative alignment from unstable live text.

### Revision Behavior

In Phase 1, alignment should remain a follow-up enhancement and should not block feed commit.

Later phases may extend this to:

1. Re-request alignment after committed segmentation repairs.
2. Re-request alignment after meaningful committed English revisions.

## Risks

1. Phrase highlighting may appear slightly later than the committed sentence.
2. Some committed segments may still be too weak for useful phrase alignment.
3. Tests that assume speculative alignment scheduling may need updates.

These risks are acceptable because they trade speculative behavior for stability and trust.

## Recommended Immediate Next Step

Start with Phase 1:

1. Trigger alignment from the commit path.
2. Stop speculative alignment scheduling.
3. Validate that committed segments still receive phrase-alignment revisions.

## Phase 5: Chunk Identity Retention

Objective:

Retain phrase identity across committed revisions and segmentation repairs so linked highlighting feels continuous instead of reset-heavy.

### Alignment Payload Schema

Phrase-alignment payloads should carry identity, spans, and lineage:

1. `alignment_version`
2. `previous_alignment_version`
3. `root_segment_id`
4. `merged_from_segment_ids`
5. `phrase_alignment[]`

Each phrase item should carry:

1. `chunk_id`
2. `english_text`
3. `spanish_text`
4. `english_span`
5. `spanish_span`
6. `ordinal`
7. `derived_from_chunk_ids`

### Why This Helps

This schema lets the client:

1. Key hover and tap-lock state by stable chunk identity instead of raw text.
2. Prefer exact span rendering over brittle substring rematching.
3. Transfer active selection forward when a chunk is preserved or lightly reshaped.
4. Fall back gracefully when a merge or repair truly invalidates the previous chunk map.

### Implementation Notes

1. Exact bilingual chunk matches should reuse the prior `chunk_id`.
2. Newly split or reshaped chunks should get a fresh `chunk_id` and point back through `derived_from_chunk_ids`.
3. Spans must be computed against the final displayed English and Spanish strings.
4. Merge repairs should preserve segment lineage even when phrase alignment is temporarily cleared and regenerated.

### Validation Goals

Success criteria:

1. Phrase-alignment revisions preserve `chunk_id` for unchanged chunks.
2. Merge-heavy segments expose ancestry metadata instead of losing all structure.
3. The client can preserve active highlight state through at least simple context repairs.
4. Browser replay and listener flows continue to pass with the richer payload.

### Follow-Up Exploration

The chunk-identity payload is now implemented, but the next tuning problem is trustworthy reuse rather than missing metadata.

See:

1. `docs/plans/chunk_identity_retention_exploration_plan.md`

That follow-up plan covers:

1. stricter same-ID reuse rules
2. structural remap experiments
3. client-side continuity gates
4. benchmark and Playwright validation strategy
