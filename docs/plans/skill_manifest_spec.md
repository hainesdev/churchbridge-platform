# Skill Manifest Specification

Last updated: 2026-05-14

## Purpose

This document defines the manifest model for domain skills in the new
general-purpose live contextual interpretation system.

The core runtime must remain domain-agnostic. Skills are the mechanism that
attach domain-specific knowledge, policy extensions, enrichers, detectors,
presentation hooks, and regression packs to a session without rewriting the
core runtime.

This spec is intended to be stable enough that multiple agents can implement
against it in parallel.

## Why This Exists

The previous system mixed two kinds of logic:

1. universal live-interpretation responsibilities
2. church-service-specific behavior

That mixture made it harder to generalize the platform without dragging church
assumptions through every layer of the stack.

The new skill manifest model is the separation line:

1. the core owns session state, truth policy, events, replay, and persistence
2. skills contribute domain resources and bounded extensions

## Non-Goals

The skill manifest is not:

1. a free-form plugin API with arbitrary runtime mutation power
2. permission to bypass core truth policy
3. a replacement for versioned contracts
4. an invitation to hot-swap unrelated domain logic mid-segment without guard

## Design Principles

1. Core-first safety.
   A skill may extend behavior only where the core exposes a contract.

2. Declarative where possible.
   Skills should declare resources, policies, and capabilities before they
   register imperative hooks.

3. Bounded effect surface.
   The runtime should know exactly which parts of behavior a skill may affect.

4. Replayable behavior.
   Skill-driven outcomes must be reproducible during replay and benchmark runs.

5. Versioned compatibility.
   Every skill declares compatibility with runtime and contract versions.

## Manifest Responsibilities

The manifest must be able to describe:

1. skill identity and versioning
2. runtime compatibility
3. domain description
4. required and optional resources
5. policy extensions
6. detectors and enrichers
7. UI presentation hooks
8. regression and benchmark packs
9. observability labels

## Core Versus Skill Boundary

### Core Runtime Owns

1. session lifecycle
2. segment lifecycle and legal transitions
3. state coordinator and mutation leases
4. event schema enforcement
5. semantic-anchor enforcement framework
6. dwell-time policy enforcement framework
7. replay, persistence, and canonicalization
8. shadow-verification orchestration

### Skills Own

1. domain terminology
2. domain-specific enrichers and detectors
3. domain-specific heuristic extensions
4. domain-specific UI conventions
5. domain-specific regression packs
6. domain-specific resource bundles

### Skills May Extend But Not Override

1. semantic-anchor categories
2. shadow-verification trigger allowlists
3. presentation metadata
4. topic-memory prompt blocks

### Skills Must Never Override

1. legal segment transitions
2. coordinator lease rules
3. transcript-edition semantics
4. event ordering guarantees
5. core trust-policy enforcement

## Manifest Format

The skill manifest should be serialized as JSON or YAML at rest. The canonical
runtime model should be a validated typed object.

Recommended top-level shape:

```json
{
  "manifest_version": "1.0",
  "skill": {
    "id": "church-service",
    "name": "Church Service",
    "version": "0.1.0",
    "domain": "church",
    "summary": "Live sermon interpretation, scripture references, and liturgical mode support."
  },
  "compatibility": {
    "runtime_api": "1.x",
    "contracts_api": "1.x",
    "min_runtime_version": "0.1.0"
  },
  "session_profile": {
    "default_source_languages": ["es-US", "en-US"],
    "default_target_language": "en-US",
    "default_display_mode": "live-caption",
    "supports_translation": true,
    "supports_passthrough": true
  },
  "resources": {
    "glossaries": [
      {
        "id": "church-glossary-default",
        "type": "term_map",
        "required": true,
        "path": "resources/glossaries/default.json"
      }
    ],
    "reference_corpora": [
      {
        "id": "bible-index",
        "type": "structured_reference_corpus",
        "required": false,
        "path": "resources/reference/bible_index.json"
      }
    ]
  },
  "policies": {
    "semantic_anchor_extensions": [
      "reference_identifier",
      "scripture_quote_boundary"
    ],
    "shadow_verification": {
      "priority_entities": ["verse_reference", "chapter_number"],
      "heuristic_pack": "church_reference_triage"
    },
    "presentation": {
      "show_source_line": true,
      "show_reference_cards": true,
      "provisional_style": "dim_italic"
    }
  },
  "capabilities": {
    "detectors": [
      "scripture_reference_detector",
      "sermon_mode_detector"
    ],
    "enrichers": [
      "verse_suggestion_enricher",
      "topic_memory_prompt_block_provider"
    ],
    "ui_hooks": [
      "reference_card_renderer",
      "church_mode_badge"
    ]
  },
  "benchmarking": {
    "replay_pack": "benchmarks/church_service_replay_pack.json",
    "ground_truth_pack": "benchmarks/church_service_ground_truth.json"
  },
  "observability": {
    "labels": {
      "domain": "church",
      "skill_family": "first_party"
    }
  }
}
```

## Field Specification

### `manifest_version`

Purpose:
Version of the manifest schema itself.

Rules:

1. required
2. must be validated before any other field is trusted

### `skill`

Purpose:
Identity block for the skill.

Required fields:

1. `id`
2. `name`
3. `version`
4. `domain`

Rules:

1. `id` must be globally unique within the runtime
2. `version` should follow semantic versioning
3. `domain` is descriptive, not an authorization scope

### `compatibility`

Purpose:
Declares runtime compatibility.

Required fields:

1. `runtime_api`
2. `contracts_api`

Rules:

1. runtime must reject incompatible skills at load time
2. compatibility must be checked during CI for first-party skills

### `session_profile`

Purpose:
Declares default session behavior that the operator may select or override.

Supported fields should include:

1. source language defaults
2. target language defaults
3. presentation defaults
4. whether translation is required
5. whether passthrough is allowed

This block provides defaults only. It does not override core session
configuration or operator choice.

### `resources`

Purpose:
Declares external or packaged resources required by the skill.

Resource categories may include:

1. glossaries
2. terminology maps
3. structured corpora
4. example packs
5. UI assets
6. evaluation packs

Each resource should declare:

1. `id`
2. `type`
3. `required`
4. `path` or `uri`

### `policies`

Purpose:
Declares skill-provided policy extensions recognized by the runtime.

Supported policy families should include:

1. semantic-anchor extensions
2. shadow-verification heuristic references
3. presentation defaults
4. optional domain mode labels

Policy declarations must be explicit. Skills must not smuggle policy through
arbitrary prompt text.

### `capabilities`

Purpose:
Declares which bounded extension points the skill implements.

Capability families may include:

1. detectors
2. enrichers
3. UI hooks
4. prompt-block providers
5. heuristic providers

Each capability must map to a runtime extension point owned by the core.

### `benchmarking`

Purpose:
Declares replay and regression packs associated with the skill.

Required for first-party skills:

1. replay pack
2. ground-truth pack

The runtime does not need these to serve live traffic, but CI and benchmark
systems do.

### `observability`

Purpose:
Attaches labels that make cross-skill behavior visible in diagnostics and
benchmark outputs.

## Activation Model

### Session Boot

At session creation time, the runtime should resolve:

1. selected session profile
2. primary skill
3. zero or more auxiliary skills
4. operator-provided overrides

The resulting resolved session configuration should be immutable for the life of
the session except through explicit, coordinator-approved reconfiguration.

### Resolution Order

Recommended order:

1. runtime defaults
2. session profile defaults
3. primary skill defaults
4. auxiliary skill additions
5. operator overrides
6. runtime validation

### Activation Events

The runtime should emit events for:

1. selected session profile
2. skill activation
3. skill resource load success or failure
4. incompatible skill rejection

## Extension Points

The core runtime should expose a bounded set of extension points.

### Detector Extension Point

Purpose:
Allow a skill to attach domain-specific detectors to settled segments or
segment windows.

Examples:

1. scripture reference detection
2. legal citation detection
3. dosage or medication pattern detection

Constraints:

1. detectors cannot mutate core state directly
2. detectors emit structured findings that the coordinator routes

### Enricher Extension Point

Purpose:
Allow a skill to attach bounded enrichment flows that consume settled segment
state and emit structured outputs.

Examples:

1. verse suggestions
2. domain mode classification
3. domain summary cards

Constraints:

1. enrichers must produce typed outputs
2. enrichers may not bypass semantic-anchor enforcement for live revisions

### Prompt Block Extension Point

Purpose:
Allow a skill to contribute structured context blocks to an LLM prompt in a
controlled way.

Constraints:

1. prompt blocks must be attributed to the skill
2. prompt blocks must be replayable and inspectable

### UI Hook Extension Point

Purpose:
Allow a skill to influence how approved metadata is presented.

Examples:

1. reference cards
2. domain badges
3. domain-specific labels

Constraints:

1. UI hooks can only render metadata emitted through core contracts
2. UI hooks cannot invent hidden state

## Validation Rules

The runtime should reject a manifest if:

1. required top-level fields are missing
2. compatibility rules fail
3. a capability references an unknown extension point
4. a required resource is missing
5. policy keys are unknown or malformed
6. the benchmarking block is missing for a required first-party skill

## Skill Packaging Conventions

Recommended structure for a first-party skill:

```text
skills/
  church-service/
    skill.manifest.json
    resources/
      glossaries/
      reference/
      prompts/
    benchmarks/
      replay_pack.json
      ground_truth.json
    ui/
      labels.json
```

The exact layout may change with the final repo design, but the packaging
convention should remain predictable and scriptable.

## First-Party Skill Requirements

A first-party skill must include:

1. validated manifest
2. explicit owner
3. replay pack
4. ground-truth pack
5. compatibility test coverage
6. operator-facing summary documentation

## Church-Service Skill As Reference Skill

The first first-party skill should be the extracted church-service skill.

It should demonstrate:

1. glossary resources
2. reference detection
3. domain mode tracking
4. domain-specific suggestions
5. domain-specific replay packs

It should not require privileged runtime behavior that other skills cannot use.

## Acceptance Criteria

The skill manifest system is ready when:

1. the runtime can load a valid skill manifest and reject an invalid one
2. session startup can resolve a profile plus skill set deterministically
3. skill activation is visible in events and diagnostics
4. the church-service skill can be expressed using manifest-defined extension
   points without hardcoded exceptions in the core
5. benchmark tooling can run skill-specific replay packs

## Open Decisions

These decisions should be resolved early by the Architecture And Contracts
Agent and the Skill System And Domain Profiles Agent:

1. JSON versus YAML at rest
2. whether auxiliary skills can contribute detectors to the same segment class
3. how to represent skill-owned UI hooks in shared contracts
4. whether prompt blocks are declared statically, dynamically, or both
5. whether first-party skills live in the same monorepo or in separately
   versioned packages
