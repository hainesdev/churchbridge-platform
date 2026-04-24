# Web Client Migration

## Context

The realtime translation contract has been cleaned up for the iOS-first architecture.

The server now treats these as the public translation events:

- `live_translation`
- `live_translation_clear`
- `feed_commit`
- `feed_revision`
- `segment_metadata`
- `caption_merge` only for `reason = segmentation_repair`

The legacy public translation events should no longer be relied on:

- `interim_translation`
- `translation`
- `correction`
- `translation_update`

This document describes what the web client must change to match the cleaned contract.

## Files To Update

- `C:\Users\Dan\Desktop\Projects\churchbridge-ai\client\lib\useTranslationFeed.ts`
- `C:\Users\Dan\Desktop\Projects\churchbridge-ai\client\components\TranslationDisplay.tsx`
- `C:\Users\Dan\Desktop\Projects\churchbridge-ai\client\lib\mergedVerseRouting.ts`

## Required Changes

### 1. Split live state from committed state

`useTranslationFeed.ts` currently mixes provisional English into `partialEnglish` and stable rows into `segments`.

It should be refactored into:

- dock/live state driven by `live_translation` and `live_translation_clear`
- committed feed state driven by `feed_commit`
- committed in-place revision driven by `feed_revision`

Recommended state shape:

- `liveEnglish`
- `liveSource`
- `liveUpdatedAt`
- `segments`
- `flashingId`
- `connected`

`partialEnglish` should be removed once the new live state is wired through the UI.

### 2. Key feed rows by `segment_id`

The web client currently keys rows by legacy `ts`.

That should change to:

- read `segment_id` as the canonical identifier
- fall back to `ts` only if needed during temporary local testing
- attach verse metadata by `segment_id`

`Segment.id` should represent the canonical segment identity, not merge-survivor routing.

### 3. Stop using `caption_merge` as the normal rendering path

Current behavior in `useTranslationFeed.ts` removes the absorbed row and rewrites the kept row.

That is no longer correct as a normal path.

New rule:

- `caption_merge` is exceptional segmentation repair only
- normal discourse continuity comes through `feed_revision` and future grouping metadata
- merge-survivor routing should not drive ordinary verse attachment or row identity

Short-term web behavior can still update the kept row when `caption_merge` arrives, but it should be treated as an exceptional repair path, not as the main flow.

### 4. Add a dedicated live surface

`TranslationDisplay.tsx` currently renders `partialEnglish` inside the scrolling reading area.

It should be changed so:

- full mode has a distinct live zone separate from stable history
- bilingual mode stops treating live English as just another trailing line
- lower-third mode can still show live text, but driven by `live_translation`

The web UI does not need to match the iOS dock visually, but it should follow the same conceptual split:

- live surface
- stable history

### 5. Update revision semantics

`feed_revision` can represent large meaning-level corrections.

The web client should:

- revise the existing committed row in place
- keep the row position stable
- allow optional visual flash/highlight
- avoid destructive removal as the normal correction mechanism

### 6. Preserve segmentation-repair behavior only where needed

If `caption_merge` arrives:

- require `reason = segmentation_repair`
- update the kept row
- treat absorbed-row handling as exceptional

If a merge arrives without `reason = segmentation_repair`, that should be treated as a contract error.

## Existing Web Assumptions That Are Now Wrong

These assumptions need to be removed:

- `interim_translation` is the live translation event
- `translation` always creates stable history
- `correction` and `translation_update` are the public revision path
- merge-survivor routing is the primary way to attach later metadata
- provisional text belongs inside the main feed

## Suggested Refactor Order

1. Update `useTranslationFeed.ts` to consume the new event family.
2. Replace `partialEnglish` with explicit live-state fields.
3. Update `TranslationDisplay.tsx` to render a dedicated live zone.
4. Restrict `caption_merge` handling to segmentation-repair-only behavior.
5. Simplify or remove merge-routing helpers once stable segment-based attachment is complete.

## Acceptance Criteria

The web migration is complete when:

- live English is driven by `live_translation`
- stable rows are created by `feed_commit`
- stable rows are revised by `feed_revision`
- `segment_id` is the canonical row identity
- `caption_merge` is treated as exceptional segmentation repair only
- the UI no longer assumes that provisional text is just another feed row
