# Topic Tracker Semantic Memory Plan

Last updated: 2026-05-05

## Purpose

This document describes the desired role of `TopicTracker` in the ChurchBridge
pipeline and, importantly, reflects what is already implemented in the codebase
today versus what still remains to be validated or built.

The goal is to use `TopicTracker` as a structured sermon-memory layer that
improves:

- live translation accuracy
- structural enrichment accuracy
- verse detection and suggestion quality
- operator explainability
- post-service observability and review

The tracker must remain off the latency-critical caption path.

## Review Outcome

During review of this plan against the current codebase, several capabilities
that were originally written as future work were found to already exist in:

- [topic_tracker.py](C:\Users\Dan\Desktop\Projects\churchbridge-ai\server\services\topic_tracker.py)
- [llm_enrichment_service.py](C:\Users\Dan\Desktop\Projects\churchbridge-ai\server\services\llm_enrichment_service.py)

This corrected version therefore separates:

- implemented architecture
- partial or transitional architecture
- remaining implementation work

## Current Architecture

### Pipeline Position

The current pipeline has the right overall separation of concerns:

- STT and sentence buffering create stable sermon units
- main enrichment makes fast per-sentence structural decisions
- `SermonStateTracker` stabilizes rhetorical mode
- `TopicTracker` maintains broader sermon context

`TopicTracker` is created once per live service session. Flushed final
segments are handed to it from `ServiceSession._on_sentence(...)`, which calls
`TopicTracker.add_segment(...)`. Main enrichment then reads topic state at the
start of each sentence-level enrichment turn.

### What TopicTracker Already Does

The current implementation already supports:

- structured semantic memory via nested dataclasses
- dedicated `get_memory()` access
- backward-compatible `get_context()` and `get_context_obj()`
- structured prompt blocks via `get_prompt_blocks_text()`
- periodic refresh scheduling
- enrichment-driven refresh requests through `request_refresh(...)`
- signal dedupe and cooldowns
- one in-flight refresh task at a time
- pending strongest-request replay after in-flight completion
- dedicated topic-tracker model configuration
- topic-tracker metrics
- observability events for summary prompt, response, applied state, and errors

### Transitional State

The topic-memory architecture is partly migrated, not fully finished.

What is still transitional:

- main enrichment still keeps the flat `get_context()` fallback path
- diagnostics UI does not yet present topic memory as a first-class live panel
- the newer refresh behavior needs replay-benchmark and live-service validation
- schema shape may still need refinement after operator use

## Design Principles

1. Keep topic tracking asynchronous.
   Topic refreshes must never delay first-visible captions.

2. Treat topic tracking as semantic memory, not just summary text.
   The canonical output should be a structured state object.

3. Use enrichment to signal refresh need, not to perform summary generation.
   Main enrichment sees discourse shifts first and is the right trigger source.

4. Preserve hard and soft context separately.
   Active passage should be treated differently from themes and summaries.

5. Optimize prompt shape before token count.
   Better structured evidence beats a blindly larger transcript dump.

6. Spend model quality where latency tolerance exists.
   Topic tracking can tolerate a stronger model than live per-sentence
   enrichment.

## Canonical Topic Memory Shape

The current implementation is already close to the right shape. The canonical
memory object should continue to be organized into these sections:

```json
{
  "active_passage": {
    "reference": "1 John 1:5-7",
    "canonical_english": "God is light...",
    "confidence": "explicit",
    "source": "verse_detection",
    "updated_at_ts": 1778000000000
  },
  "sermon_state": {
    "current_mode": "exposition",
    "sermon_arc": "development",
    "rhetorical_goal": "establishing the implications of walking in the light",
    "confidence": 0.86,
    "updated_at_ts": 1778000000000
  },
  "theme_state": {
    "primary_themes": ["light", "fellowship", "confession"],
    "supporting_themes": ["truthfulness", "holiness"],
    "theme_shift": false,
    "updated_at_ts": 1778000000000
  },
  "illustration_state": {
    "active": false,
    "subject": null,
    "started_at_ts": null,
    "updated_at_ts": 1778000000000
  },
  "summary_state": {
    "short_summary": "The preacher is expounding 1 John 1 and stressing that walking with God requires honesty, confession, and fellowship in the light.",
    "long_summary": "The sermon has moved from announcing the passage into exposition of God's holiness and the believer's response...",
    "last_refresh_ts": 1778000000000,
    "refresh_reason": "passage_change"
  },
  "evidence": {
    "recent_segments": [
      "Dios es luz y no hay ningunas tinieblas en el",
      "si decimos que tenemos comunion con el"
    ],
    "recent_mode_history": ["scripture", "scripture", "exposition", "exposition"],
    "recent_trigger_reasons": ["passage_change", "mode_shift"]
  }
}
```

### Field Roles

- `active_passage`
  - hard context
  - should be trusted for passage continuity and verse-suggestion suppression

- `sermon_state`
  - semi-hard context
  - drives rhetorical interpretation and translation register decisions

- `theme_state`
  - soft context
  - supports theological vocabulary continuity and broader semantic framing

- `illustration_state`
  - semi-hard context
  - useful for suppressing false scripture inference during narrative turns

- `summary_state`
  - human/operator-facing summary plus compact prompt context

- `evidence`
  - debugging and observability support
  - should stay bounded and lightweight

## Refresh Trigger Model

The best trigger model is now largely implemented:

- periodic fallback refreshes still exist
- main enrichment can emit `topic_refresh_signal`
- topic tracker can decide whether to schedule or suppress the refresh

### Structural Signal Shape

The structural enrichment schema already includes:

```json
{
  "topic_refresh_signal": {
    "strength": "none | soft | strong",
    "reason": "passage_change | mode_shift | theme_shift | illustration_started | illustration_ended | application_started | exhortation_started | altar_call_started | closing_shift"
  }
}
```

### Trigger Semantics

- `none`
  - no explicit refresh request

- `soft`
  - refresh after normal signal cooldown
  - intended for gradual rhetorical movement

- `strong`
  - refresh more aggressively
  - intended for explicit passage shifts or major sermon transitions

### Guardrails

Already implemented in code:

- dedupe of repeated identical signals
- separate cooldown behavior for soft vs strong refreshes
- in-flight suppression
- pending strongest-request replay after task completion
- urgent allowlist for definitive passage changes

## Context Window Strategy

The best topic-tracker prompt window is layered rather than purely transcript
based.

### Layer 1: Prior Structured Memory

- prior short summary
- prior long summary
- active passage
- sermon arc
- rhetorical goal
- theme state
- illustration state

### Layer 2: Recent Local Evidence

- bounded recent final segments
- recent mode history
- recent refresh reasons

### Layer 3: Trigger Context

- current refresh reason
- any explicit passage-change signal
- recent rhetorical movement evidence

### Layer 4: Metadata

- current settled mode
- elapsed sermon phase
- segment count

This design is better than simply enlarging the raw transcript window.

## Model Strategy

The current code already supports dedicated topic-tracker model configuration.

### Existing Config Knobs

- `TOPIC_TRACKER_MODEL`
- `TOPIC_TRACKER_MAX_TOKENS`
- `TOPIC_TRACKER_PROMPT_CACHE_TTL`
- `TOPIC_TRACKER_MIN_REFRESH_GAP_SECS`
- `TOPIC_TRACKER_STRONG_REFRESH_GAP_SECS`
- `TOPIC_TRACKER_PERIODIC_FAST_SECS`
- `TOPIC_TRACKER_PERIODIC_SLOW_SECS`

### Recommended Use

- keep main sentence enrichment on the faster live model
- use topic tracking as the safer place to spend model-quality budget
- validate the stronger topic model with replay benchmarks before broad rollout

## Downstream Consumption Strategy

Different consumers should trust different parts of topic memory.

### Structural Enrichment

Best inputs:

- active passage
- current mode
- sermon arc
- rhetorical goal
- primary themes
- illustration state

Long-form summary text should remain secondary.

### Verse Suggestions

Best inputs:

- active passage
- primary themes
- current mode
- short summary

### Diagnostics UI

Should show:

- active passage
- current mode
- sermon arc
- rhetorical goal
- primary themes
- last refresh reason
- short summary
- refresh counters

### Future Reviewer Tools

Should show:

- long summary
- refresh history
- evidence segments

## Implementation Status By Phase

Status legend:

- `[x]` implemented
- `[~]` partially implemented / needs validation or follow-through
- `[ ]` not yet implemented

### Phase 0: Baseline And Instrumentation `[~]`

Implemented:

- refresh counters
- refresh reason counts
- cooldown suppression counters
- refresh latency aggregation
- parse failure counter

Remaining:

- make topic metrics more visible in diagnostics UI
- use replay benchmarks to validate the new refresh behavior

### Phase 1: Data Model Expansion `[x]`

Implemented:

- `ActivePassageState`
- `SermonStateMemory`
- `ThemeStateMemory`
- `IllustrationStateMemory`
- `SummaryStateMemory`
- `EvidenceMemory`
- `TopicTrackerMemory`
- backward-compatible string view helpers

### Phase 2: Enrichment-Driven Refresh Signals `[x]`

Implemented:

- `topic_refresh_signal` in structural enrichment schema
- parser/validator for refresh signal
- `TopicTracker.request_refresh(...)`
- enrichment-driven refresh requests into topic tracker

### Phase 3: Smarter Scheduler `[x]`

Implemented:

- periodic fallback scheduling
- signal cooldowns
- dedupe
- no concurrent refresh tasks
- pending strongest-request replay

### Phase 4: Context Assembly Rewrite `[x]`

Implemented:

- structured topic prompt blocks
- shaped prompt assembly
- bounded recent evidence windows

### Phase 5: Stronger Topic-Tracker Model `[x]`

Implemented in configuration:

- dedicated topic-tracker model selection
- dedicated token and cache knobs

Still needed:

- benchmark validation of model choice and cost profile

### Phase 6: Downstream Consumer Refactor `[~]`

Implemented:

- main enrichment now consumes structured topic prompt blocks

Still present:

- fallback `get_context()` compatibility path

Recommendation:

- keep the fallback path until replay and live validation are complete

### Phase 7: Diagnostics And Operator Visibility `[ ]`

Not yet implemented:

- dedicated live topic-memory panel in diagnostics
- first-class display of refresh reason and topic state

### Phase 8: Testing, Benchmarking, And Rollout `[~]`

Still needed:

- replay-benchmark comparison of old vs new topic-memory behavior
- verse-suggestion relevance review
- false scripture-inference review during illustration
- live-service validation

## Remaining Work

The architecture is now largely in place. The remaining work is about
validation, operator visibility, and cleanup.

### Priority 1: Validate The Existing Semantic-Memory Behavior

- run focused replay benchmarks
- compare refresh counts and trigger reasons
- review passage stability
- review verse-suggestion relevance
- review illustration-mode false positives

### Priority 2: Surface Topic Memory In Diagnostics

- add a live topic-state panel
- show:
  - active passage
  - current mode
  - sermon arc
  - rhetorical goal
  - themes
  - last refresh reason
  - refresh counters

### Priority 3: Review The Schema After Real Use

- confirm whether current memory fields are sufficient
- decide whether any field should move between hard vs soft context
- trim fields that are not actually helping prompts or operators

### Priority 4: Remove Transitional Shims Carefully

- retire flat string-only prompt dependence after confidence is high
- keep compatibility helpers until benchmarks and live tests support removal

## Recommended Next Slice

The best next slice is not another architectural rewrite. It is:

1. benchmark the implemented topic-memory behavior against replay clips
2. expose topic memory and refresh reasons in the diagnostics dashboard
3. evaluate the schema after live-service usage
4. only then decide whether to remove fallback shims or refine fields

That is the highest-leverage path because the system already contains most of
the semantic-memory architecture discussed in planning.
