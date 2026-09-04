# General-Purpose Repo And Module Layout Plan

Last updated: 2026-05-14

## Purpose

This document defines the target repository and module layout for the new
general-purpose live contextual interpretation system.

It is intended to solve three problems at once:

1. give agents a concrete physical structure to build into
2. enforce core-versus-skill separation at the repo level
3. make replay, testing, and benchmark assets first-class citizens

## Layout Principles

1. Contracts live above implementations.
   Shared types, schemas, and policies should not be buried inside one app.

2. Core runtime and domain skills are separate packages.
   Domain-specific logic should not be mixed into the core backend package.

3. Replay and benchmarks are production-grade support systems.
   They should have obvious homes in the repo.

4. Frontend experiences share contracts, not hidden assumptions.

5. Tooling and docs should reflect the same boundaries as the code.

## Recommended Top-Level Layout

```text
churchbridge-ai/
  apps/
  packages/
  skills/
  benchmarks/
  scripts/
  docs/
  infra/
  .github/
```

## Top-Level Directories

### `apps/`

Contains deployable applications or services.

Recommended contents:

```text
apps/
  runtime-api/
  web-console/
  canonicalizer-worker/
  replay-runner/
```

Responsibilities:

1. runtime-api
   live backend service for sessions, WebSockets, and coordinator-owned state

2. web-console
   admin, display, listener, and diagnostics UI

3. canonicalizer-worker
   post-session transcript materialization and archival jobs

4. replay-runner
   executable replay and benchmark service or CLI wrapper

### `packages/`

Contains shared libraries used by multiple apps.

Recommended contents:

```text
packages/
  contracts/
  core-runtime/
  state-coordinator/
  stt/
  buffering/
  translation/
  enrichment/
  persistence/
  replay/
  benchmark/
  ui-contracts/
  skill-sdk/
  observability/
```

### `skills/`

Contains first-party domain skills.

Recommended contents:

```text
skills/
  church-service/
  examples/
```

### `benchmarks/`

Contains shared replay corpora, evaluation harnesses, and benchmark metadata not
owned by a single skill.

### `scripts/`

Contains developer automation, codegen, import/export helpers, and operational
utilities.

### `docs/`

Contains planning, architecture, operations, and program-management documents.

### `infra/`

Contains deployment and local environment definitions.

## Package-Level Plan

## `packages/contracts/`

Purpose:
Own the shared runtime contracts used across backend, frontend, replay, and
skills.

Should contain:

1. session models
2. segment models
3. event schemas
4. transcript edition schemas
5. skill manifest schemas
6. API request and response models

Dependency rule:
No business-logic package may redefine contract shapes locally.

## `packages/core-runtime/`

Purpose:
Own the core session orchestration model without domain-specific enrichers.

Should contain:

1. session bootstrap
2. core policy interfaces
3. runtime wiring
4. session profile resolution

Dependency rule:
May depend on `contracts`, `state-coordinator`, and extension interfaces, but
not on first-party skills directly.

## `packages/state-coordinator/`

Purpose:
Own the centralized state coordinator and mutation lease logic.

Should contain:

1. in-memory session state store
2. segment lookup and chain lookup
3. mutation intent API
4. lease enforcement
5. transition validation
6. ordered event emission rules

Dependency rule:
This package should be dependency-light and highly tested.

## `packages/stt/`

Purpose:
Own provider adapters, STT normalization, and span-verification interfaces.

Should contain:

1. provider-agnostic adapter interfaces
2. Deepgram and Google implementations
3. confidence normalization
4. rolling audio span helpers
5. shadow-verification interface

## `packages/buffering/`

Purpose:
Own buffering and segmentation logic.

Should contain:

1. sentence buffer
2. incomplete-tail heuristics
3. abandoned-fragment rules
4. merge-eligibility scaffolding

## `packages/translation/`

Purpose:
Own fast translation and passthrough routing.

Should contain:

1. language routing
2. fast translation adapters
3. forward-correction helpers
4. translation metadata models

## `packages/enrichment/`

Purpose:
Own structural enrichment, semantic-anchor checks, deferred release, and
revision classification.

Should contain:

1. enrichment orchestration
2. semantic-anchor engine
3. dwell-time revision classifier
4. alignment request helpers
5. extension-point dispatch for skill enrichers

## `packages/persistence/`

Purpose:
Own storage models, transcript editions, and canonicalization helpers.

Should contain:

1. live storage adapters
2. transcript edition persistence
3. event persistence
4. canonical transcript materialization logic

## `packages/replay/`

Purpose:
Own replay data models and execution helpers.

Should contain:

1. capture file readers
2. event replay drivers
3. deterministic session simulators

## `packages/benchmark/`

Purpose:
Own benchmark execution and result comparison logic.

Should contain:

1. replay benchmarks
2. semantic-delta scoring
3. latency result aggregation
4. CI-friendly comparison tools

## `packages/ui-contracts/`

Purpose:
Own frontend-safe projections of shared contracts and display models.

Should contain:

1. feed state projections
2. presentation metadata types
3. UI event helpers

## `packages/skill-sdk/`

Purpose:
Own the developer-facing abstractions for skill manifests, resources, detectors,
enrichers, and UI hooks.

Should contain:

1. manifest validation helpers
2. extension-point interfaces
3. test harnesses for skills
4. resource loaders

## `packages/observability/`

Purpose:
Own pipeline trace helpers, metric names, and shared diagnostics utilities.

## App-Level Plan

## `apps/runtime-api/`

Purpose:
Run the live interpretation backend.

Should depend on:

1. contracts
2. core-runtime
3. state-coordinator
4. stt
5. buffering
6. translation
7. enrichment
8. persistence
9. skill-sdk
10. observability

Must not:

1. contain first-party church logic directly
2. redefine contract models

## `apps/web-console/`

Purpose:
Host the operator, display, listener, and diagnostics interfaces.

Should depend on:

1. ui-contracts
2. contracts
3. skill-safe presentation adapters

The web app may contain submodules or routes for:

1. operator console
2. display surface
3. listener surface
4. diagnostics surface

## `apps/canonicalizer-worker/`

Purpose:
Build canonical transcripts from evented or captured session state.

Should depend on:

1. contracts
2. persistence
3. replay
4. benchmark where needed for comparison tooling

## `apps/replay-runner/`

Purpose:
Run replay scenarios, benchmark suites, and regression comparisons.

## Skill Layout

Each skill should be self-contained and predictable.

Recommended structure:

```text
skills/
  church-service/
    skill.manifest.json
    resources/
    detectors/
    enrichers/
    ui/
    benchmarks/
    README.md
```

Every first-party skill should ship with:

1. manifest
2. resources
3. extension implementations
4. benchmark pack
5. short operator/developer README

## Testing Layout

Recommended shape:

```text
tests/
  contracts/
  runtime/
  replay/
  benchmarks/
  skills/
```

### `tests/contracts/`

1. schema validation
2. session protocol tests
3. skill manifest tests

### `tests/runtime/`

1. coordinator lease tests
2. segment state-machine tests
3. buffering and revision tests

### `tests/replay/`

1. deterministic replay tests
2. capture compatibility tests

### `tests/benchmarks/`

1. semantic-delta gates
2. latency regression checks

### `tests/skills/`

1. first-party church skill compatibility tests
2. skill extension-point compliance tests

## Documentation Layout

Recommended structure within `docs/`:

```text
docs/
  overview/
  operations/
  plans/
  skills/
```

Add `docs/skills/` for:

1. skill manifest guidance
2. skill authoring docs
3. first-party skill overviews

## Dependency Rules

These rules should be treated as architecture law.

1. apps may depend on packages, never the reverse
2. contracts may depend on nothing domain-specific
3. core-runtime may depend on contracts and extension interfaces, not concrete
   first-party skills
4. skills may depend on skill-sdk and contracts, not on app internals
5. UI code may consume only contract-safe projections
6. benchmark code must be able to run without the web app

## Ownership Rules

Recommended ownership:

1. `packages/contracts/`
   Architecture And Contracts Agent

2. `packages/state-coordinator/`
   Backend Platform Agent

3. `packages/stt/`
   Audio And STT Agent

4. `packages/buffering/`
   Buffering And Segmentation Agent

5. `packages/translation/` and `packages/enrichment/`
   Translation And Enrichment Agent

6. `packages/persistence/`
   Persistence And Canonicalization Agent

7. `packages/replay/` and `packages/benchmark/`
   Diagnostics, Replay, And Benchmark Agent

8. `skills/`
   Skill System And Domain Profiles Agent with domain-owner collaboration

9. `apps/web-console/`
   Frontend Live Experience Agent

## Migration Guidance

When rebuilding from the current repo:

1. start by defining packages and contracts before porting behavior
2. move church-specific logic into the church skill package early
3. keep replay assets near the skill and benchmark layers
4. avoid copying current mixed-layer file organization into the new repo

## Acceptance Criteria

The repo/module layout is ready when:

1. all major program agents have an obvious code home
2. the core-versus-skill boundary is visible in the directory structure
3. contracts, replay, and benchmark packages are first-class
4. first-party church skill files can live outside the core backend package
5. dependency rules are documented and enforceable

## Open Decisions

1. monorepo toolchain choice
2. exact Python package boundaries versus service boundaries
3. whether frontend lives as one app or multiple deployable surfaces
4. whether skills ship in-repo only or also as external packages later
