# ChurchBridge AI Full Production Plan

This document defines the full production roadmap for ChurchBridge AI after the
initial MVP is complete.

The purpose of this plan is not just to make the application deployable.
It is to make the product commercially stronger than generic live translation
tools and church-specific competitors by building a durable value proposition.

The product strategy is:

- better sermon accuracy than generic translation platforms
- better church workflow than enterprise interpretation tools
- greater trust and reviewability than low-cost church caption tools

---

## Product Vision

ChurchBridge AI should become:

**The live church translation platform that understands sermons, not just speech.**

A full production version should support:

- live Spanish to English sermon interpretation
- additional languages over time
- sanctuary displays, mobile listeners, and livestream outputs
- operator and reviewer workflows
- repeated weekly use across multiple churches or campuses
- product packaging, reliability, and support fit for paid adoption

---

## Strategic Pillars

### 1. Accuracy Moat

ChurchBridge must win on output quality in real sermons.

Target capabilities:

- denomination-aware glossary profiles
- church-specific and pastor-specific terminology memory
- scripture citation and quote recognition
- sermon-mode-aware translation behavior
- confidence-aware hold, release, and merge logic
- theological term review and correction loop
- benchmarked quality metrics for scripture and doctrinal vocabulary

### 2. Workflow Moat

ChurchBridge must fit the realities of church services better than generic
meeting tools.

Target capabilities:

- speaker diarization
- no cross-speaker caption merges
- music detection and translation suppression during songs
- role labels for preacher, reader, testimony speaker, and congregation
- single-speaker and multi-speaker service modes
- sanctuary display optimization
- mobile listener optimization
- livestream and presentation-tool integrations

### 3. Trust Moat

Churches must trust the system doctrinally and operationally.

Target capabilities:

- live correction tools
- post-service review and approval workflow
- explainability for sensitive translation choices
- audit trail for visible caption changes
- safe-mode behavior when confidence drops
- archival correction memory that improves future services

### 4. Commercial Readiness

The platform must be sellable, supportable, and scalable.

Target capabilities:

- authentication and role-based access
- multi-church and multi-campus account structure
- billing and subscriptions
- usage metering
- onboarding workflow
- deployment automation
- monitoring, alerting, backups, and incident response
- privacy, retention, and support tooling

---

## Production Roadmap

## Phase 1: Foundation Hardening

Goal:

Create a stable operational base for production use.

Features:

- environment separation for local, staging, and production
- secure secret handling
- structured logging and tracing
- service/session correlation IDs
- Redis as standard event transport in deployed environments
- background task timeout and retry policy
- robust health, readiness, and dependency checks
- reconnect-safe WebSocket lifecycle
- backup and restore process for SQLite replacement or hosted database layer
- deployment playbooks and rollback procedure

Outcome:

The platform becomes reliably deployable and operable.

## Phase 2: MVP Completion

Goal:

Ship the pilot-ready product.

Features:

- operator console
- church profile and glossary management
- denomination-aware configuration
- live service health dashboard
- low-confidence safe mode
- post-service correction review
- benchmark-driven release gates

Outcome:

One church can use the system live, repeatedly, with manageable support.

## Phase 3: Accuracy Moat Expansion

Goal:

Make the translation engine materially better for Spanish sermons than generic
alternatives.

Features:

- pastor memory from approved past sermons
- theological term scoring in benchmark reports
- scripture precision and recall evaluation
- improved quote-introduction and rhetorical merge handling
- sermon-mode-conditioned translation prompts
- confidence-scored theological term overrides
- passage-aware verse detection improvements
- support for denomination-specific defaults and rule packs

Outcome:

ChurchBridge has a measurable claim to sermon-specific translation quality.

## Phase 4: Workflow Moat Expansion

Goal:

Handle full-service complexity.

Features:

- Deepgram diarization integration
- speaker-aware sentence buffering
- no merge across speaker boundaries
- speaker labels and optional role mapping
- browser-side or server-side music detection
- suppression rules for worship songs and transitions
- operator mode toggle for single-speaker vs multi-speaker service
- better handling for testimony, prayer, reading, and Q&A formats

Outcome:

ChurchBridge becomes more compatible with real church service patterns than
generic event platforms.

## Phase 5: Trust And Review Expansion

Goal:

Make the product reviewable and safe for doctrinally sensitive use.

Features:

- detailed caption history and audit trail
- per-term explanation view for sensitive translations
- flagged-segment review queue
- reviewer roles and approval states
- reusable translation decisions across future services
- incident export for support and customer success

Outcome:

Churches can verify, correct, and improve the system over time.

## Phase 6: Product Experience And Integrations

Goal:

Improve adoption and reduce workflow friction.

Features:

- polished sanctuary display themes
- listener controls for font, mode, and delay
- QR-based join and branded landing flows
- OBS integration
- ProPresenter and EasyWorship integration
- RTMP/livestream caption overlays
- transcript, summary, and archive exports
- searchable multilingual sermon archive

Outcome:

The product fits naturally into church media and accessibility workflows.

## Phase 7: Commercial Platform

Goal:

Scale beyond pilot usage into a real commercial SaaS offering.

Features:

- subscription plans by hours, languages, and campuses
- onboarding wizard and setup checklist
- self-serve or assisted billing flows
- account admin and staff roles
- usage dashboards
- customer support tooling
- retention and privacy controls
- multi-region deployment strategy as needed

Outcome:

ChurchBridge can support paid customers in a repeatable way.

---

## Detailed Feature Plan

## A. Accuracy Features

### A1. Denomination-Aware Glossary Profiles

- base glossaries by denomination or church tradition
- church-level overrides
- pastor-level preferred usage
- benchmark checks for term fidelity

### A2. Scripture Intelligence

- explicit scripture reference detection
- quote-mode handling for readings and citations
- verse-range extension logic
- safer translation policy when scripture confidence is high

### A3. Sermon Memory

- store approved corrections from prior sermons
- inject recent pastor language patterns into translation context
- prioritize stable approved term mappings

### A4. Confidence-Aware Translation Control

- assign confidence bands per caption
- increase hold behavior when confidence is weak
- surface uncertainty to operators without exposing confusion to the audience

### A5. Accuracy Evaluation

- theological term precision and recall
- scripture detection precision and recall
- merge accuracy rate
- fragment leak and visible rewrite tracking

## B. Workflow Features

### B1. Speaker Awareness

- diarization support in live streaming
- preserve `speaker_id` through sentence lifecycle
- role labeling and UI display options

### B2. Music Awareness

- detect likely music locally or server-side
- suppress translation during worship music
- surface service-state changes to the operator

### B3. Service Mode Intelligence

- sermon
- scripture reading
- testimony
- prayer
- announcements
- worship transition

Each mode should influence segmentation, translation, and display behavior.

### B4. Integration Layer

- presentation software output
- livestream overlays
- external display feed support

## C. Trust Features

### C1. Live Correction

- edit glossary terms during service
- manually pin preferred translations
- mark segments as sensitive or reviewed

### C2. Post-Service Review

- inspect transcript, translations, and merges
- approve or reject recommended changes
- persist accepted decisions

### C3. Explainability And Auditability

- show why a sensitive term was translated a certain way
- record caption changes, merges, suppressions, and manual overrides

### C4. Safe Mode

- degrade to simpler, more conservative behavior under uncertainty
- reduce aggressive merge/correction behavior when signals conflict

---

## Platform Requirements

### Security

- authenticated admin access
- role-based permissions
- secure secret storage
- configurable data retention

### Observability

- logs
- metrics
- traces where useful
- alerting for vendor or websocket instability

### Data

- evolve beyond lightweight local persistence as usage grows
- backup and restore procedures
- migration strategy for schema changes

### Deployment

- repeatable backend/frontend deployments
- staging environment
- rollback and incident procedures

---

## Recommended Delivery Order

1. Foundation hardening
2. MVP completion
3. Accuracy moat
4. Workflow moat
5. Trust moat
6. Integrations and UX polish
7. Commercial platform features

This order protects the product from trying to look enterprise-ready before it
is reliable or differentiated.

---

## Release Gates

Before each major phase promotion, require:

- benchmark validation against target sermon sets
- no critical event ordering regressions
- acceptable fragment leak rate
- stable live-session behavior under reconnect scenarios
- manual review of theological and scripture-sensitive segments

---

## Success Definition

ChurchBridge reaches full production maturity when it can:

- run reliably across repeated weekly services
- support multiple churches without custom engineering each time
- demonstrate sermon-specific accuracy advantages
- fit naturally into church live-service workflows
- provide clear trust and review controls
- operate as a sustainable paid product

At that point, ChurchBridge is no longer just a translation pipeline.
It becomes a church translation platform with a defensible value proposition.
