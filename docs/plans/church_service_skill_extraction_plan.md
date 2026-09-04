# Church-Service Skill Extraction Plan

Last updated: 2026-05-14

## Purpose

This document defines how the current church-service-specific behavior will be
extracted from the existing ChurchBridge AI implementation and repackaged as a
first-party skill for the new general-purpose core runtime.

The goal is to preserve what we have learned from live sermon interpretation
without hardcoding church logic into the new platform.

## Why This Matters

The current system is our deepest source of real-world learning:

1. bilingual sermon audio is noisy, code-switched, and structurally irregular
2. religious references are high-impact truth anchors
3. speaker intent often matters more than sentence punctuation
4. users tolerate fast draft text only when the system is honest about
   provisional versus settled output

Those learnings are valuable beyond church services, but the church-specific
logic itself should become a packaged skill, not a permanent assumption in the
core runtime.

## Extraction Goals

The church-service skill should capture:

1. church terminology and glossary behavior
2. scripture reference detection
3. verse suggestion behavior
4. sermon-mode and liturgical-mode semantics
5. church-specific topic-memory prompt blocks
6. church-specific evaluation and replay packs
7. church-oriented display and metadata conventions

The extraction should not move core responsibilities out of the runtime.

## Source Material

The extraction effort should use these as the main references:

1. [README.md](C:/Users/Dan/Desktop/Projects/churchbridge-ai/README.md)
2. [data-flow.md](C:/Users/Dan/Desktop/Projects/churchbridge-ai/docs/overview/data-flow.md)
3. [topic-tracker-semantic-memory-plan.md](C:/Users/Dan/Desktop/Projects/churchbridge-ai/docs/plans/topic-tracker-semantic-memory-plan.md)
4. [caption_chain_lifecycle_implementation.md](C:/Users/Dan/Desktop/Projects/churchbridge-ai/docs/caption_chain_lifecycle_implementation.md)
5. current implementation files in `server/services/`, `server/db/`, and the
   relevant client display components

## Church Skill Scope

The church-service skill should own the following categories.

### 1. Terminology And Glossary Resources

Includes:

1. church glossary terms
2. church translation overrides
3. common theological vocabulary mappings
4. high-risk phonetic collision lists relevant to sermons

Examples:

1. Pentecost-related terms
2. denomination-specific vocabulary
3. scripture book names and abbreviations

### 2. Reference Detection

Includes:

1. scripture reference detection
2. scripture range updates
3. scripture quote heuristics
4. chapter-only and verse-specific patterns

The core runtime should expose the detector hooks. The church skill provides the
church-specific reference logic and resources.

### 3. Suggestion Systems

Includes:

1. verse suggestion logic
2. church-specific suggestion prompt blocks
3. suppression rules tied to church discourse modes

### 4. Domain Modes

Includes:

1. sermon-mode categories
2. liturgical or service-flow modes
3. mode-label display conventions

Examples of church-owned domain modes:

1. scripture
2. exposition
3. illustration
4. application
5. exhortation
6. procedural

The core runtime should treat these as skill-defined mode values, not as
universally hardcoded semantics.

### 5. Topic-Memory Contributions

Includes:

1. church-specific prompt blocks
2. active-passage semantics
3. church-specific refresh reasons if needed
4. church-specific summary shaping

### 6. UI Conventions

Includes:

1. reference cards
2. church-specific labels and badges
3. scripture-specific display affordances
4. church-oriented default presentation settings

## What Stays In The Core

The following stay in the general-purpose runtime:

1. session lifecycle
2. segment lifecycle
3. event contracts
4. state coordinator and leases
5. semantic-anchor enforcement framework
6. dwell-time revision framework
7. replay and canonicalization framework
8. shadow-verification orchestration
9. audio ingest and STT adapter framework
10. general topic-memory and enrichment hooks

## Extraction Matrix

### Resource And Behavior Mapping

Church-specific responsibility:

1. `church_glossary`
   Destination: church skill glossary resources

2. `church_terms`
   Destination: church skill terminology maps

3. verse detection logic
   Destination: church skill detector package

4. verse suggestions
   Destination: church skill enricher package

5. sermon-mode semantics
   Destination: church skill mode definition package

6. church topic blocks and evaluation packs
   Destination: church skill prompt and benchmark resources

Core responsibility:

1. live event transport
2. pending commit handling
3. revision classification
4. merge coordination
5. transcript editions
6. observability and replay

## Proposed Skill Package Shape

Recommended package layout:

```text
skills/
  church-service/
    skill.manifest.json
    resources/
      glossaries/
        default.json
      terminology/
        theological_terms.json
      references/
        bible_books.json
      prompts/
        topic_blocks.md
    detectors/
      scripture_reference_detector.*
      church_mode_detector.*
    enrichers/
      verse_suggestion_enricher.*
    ui/
      labels.json
      display_defaults.json
    benchmarks/
      replay_pack.json
      ground_truth.json
```

## Migration Workstreams

### Workstream 1: Behavioral Inventory

Goal:
Identify every current church-specific behavior and classify it as:

1. core runtime
2. skill resource
3. skill detector
4. skill enricher
5. skill UI hook
6. church-only benchmark case

Deliverables:

1. inventory spreadsheet or structured checklist
2. final extraction mapping

### Workstream 2: Resource Extraction

Goal:
Move church-owned static and semi-static resources into skill-owned files.

Includes:

1. glossaries
2. terminology maps
3. reference corpora
4. prompt blocks
5. UI label defaults

Deliverables:

1. manifest resource declarations
2. packaged church resources

### Workstream 3: Detector And Enricher Extraction

Goal:
Move church-specific logic behind bounded core extension points.

Includes:

1. scripture reference detector
2. verse suggestion enricher
3. church mode detector
4. church topic-memory provider

Deliverables:

1. skill-owned detector interfaces
2. skill-owned enricher interfaces
3. compatibility tests with the core extension points

### Workstream 4: Replay And Regression Pack Construction

Goal:
Preserve high-value church-service learnings as benchmark assets.

Includes:

1. merge-heavy sermon segments
2. code-switched preaching
3. scripture quotation cases
4. chapter and verse references
5. false-start and unfinished-thought cases
6. high-impact negation and number cases

Deliverables:

1. church replay pack
2. church ground-truth pack
3. church truth-policy benchmark scenarios

### Workstream 5: UI Conventions Extraction

Goal:
Move church-specific display conventions into skill-owned configuration or UI
hook contracts.

Includes:

1. scripture card rendering defaults
2. church-specific labels
3. default church display presentation profile

## Phased Extraction Plan

## Phase 0: Inventory And Boundaries

Objective:
Produce a complete mapping of current church behaviors to core versus skill.

Deliverables:

1. extraction matrix
2. manifest draft
3. benchmark-case inventory

Exit criteria:

1. no known church-specific behavior is left unclassified

## Phase 1: Resource Packaging

Objective:
Package static and semi-static church resources.

Deliverables:

1. glossary files
2. terminology files
3. reference resource files
4. prompt resource files

Exit criteria:

1. core runtime can load church resources from the skill package

## Phase 2: Detector And Enricher Hook-Up

Objective:
Wire church logic into core extension points.

Deliverables:

1. scripture reference detector
2. church mode detector
3. verse suggestion enricher
4. church topic-memory block provider

Exit criteria:

1. the skill can reproduce key church-specific behaviors without hardcoded
   exceptions in the core

## Phase 3: Replay And Truth Regression

Objective:
Bind church replay and ground-truth cases to the benchmark framework.

Deliverables:

1. replay pack
2. ground-truth pack
3. CI scenarios for church truth regression

Exit criteria:

1. the extracted church skill can be benchmarked independently

## Phase 4: UX And Operator Validation

Objective:
Verify that church-specific UX conventions still feel correct when driven by a
skill instead of hardcoded runtime logic.

Deliverables:

1. church admin profile
2. church display profile
3. church listener profile

Exit criteria:

1. operator and display behavior remain usable and recognizable

## Risks

### Risk 1: Hidden Church Assumptions Remain In The Core

Mitigation:
Use the extraction matrix as a blocking review artifact before Phase 2 begins.

### Risk 2: Skill Hooks Are Too Weak

Mitigation:
Let the church skill be the proving ground. If the church skill cannot express
critical church behavior without core hacks, the extension point design is not
finished.

### Risk 3: Skill Hooks Are Too Powerful

Mitigation:
Do not allow church-specific code to bypass segment truth coordination or
semantic-anchor enforcement.

### Risk 4: Benchmark Quality Regresses During Extraction

Mitigation:
Church replay and ground-truth packs must be established before large behavior
moves are declared complete.

## Acceptance Criteria

The church-service skill extraction is complete when:

1. the church-service skill can be activated through the new skill manifest
2. core runtime code no longer contains avoidable church-only policy logic
3. church-specific resources load from the skill package
4. church-specific behaviors are exercised through bounded skill hooks
5. church replay and ground-truth packs pass in CI
6. the church skill remains a first-class example for future domain skills

## Open Decisions

1. whether Bible corpus assets remain in the main repo or move into the skill
2. whether the church skill owns display defaults directly or through a session
   profile bundle
3. which current diagnostics panels remain core and which become skill-aware
4. whether sermon-mode values stay church-owned strings or move to a richer
   mode schema
