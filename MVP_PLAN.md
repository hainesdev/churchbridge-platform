# ChurchBridge AI MVP Plan

This document defines the initial MVP needed to move ChurchBridge AI from a
working prototype to a pilot-ready product for real church services.

The MVP is intentionally narrow.

It should optimize for:

- live Spanish to English sermon translation
- one church at a time, with light multi-church support
- operator confidence during a real service
- enough stability to run weekly without developer babysitting

It should not yet optimize for:

- enterprise sales requirements
- deep integrations across all church production tools
- broad multilingual expansion
- full analytics and billing sophistication

---

## MVP Goal

Deliver a production-hosted version of ChurchBridge AI that a church can use for
live Spanish sermon interpretation on:

- a sanctuary display
- mobile listeners via browser
- a single operator/admin console

The MVP should be strong enough to support:

- one or more weekly services
- pastor-specific glossary tuning
- confidence in visible caption behavior
- post-service review and correction

---

## MVP Product Promise

For the MVP, ChurchBridge should be positioned as:

**A sermon-aware live translation system for churches that prioritizes thought-complete captions and theological term accuracy over raw speed.**

That means the MVP must clearly outperform generic live caption products in:

- fragmented sermon sentence handling
- theological term stability
- scripture-aware translation behavior
- sanctuary-friendly caption output

---

## MVP Scope

### 1. Core Live Translation

Keep and harden the current core pipeline:

- browser-based soundboard/admin audio capture
- Deepgram live Spanish STT
- Google fast-path translation
- async LLM enrichment and translation improvement
- discourse-aware sentence buffering
- caption merge behavior for split sermon thoughts
- sanctuary display WebSocket feed
- mobile listener WebSocket feed

MVP exit criteria:

- a service can run end to end without manual restarts
- captions remain readable and structurally coherent
- delayed corrections do not visibly break the experience

### 2. Reliability And Recovery

The MVP must be safe to run in a live service.

Required work:

- structured logs with service/session IDs
- production environment configuration for backend and frontend
- reconnect handling for display, listener, and admin clients
- graceful session cleanup on disconnect
- vendor error handling with fallback behavior
- Redis-backed broadcaster enabled for deployed environments
- readiness and liveness checks
- basic monitoring and alerting

MVP exit criteria:

- app survives normal network interruptions
- app degrades gracefully when enrichment fails
- operator can recover without code changes

### 3. Operator Console

The MVP needs a usable live operator workflow.

Required UI additions:

- service status panel
- current church/session info
- vendor health and latency indicators
- live raw Spanish transcript view
- live visible English caption view
- warning state for stalled enrichment or repeated deferred releases
- simple session controls: start, stop, reconnect, safe mode

MVP exit criteria:

- a volunteer or staff operator can understand service state in real time
- issues become visible before the congregation notices them

### 4. Glossary And Church Configuration

The MVP must let each church tune language without code changes.

Required backend and UI support:

- church profile
- denomination selection
- custom glossary management
- preferred translation overrides
- display preferences
- language and caption settings

MVP exit criteria:

- a church can update key theological terms before service
- the pipeline consumes these settings during translation and enrichment

### 5. Accuracy Features For MVP

The MVP should implement the highest-value accuracy moat features first.

Required features:

- denomination-aware glossary profiles
- scripture reference detection
- quote-introduction handling improvements
- stronger confidence-aware hold/release behavior
- local correction memory from prior reviewed sermons

MVP exit criteria:

- scripture-heavy sermons produce fewer visible mistranslations
- repeated pastor terminology gets more stable over time

### 6. Trust Features For MVP

Churches need to trust the product before they adopt it.

Required features:

- live glossary correction panel
- post-service transcript review screen
- correction approval flow for future sermon reuse
- visible audit log for caption rewrites and merges
- low-confidence safe mode that prefers caution over aggressive rewriting

MVP exit criteria:

- a church can fix bad terminology without developer intervention
- the next service benefits from the correction history

---

## MVP Out Of Scope

These should not block the MVP:

- full billing and self-serve subscriptions
- deep enterprise auth and SSO
- complex multi-campus administration
- full music detection and suppression
- full speaker diarization and role labeling
- OBS, ProPresenter, and livestream integrations
- multilingual expansion beyond the core Spanish to English use case
- advanced sermon archive analytics

Some of these are high-value, but they belong in the post-MVP production plan.

---

## MVP Milestones

## Milestone 1: Production Hardening

Deliver:

- deployable backend/frontend configuration
- Redis-backed event distribution
- structured logging
- health checks
- reconnect and recovery logic
- monitoring hooks

Success signal:

- the system can stay up through a rehearsal and a real service without manual
  engineering intervention

## Milestone 2: Operator And Church Setup

Deliver:

- church settings UI
- glossary management
- denomination profile selection
- operator console status panel
- visible live transcript/caption monitor

Success signal:

- a church admin can prepare the system for a service without editing code or
  environment files

## Milestone 3: Accuracy And Trust

Deliver:

- scripture-aware handling
- pastor/church terminology memory
- safe-mode confidence behavior
- live glossary correction
- post-service review flow

Success signal:

- the system improves over repeated services and builds operator trust

## Milestone 4: Pilot Launch

Deliver:

- staging deployment
- pilot onboarding checklist
- incident runbook
- benchmark and service QA checklist
- one pilot church launch

Success signal:

- at least one church can run weekly on the product with manageable support

---

## MVP Technical Workstreams

### Backend

- harden session lifecycle in `server/services/session_manager.py`
- improve reconnect-safe pub/sub in `server/services/broadcaster.py`
- add structured health and readiness checks in `server/main.py`
- extend church config and glossary persistence under `server/db`
- add review/correction APIs under `server/routes/services.py`
- strengthen translation gating in `server/services/sentence_buffer.py`
- extend enrichment context in `server/services/llm_enrichment_service.py`

### Frontend

- expand `client/components/SoundboardAdmin.tsx` into a real operator console
- add settings and glossary management UI
- surface warnings and safe-mode controls
- refine `client/components/TranslationDisplay.tsx` for production readability
- refine `client/components/MobileListener.tsx` for stable listener UX

### QA And Evaluation

- add benchmark coverage for scripture-heavy and fragment-heavy sermons
- implement theological term and scripture fidelity metrics
- create a pilot acceptance checklist per church
- compare benchmark scorecards before each production rollout

---

## MVP Launch Criteria

ChurchBridge is ready for MVP pilot use when all of the following are true:

- production deployment is documented and repeatable
- operator console exposes live service health
- glossary and church settings are editable in-product
- low-confidence handling is safer than the current prototype behavior
- post-service corrections persist into future runs
- benchmark coverage includes theological and scripture-focused evaluation
- one church can complete repeated weekly services successfully

---

## MVP Success Metrics

- first live caption appears reliably within acceptable service latency
- fragment leak rate trends downward
- visible incorrect merge incidents are rare
- theological term precision improves across repeated services
- operator interventions decrease over time
- pilot church retains usage across multiple weeks

---

## Recommended MVP Sequence

1. Production hardening
2. Operator console
3. Church settings and glossary UI
4. Scripture-aware accuracy improvements
5. Live correction and post-service review
6. Pilot launch with one church

The MVP should prove that ChurchBridge can be a dependable church product
before expanding into a broader commercial platform.
