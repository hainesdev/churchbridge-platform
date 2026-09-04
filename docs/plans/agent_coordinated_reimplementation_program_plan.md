# Agent-Coordinated General-Purpose Reimplementation Program Plan

Last updated: 2026-05-15

## Purpose

This document is the coordination plan for reimplementing ChurchBridge AI into
a more general-purpose live contextual interpretation system using a system of
collaborating agents.

The goal is not to produce a line-by-line port of the current codebase. The
goal is to rebuild the product from the behavior we now understand, preserve
what is working, and incorporate the improvements and operational lessons gained
from the current system while moving domain-specific logic out of the core and
into skills.

This plan is written as a project-management and execution document. It is
intended to coordinate multiple agents working in parallel without allowing them
to invent incompatible architectures, duplicate work, or quietly regress the
core low-latency interpretation experience.

## Executive Summary

The new system must preserve the product's core value:

1. accept live audio in configurable session contexts
2. tolerate multilingual and code-switched speech
3. delay unsafe fragments until they are structurally meaningful
4. provide fast output without pretending every first draft is final
5. repair segmentation and translation issues conservatively
6. serve operator, display, listener, and diagnostics consumers
7. retain enough state to support review, replay, and post-session study
8. load domain skills that contribute context, policy, enrichers, and user
   experience behavior without changing the core runtime

The new system must also address known debt in the current implementation:

1. semantic-hijack risk during LLM repair
2. late live revisions that can overburden readers
3. transcript persistence that stores the first stable draft instead of the
   final canonical interpretation
4. lack of a first-class abandoned-fragment state
5. lack of a speaker-facing or operator-facing understanding-health signal
6. excessive coupling between implementation detail and product trust policy

The program should therefore be treated as a behavior-preserving rebuild with a
general-purpose core, a stronger truth contract, better archival integrity,
better observability, and cleaner coordination boundaries between runtime and
skill logic.

## Program Outcome

At the end of this program, we should have:

1. a cleanly organized codebase with explicit module boundaries
2. a contract-first event and state model
3. a low-latency live interpretation path
4. a conservative repair path with semantic-anchor guardrails
5. a canonical transcript materialization path
6. a replay-first validation workflow
7. a skill system that can package domain-specific behavior cleanly
8. a first-party church-service skill built from prior learnings
9. a confidence and health layer visible to operators
10. documentation, tests, and benchmarks strong enough that new agents can work
   safely on the system after the rebuild

## Reimplementation Stance

This rebuild should follow five rules:

1. Preserve behavior before changing behavior.
   Recreate the current proven user-facing capabilities first. Improvements must
   be explicit, measured, and gated.

2. Do not copy implementation debt just because it exists.
   If the old code solved a real problem but did so awkwardly, preserve the
   requirement and redesign the mechanism.

3. Treat trust as a first-class product requirement.
   Caption correctness, clarity of provisional versus settled text, and
   archival fidelity are not "nice to have" polish.

4. Use contracts to coordinate agents, not intuition.
   Shared models, event schemas, state transitions, and acceptance tests must
   be specified before broad parallel implementation begins.

5. Make replay and benchmarking part of the main build, not post hoc cleanup.
   The old system taught us that live behavior is too nuanced to trust without
   capture, replay, and diagnostics.

6. Move domain specificity into skills.
   The runtime should understand sessions, segments, events, policy hooks, and
   verification flows. Domain-specific heuristics, glossaries, reference logic,
   and presentation semantics should live in skills whenever possible.

## Source Material For The Rebuild

All agents should orient themselves using these existing documents before
implementing behavior:

1. high-level product overview:
   [`README.md`](../../README.md)
2. implementation-level runtime data flow:
   [`docs/overview/data-flow.md`](../overview/data-flow.md)
3. caption merge and lifecycle details:
   [`docs/caption_chain_lifecycle_implementation.md`](../caption_chain_lifecycle_implementation.md)
4. topic-tracker design and status:
   [`docs/plans/topic-tracker-semantic-memory-plan.md`](./topic-tracker-semantic-memory-plan.md)
5. bilingual display and pair-generation direction:
   [`docs/bilingual_display_and_pair_generation_plan.md`](../bilingual_display_and_pair_generation_plan.md)

Agents may also inspect the current implementation for behavioral reference, but
the rebuild must treat those files as evidence, not as architecture law.

The current church-service implementation should be treated as:

1. the reference product whose live behavior we understand best
2. the seed corpus for replay and regression testing
3. the source material for a first-party church-service skill
4. one domain among many that the new core should eventually support

## Current Proven Design Baseline

Before the rebuild abstracts anything, it should preserve the specific live
contract that the church-service implementation has now proven in code.

### WebSocket Topology

1. The operator browser opens `/api/stream/v1` and sends a `session.start`
   control message followed by repeated `audio` payloads.
2. The backend creates exactly one active `ServiceSession` per `church_id`.
3. That session owns STT callbacks, sentence buffering, fast translation, LLM
   enrichment, merge decisions, phrase-alignment requests, verse hydration, and
   broadcast emission.
4. The broadcaster publishes church-scoped JSON events over Redis when
   available and over in-process callbacks in development.
5. `/api/display/v1` subscribes to the full church event stream.
6. `/api/listen/v1` subscribes to the same church stream but forwards only the
   lightweight translation-facing subset.

### Proven Display Event Contract

The rebuild should treat these semantics as first-class behavior, not as
accidental implementation detail:

1. `live_translation` is draft text for active reading while the sentence is
   still settling.
2. `live_translation_clear` clears draft text when a segment becomes committed.
3. `feed_commit` creates or finalizes a stable on-screen segment.
4. `feed_revision` silently rewrites a committed segment without creating a new
   segment identity.
5. `caption_merge` is a segmentation-repair event that removes an absorbed tail
   segment and rewrites the head-anchored segment in place.
6. `segment_metadata` carries display-facing scaffolding such as
   `pending_completion`, `source_quality`, `translation_register`, and
   paragraph-break hints.
7. `verse_detected`, `verse_range_update`, and `verse_suggestion` are
   post-translation enrichments attached to visible segments.
8. `mode_change` and `pipeline_trace` are part of the live diagnostics story,
   not separate offline-only concepts.

### Proven Server-Side Emission Policy

1. `display_ready` is the authoritative emission gate for whether an LLM
   translation improvement can ship immediately.
2. Unsafe or incomplete sentences are suppressed and moved into deferred
   release rather than being treated as normal revisions.
3. Head-anchored merge chains are the repair model: later fragments are folded
   into the earliest visible segment rather than replacing the entire display
   position.
4. Phrase alignment is requested after stable commit and returned as a
   follow-up enhancement revision.
5. Chunk-lineage payloads are part of the stable contract:
   `alignment_version`, `previous_alignment_version`, `root_segment_id`,
   `merged_from_segment_ids`, per-chunk `chunk_id`, spans, and
   `derived_from_chunk_ids`.
6. Diagnostics combine the raw display websocket event stream with polled
   session stats instead of trying to infer health from the UI alone.

### Proven Client Responsibilities

1. The sanctuary display is a state machine driven by websocket events, not a
   plain append-only transcript.
2. It reconstructs committed segments, live draft English, merge lineage, verse
   attachments, and chunk-level bilingual interactions from the event stream.
3. The display preserves hover and tap-lock state through safe lineage remaps
   across phrase-alignment revisions.
4. The mobile listener is intentionally simpler: it renders only the
   translation-facing subset and does not own the full bilingual or diagnostics
   state model.

### Known Gaps In The Proven Design

These are important lessons for the rebuild because they identify where the
current system is good enough to learn from but not ideal to preserve as-is:

1. New display or listener subscribers do not receive a state snapshot or
   replay window; they resume from future events only.
2. The mobile listener path is deliberately lighter than the display path and
   therefore is not yet fully merge-aware.
3. An ingest reconnect currently creates a fresh `ServiceSession`, which resets
   in-memory buffer, discourse, chain, and topic state for that church.
4. The diagnostics raw event log is retained in browser memory without a hard
   bound during long sessions.

## What Must Be Preserved From The Current System

The rebuild must preserve these product behaviors as general capabilities unless
leadership explicitly changes scope:

1. browser or operator-source ingest over a live session protocol
2. session-scoped live runtime isolation
3. multilingual STT handling with configurable passthrough and translation
   routing
4. sentence buffering that is aware of structural incompleteness
5. fast-path output preview before full enrichment settles
6. deferred release when a segment is not yet safe to show
7. head-anchored segmentation repair
8. optional domain enrichments such as reference detection, suggestion systems,
   or mode tracking
9. topic-memory support off the latency-critical path
10. live display websocket
11. mobile or lightweight listener websocket
12. diagnostics and replay artifacts
13. configurable domain skills that can specialize the core runtime
14. distinct live draft, stable commit, revision, and segmentation-repair event
    semantics for downstream clients
15. post-commit phrase alignment and chunk-lineage metadata for revision-aware
    bilingual display behavior

## Improvements That Must Be Designed In From Day One

These are not stretch goals. They are part of the target system.

### 1. Truth Policy And Semantic Anchor Gate

The new system must explicitly protect against semantic hijack.

Before an LLM-originated repair is accepted for live use or archival use, the
system must check whether it changes high-risk meaning dimensions such as:

1. negation
2. numbers and ordinals
3. reference identifiers such as Bible verses, legal citations, or protocol
   numbers
4. named entities
5. question versus statement intent
6. causal or conditional structure such as "if," "because," and "therefore"

If a candidate changes one of those dimensions, the system must not treat it as
an ordinary improvement. It must either:

1. reject the candidate
2. downgrade to a more literal baseline
3. send the segment into explicit adjudication

### 2. Dwell-Time Revision Policy

The new system must distinguish between:

1. text that is still safely revisable
2. text that a user has likely already read

Late revisions are sometimes necessary, but they must be governed by a server-
side policy that weighs:

1. age on screen
2. severity of error
3. whether the change is cosmetic, structural, or semantic

### 3. Abandoned Fragment State

The new system must support a first-class false-start or abandoned-fragment
state. Not every incomplete phrase should be merged or deferred indefinitely.

### 4. Canonical Transcript Model

The new system must preserve:

1. what was first shown live
2. what was later revised live
3. what the final canonical post-session transcript should be

The old persistence behavior stored only the first committed draft. The rebuild
must support canonicalization.

### 5. Shadow Verification For High-Risk Spans

The rebuild should support sparse secondary verification of risky finalized
spans, especially for:

1. low-confidence STT
2. numbers and ordinals
3. reference identifiers
4. names
5. mixed-language spans
6. segments about to trigger meaningful revision or merge

This should be sparse and bounded, not a second always-on live transcription
path.

Shadow verification must not trigger from model uncertainty alone. It should be
gated by a heuristic triage layer that prefers cheap deterministic checks before
expensive secondary STT.

At minimum, the triage layer should inspect:

1. confidence thresholds
2. number and ordinal patterns
3. reference-like structures defined by the active skill
4. named-entity candidates
5. suspicious punctuation or tokenization mismatches
6. known phonetic collision classes that matter in the active skill or session
   profile

Default policy:

1. no secondary verification when confidence is healthy and heuristics are calm
2. secondary verification when low confidence and heuristic suspicion agree
3. forced secondary verification for a small allowlist of mission-critical
   cases such as high-impact numbers, legal citations, medical dosage terms, or
   domain-defined reference entities

### 6. Confidence Beacon

The system must expose operator-facing health indicators that summarize how well
the live pipeline thinks it is understanding the audio.

### 7. Replay-First Validation

The rebuild must ship with capture, replay, and benchmark support so we can
compare:

1. old system versus new system
2. model choices
3. policy changes
4. transcript quality
5. live-latency tradeoffs

Replay-first validation must include automated regression-delta tracking against
an approved ground-truth corpus. Truth policy should be enforced in CI, not
only in manual review.

### 8. State Coordination And Mutation Leases

The rebuild must avoid race conditions between buffering, enrichment,
persistence, and broadcast behavior.

The system should therefore include a centralized state coordinator in the live
backend. All segment mutations that affect visible or archival truth must pass
through that coordinator.

The coordinator should enforce short-lived mutation leases on segments or
segment chains so that:

1. only one mutating workflow owns a segment transition at a time
2. enrichment cannot revise a segment while another workflow is finalizing a
   conflicting mutation
3. persistence does not publish or materialize a state that the live runtime
   has not acknowledged
4. broadcaster output always reflects one coherent ordered segment state

The coordinator is not a second business-logic engine. It is the single source
of transition authority.

## Recommended Target Topology

The rebuild may use a different repo layout than the current system, but the
following boundary model should be preserved:

1. realtime backend service
2. web application for admin, display, listener, and diagnostics
3. shared contracts package
4. replay and benchmark package
5. post-session canonicalization worker
6. centralized live state coordinator inside the backend runtime
7. skill manifest and domain skill package set

Unless leadership explicitly chooses a different stack, the default assumption
for Phase 1 should be:

1. Python backend
2. FastAPI or equivalent async HTTP/WebSocket service
3. web frontend compatible with current surfaces
4. SQLite in development, with a schema that can later support stronger storage
5. Redis as optional pub/sub, not a hard dependency for local development

The rebuild should avoid changing both product behavior and infrastructure stack
at the same time unless there is a demonstrated need.

## Scope Boundary: General-Purpose Core With Domain Skills

The rebuilt system should be general-purpose at the core and domain-specialized
through skills.

The church-service implementation is no longer the permanent center of scope.
It becomes:

1. a first-party domain skill
2. a high-value replay corpus
3. a strong source of truth-policy and UX learnings

What should be designed into the core now:

1. a clean configuration surface for session context
2. a skill manifest format for domain resources, policies, enrichers, and
   presentation hooks
3. explicit separation between hard contracts and skill-provided behavior
4. bounded support for skill-selected enrichers, detectors, and heuristics
5. skill-aware benchmarking and regression packs

What should live in skills rather than in the core:

1. church glossary and terminology rules
2. verse detection and citation handling
3. sermon-mode and liturgical-mode policies
4. church-specific reference suggestions
5. domain-specific phonetic collision lists
6. domain-specific tone and display conventions

What should remain out of scope unless leadership expands the mission further:

1. uncontrolled autonomous hot-swapping of unrelated skills mid-segment
2. arbitrary domain discovery without an explicit session profile
3. replacing hard runtime truth policy with skill-local prompt behavior

## Canonical Domain Model

The new system should revolve around explicit internal concepts rather than
loosely coupled ad hoc payloads.

### Session

A live contextual interpretation run with:

1. source audio stream
2. configuration
3. active session state
4. capture and replay artifacts
5. selected session profile
6. active skill manifest

### Segment

A segment is the canonical unit of live interpretation.

Every segment should carry:

1. stable `segment_id`
2. session id
3. source text
4. chosen rendered text
5. timing bounds
6. STT metadata
7. quality metadata
8. revision lineage
9. display state
10. archival state
11. source and target language metadata
12. skill or policy provenance where relevant

### State Coordinator And Segment Lease Model

All segment mutations should flow through a centralized coordinator that owns
authoritative in-memory state for the live session.

The coordinator should provide:

1. segment lookup
2. chain lookup
3. mutation intents
4. short-lived segment or chain leases
5. transition validation
6. ordered event emission
7. acknowledgements to persistence and canonicalization workers

Leases should be used for mutations such as:

1. pending commit finalization
2. segmentation repair
3. semantic-anchor adjudication
4. deferred release resolution
5. post-commit revision classification

Persistence should not independently mutate live truth. It should persist
acknowledged transitions from the coordinator.

### Segment Lifecycle States

At minimum, the new model should distinguish:

1. `interim_preview`
2. `final_fragment`
3. `buffered_sentence`
4. `pending_commit`
5. `committed_live`
6. `revised_live`
7. `merged_absorbed`
8. `abandoned_false_start`
9. `canonicalized`

### Legal Transition Rules

The lifecycle above should be implemented as an explicit state machine with
transition guards. At minimum, the following transitions should be supported:

1. `interim_preview` -> `final_fragment`
   Trigger: primary STT finalized output arrives

2. `final_fragment` -> `buffered_sentence`
   Trigger: sentence buffer accepts a thought unit

3. `final_fragment` -> `abandoned_false_start`
   Trigger: timeout or semantic pivot policy concludes the fragment was dropped

4. `buffered_sentence` -> `pending_commit`
   Trigger: fast translation path completes and segment enters live settlement

5. `pending_commit` -> `committed_live`
   Trigger: coordinator confirms that dwell, verification, and blocking
   mutation checks are clear

6. `committed_live` -> `revised_live`
   Trigger: semantic-anchor gate passes and dwell-time policy still allows a
   visible update

7. `committed_live` -> `merged_absorbed`
   Trigger: coordinator approves a head-anchored segmentation repair that
   removes this segment's visible identity

8. `revised_live` -> `canonicalized`
   Trigger: post-session canonicalization materializes the final transcript
   edition

Agents must not invent extra transitions locally. Illegal transitions should
fail loudly in tests and replay.

### Transcript Editions

The system should support three transcript views:

1. `live_first_commit`
2. `live_revised`
3. `canonical_post_session`

## Skill Manifest And Domain Skill Model

The general-purpose core should not hardcode domain-specific intelligence. It
should load domain skills through a manifest-driven model.

### Skill Manifest

Each session should be able to declare one primary skill profile and zero or
more auxiliary skills.

At minimum, a skill manifest should be able to declare:

1. skill id and version
2. domain description
3. glossary and terminology resources
4. domain-specific detectors or enrichers
5. semantic-anchor extensions
6. shadow-verification heuristics
7. presentation hooks
8. regression and benchmark pack references
9. policy knobs that the core runtime recognizes explicitly

### Core Versus Skill Responsibility

The core runtime owns:

1. session lifecycle
2. segment state model
3. event schemas
4. state coordination and leases
5. truth policy enforcement hooks
6. replay and archival mechanics

Skills own:

1. domain terminology
2. domain enrichers and detectors
3. domain-specific heuristic extensions
4. domain-specific UX conventions
5. domain-specific regression packs

### Church-Service Skill

The current church-service implementation should be extracted into a first-party
skill that contributes:

1. church glossary and terminology maps
2. verse and scripture-reference logic
3. sermon-mode and liturgical-mode signals
4. church-specific topic-memory prompt blocks
5. church-specific collision lists and evaluation cases

The point of the reimplementation is not to discard those learnings. It is to
package them cleanly.

## Canonical Event Contract

The event stream should remain the main description of live behavior. The
rebuild should define versioned schemas for at least these event families:

1. session lifecycle
2. session profile and skill activation
3. STT interim and final
4. buffered source sentence
5. live translation update
6. live translation clear
7. feed commit
8. feed revision
9. caption merge
10. segment metadata
11. optional skill-generated enrichments and suggestions
12. optional domain mode changes
13. pipeline trace
14. confidence and health events

No agent should invent new event fields casually. Contract changes must go
through the architecture/contracts owner.

## Program Organization

The rebuild will be executed by a system of specialized agents. Each agent must
have:

1. an explicit mission
2. owned artifacts
3. upstream inputs
4. downstream consumers
5. acceptance criteria

## Agent Roster

### 1. Program Orchestrator Agent

Mission:
Coordinate scope, sequencing, dependencies, and decision escalation.

Owns:

1. master plan
2. milestone board
3. risk register
4. cross-agent status reporting

Must not:

1. unilaterally change architecture contracts
2. absorb implementation work that belongs to specialized agents

### 2. Architecture And Contracts Agent

Mission:
Define the target domain model, interface contracts, event schemas, and state
transition rules.

Owns:

1. shared contracts package
2. event schemas
3. transcript edition model
4. segment lifecycle model
5. ADRs for cross-cutting decisions

Deliverables:

1. schema docs
2. contract tests
3. migration and compatibility notes

### 3. Skill System And Domain Profiles Agent

Mission:
Define the skill manifest model, extract domain-specific logic out of the core,
and package first-party skills such as the church-service skill.

Owns:

1. skill manifest schema
2. skill lifecycle and activation rules
3. domain resource packaging conventions
4. church-service skill extraction plan
5. domain-specific regression pack structure

Deliverables:

1. skill manifest specification
2. first-party church-service skill definition
3. conventions for skill-owned enrichers, heuristics, and UX hooks
4. skill acceptance and compatibility tests

### 4. Backend Platform Agent

Mission:
Build the session runtime shell and service scaffolding.

Owns:

1. session lifecycle
2. WebSocket protocols
3. broadcaster integration
4. configuration loading
5. service composition
6. centralized state coordinator and lease enforcement

Deliverables:

1. backend skeleton
2. session manager equivalent
3. health and readiness endpoints
4. test harness for session startup and teardown
5. state-coordinator contract and lease behavior tests

### 5. Audio And STT Agent

Mission:
Implement audio ingest, normalization, provider adapters, and optional shadow
verification hooks.

Owns:

1. browser audio contract
2. audio normalization
3. primary STT integration
4. STT metadata normalization
5. bounded secondary-verification interface
6. heuristic triage layer for secondary verification

Deliverables:

1. provider-agnostic STT adapter
2. timing and confidence normalization
3. rolling audio span buffer contract
4. span-verification trigger interface
5. deterministic heuristic triage rules and fixtures

### 6. Buffering And Segmentation Agent

Mission:
Implement the structural buffering policy that converts noisy STT output into
trustworthy caption units.

Owns:

1. sentence buffering
2. incomplete-tail heuristics
3. utterance-end handling
4. abandoned-fragment policy
5. merge eligibility scaffolding

Deliverables:

1. buffering engine
2. state-transition tests
3. replay scenarios for hard domain edge cases, including church-service cases

### 7. Translation And Enrichment Agent

Mission:
Implement fast translation, structural enrichment, semantic-anchor policy, and
revision decisions.

Owns:

1. fast translation path
2. English passthrough path
3. structural enrichment
4. semantic-anchor gate
5. deferred release policy
6. revision classification

Deliverables:

1. translation contract
2. semantic-anchor decision engine
3. structured-output parsing policy
4. live versus archival revision decision rules

### 8. Persistence And Canonicalization Agent

Mission:
Build the storage model for live state, event state, and canonical transcript
materialization.

Owns:

1. database schema
2. transcript editions
3. segment lineage persistence
4. canonicalization worker
5. replayable event persistence format

Constraint:
This agent persists coordinator-approved truth. It does not independently
reinterpret live segment state.

Deliverables:

1. schema migrations
2. canonical transcript materializer
3. archival APIs
4. integrity tests for merge and revision replay

### 9. Frontend Live Experience Agent

Mission:
Rebuild the display, listener, and admin experiences around the new contracts
and trust policies.

Owns:

1. display feed state machine
2. listener feed state machine
3. soundboard session UX
4. confidence and revision language
5. skill-generated enrichments and alignment rendering

Deliverables:

1. admin UI
2. display UI
3. mobile listener UI
4. provisional-versus-settled visual language

### 10. Diagnostics, Replay, And Benchmark Agent

Mission:
Make the system measurable and replayable from the beginning.

Owns:

1. capture artifacts
2. replay runner
3. latency and quality dashboards
4. benchmark scenario definitions
5. comparison tooling against the old system

Deliverables:

1. replay CLI or service
2. benchmark corpus
3. metrics glossary
4. acceptance dashboards

### 11. QA, Release, And Documentation Agent

Mission:
Drive release readiness, documentation completeness, and validation discipline.

Owns:

1. test plan
2. release checklist
3. operator documentation
4. incident-response documentation
5. cutover and rollback plan

Deliverables:

1. release runbook
2. operator guide
3. smoke-test suite
4. deployment acceptance criteria

## Coordination Rules

These rules are mandatory for all agents.

1. One owner per contract.
   Shared models must have exactly one contract owner.

2. No cross-boundary changes without explicit notice.
   If an agent changes an event, state model, or interface, all dependent
   agents must be notified in the same change set or same milestone update.

3. Every major behavior needs a replay case.
   If a new policy is added, the change is not done until there is at least one
   replay or fixture that exercises it.

4. No silent policy embedded in prompts alone.
   Trust-sensitive rules must be enforced in code, not merely requested from an
   LLM.

5. Live-path latency budgets are protected.
   Any agent adding new work to the live path must document expected latency and
   fallback behavior.

6. Archival behavior must be explicit.
   Every live mutation path must declare whether it affects:
   `live_first_commit`, `live_revised`, `canonical_post_session`, or some
   combination of them.

7. All visible segment mutations require coordinator approval.
   No agent may publish a live segment-state mutation directly around the
   coordinator, even if it "already knows" the target state.

8. Secondary verification must be triaged.
   Expensive shadow STT is not permitted as a default reaction to ordinary
   uncertainty. It must satisfy the documented triage policy.

## Phase Plan

## Phase 0: Discovery, Contract Freeze, And Replay Corpus

Objective:
Turn the current system into an executable spec for the rebuild.

Primary owners:

1. Program Orchestrator Agent
2. Architecture And Contracts Agent
3. Skill System And Domain Profiles Agent
4. Diagnostics, Replay, And Benchmark Agent

Deliverables:

1. approved reimplementation scope
2. target domain model draft
3. event schema draft
4. skill manifest draft and core-versus-skill boundary definition
5. replay corpus built from representative captures
6. approved ground-truth subset for semantic regression testing
7. benchmark scenarios for church-service replay cases, code-switching,
   fragmented audio, reference citations, and merge-heavy segments

Exit criteria:

1. every major live behavior has at least one replay scenario
2. architecture contract owners are assigned
3. semantic-delta rubric is approved
4. milestone plan is approved

## Phase 1: Core Skeleton And Shared Contracts

Objective:
Create the new repo skeleton and the contracts package before deep feature work
begins.

Primary owners:

1. Architecture And Contracts Agent
2. Skill System And Domain Profiles Agent
3. Backend Platform Agent
4. Frontend Live Experience Agent

Deliverables:

1. repo topology
2. shared event and model definitions
3. skill manifest definitions
4. session protocol definitions
5. initial backend shell
6. initial frontend shell

Exit criteria:

1. backend and frontend compile
2. contract tests pass
3. session startup handshake is proven in isolation

## Phase 2: Audio Ingest, STT, And Session Runtime

Objective:
Recreate the live ingest path and normalized STT behavior.

Primary owners:

1. Backend Platform Agent
2. Audio And STT Agent
3. Skill System And Domain Profiles Agent

Deliverables:

1. browser audio ingest
2. session-start and stop handling
3. normalized STT interim and final callbacks
4. STT metadata model
5. rolling audio buffer for future verification
6. heuristic triage interface for shadow verification

Exit criteria:

1. replayed or live audio can produce normalized interim and final output
2. timing and confidence metadata are preserved
3. session-scoped runtimes are isolated correctly
4. shadow-verification triggers are sparse, deterministic, and observable

## Phase 3: Buffering, Fast Translation, And Pending Commit

Objective:
Restore the minimum viable live interpretation path.

Primary owners:

1. Buffering And Segmentation Agent
2. Translation And Enrichment Agent
3. Backend Platform Agent

Deliverables:

1. sentence buffer equivalent
2. fast translation path
3. passthrough path for English-dominant segments
4. pending-commit queue
5. first live event stream to display/listener consumers

Exit criteria:

1. live output is readable and low latency
2. incomplete fragments are not over-eagerly committed
3. replay scenarios show parity with old-system baseline on core flows

## Phase 4: Structural Enrichment, Merge Policy, And Truth Policy

Objective:
Rebuild the system's intelligence layer with stronger controls than the old
implementation.

Primary owners:

1. Translation And Enrichment Agent
2. Buffering And Segmentation Agent
3. Architecture And Contracts Agent

Deliverables:

1. discourse classification
2. `display_ready` equivalent
3. head-anchored merge policy
4. semantic-anchor gate
5. dwell-time revision policy
6. abandoned-fragment state

Exit criteria:

1. merge decisions are measurable and replayable
2. semantic-anchor violations are blocked or explicitly escalated
3. late revisions follow documented policy

## Phase 5: Domain Skills, Topic Memory, And Alignment

Objective:
Restore non-core but user-visible intelligence features and prove the skill
architecture with the church-service skill.

Primary owners:

1. Translation And Enrichment Agent
2. Skill System And Domain Profiles Agent
3. Frontend Live Experience Agent
4. Diagnostics, Replay, And Benchmark Agent

Deliverables:

1. first-party church-service skill extraction
2. verse detection
3. verse suggestions
4. sermon-mode tracking
5. topic-memory support
6. phrase alignment after stable commit
7. skill hook points for future first-party domains

Exit criteria:

1. church-skill enrichments work without blocking the live path
2. alignment is post-commit and stable
3. replay scenarios show acceptable skill behavior and detection quality

## Phase 6: Canonical Transcript And Historical Integrity

Objective:
Solve the biggest archival weakness in the old system.

Primary owners:

1. Persistence And Canonicalization Agent
2. Diagnostics, Replay, And Benchmark Agent
3. QA, Release, And Documentation Agent

Deliverables:

1. transcript event schema
2. canonical transcript materializer
3. transcript edition APIs
4. comparison tooling between live and canonical transcript forms

Exit criteria:

1. a session can be replayed into a canonical transcript deterministically
2. absorbed and revised segments preserve lineage
3. operator-facing history no longer reflects only the first draft

## Phase 7: Confidence Beacon, Diagnostics, And Hardening

Objective:
Add the operational trust layer and harden the system for production-like use.

Primary owners:

1. Diagnostics, Replay, And Benchmark Agent
2. Frontend Live Experience Agent
3. QA, Release, And Documentation Agent

Deliverables:

1. confidence beacon
2. richer diagnostics dashboards
3. latency, revision, and confidence metrics
4. incident and operator runbooks

Exit criteria:

1. operators can tell when the system is healthy or struggling
2. confidence signals correlate with known replay outcomes
3. the release checklist is complete

## Acceptance Criteria By Product Surface

### Admin / Soundboard

Must support:

1. session start and stop
2. device capture
3. session-profile and skill selection
4. domain-resource selection when required by the active skill
5. visible system health state

### Sanctuary Display

Must support:

1. low-latency live draft language
2. clear committed segment rendering
3. visible distinction between provisional and settled states
4. conservative handling of revisions
5. merge-safe rendering

### Mobile Listener

Must support:

1. stable committed English feed
2. one live draft line
3. connection reliability appropriate for weaker devices

### Diagnostics

Must support:

1. event tracing
2. metrics inspection
3. replay-driven debugging
4. transcript-edition comparison

## Testing And Validation Strategy

The rebuild must pass five categories of validation.

### 1. Contract Tests

Validate:

1. event schemas
2. session protocol
3. transcript-edition rules
4. skill manifest compatibility
5. merge lineage behavior

### 2. Replay Tests

Validate:

1. buffering behavior
2. merge decisions
3. revision rate
4. canonical transcript generation
5. skill-pack parity on approved church-service replay cases
6. approved domain-skill behavior on any additional first-party skill packs

### 3. Regression Delta Tests

Validate:

1. semantic-anchor protection against negation drift
2. number and reference-identifier preservation
3. replay parity against the approved ground-truth corpus
4. build-fail behavior when a new policy or model introduces a forbidden
   semantic regression

### 4. Latency Tests

Track:

1. interim latency
2. first visible English latency
3. commit latency
4. deferred-release latency
5. shadow-verification overhead

### 5. Trust Tests

Track:

1. number and reference fidelity
2. negation preservation
3. late semantic revisions after read threshold
4. canonical transcript divergence from live-revised transcript
5. false-start and abandoned-fragment correctness

## Recommended Metrics

At minimum, the new system should emit:

1. interim-to-final latency
2. final-to-first-English latency
3. final-to-feed-commit latency
4. feed-revision counts by reason
5. merge counts and merge-chain length
6. deferred-release counts
7. abandoned-fragment counts
8. semantic-anchor rejection counts
9. shadow-verification trigger counts
10. canonicalization drift counts
11. semantic-delta regression failures

## Risk Register

### Risk 1: Rebuild Scope Creep

Mitigation:
Preserve current product contract first. Do not mix experimental product ideas
into parity milestones.

### Risk 2: Overpowered LLM Behavior

Mitigation:
Enforce semantic anchors in code. Use literal fallback paths. Replay all
high-risk semantic cases.

### Risk 3: Late Revision Fatigue

Mitigation:
Server-side dwell policy, provisional visual language, and explicit revision
severity classes.

### Risk 4: Canonical Transcript Ambiguity

Mitigation:
Treat transcript editions as separate first-class outputs with deterministic
materialization rules.

### Risk 5: Agent Drift And Duplicate Work

Mitigation:
Use contract ownership, milestone gates, and required replay cases per feature.

### Risk 6: Latency Regression

Mitigation:
Protect the live path with explicit budgets and bounded fallback behavior.

## Decision Log Requirements

Any agent proposing a meaningful change in these areas must create a short ADR
or decision note:

1. event schemas
2. segment lifecycle states
3. merge policy
4. dwell policy
5. semantic-anchor rules
6. persistence model
7. STT verification strategy
8. transcript edition rules
9. skill manifest rules and skill/core boundary changes

## Immediate Phase 0 Tasks

The first wave of work after approving this plan should be:

1. freeze the target behavior list from the current system
2. define the canonical segment lifecycle
3. define the transcript-edition model
4. define the session profile and skill manifest model
5. identify which current church-service behaviors move into the church skill
6. collect and label representative replay captures
7. define the server-side truth policy and semantic-anchor categories
8. define the operator-facing confidence-beacon inputs
9. define the coordinator lease model and legal transitions
10. define the shadow-verification triage policy
11. create the shared contracts package

## Definition Of Done For The Program

The reimplementation program is complete only when:

1. live behavior reaches agreed parity on replay scenarios
2. semantic-anchor policy prevents silent high-risk meaning drift
3. late revision behavior is intentional and bounded
4. canonical transcript materialization exists and is trusted
5. diagnostics, replay, and benchmark workflows are usable by future agents
6. coordinator-managed segment truth prevents conflicting live states
7. operator documentation and cutover guidance are complete

## Final Guidance To All Agents

Build the new system as if future maintainers did not live through the old one.

That means:

1. make state explicit
2. make trust policy explicit
3. make replay easy
4. make archival behavior explicit
5. keep the core runtime general and move domain specificity into skills
6. prefer deterministic contracts over clever prompt behavior

The old system taught us what the real problems are. The new system should
encode those lessons directly, not rediscover them by accident.
