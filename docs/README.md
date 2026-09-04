# ChurchBridge AI Docs

This folder is the documentation index for the project as it exists today.

The repository is currently in a mixed state:

- some durable docs already live under `docs/`
- some important runbooks and directives still live at the repository root

The goal of this index is to make that mixed state easy to navigate without
pretending the migration is already finished.

## Start Here

- Project overview, local setup, and pipeline walkthrough:
  [`README.md`](../README.md)
- Overview docs:
  [`overview/README.md`](./overview/README.md)
- Operations docs:
  [`operations/README.md`](./operations/README.md)
- Planning docs:
  [`plans/README.md`](./plans/README.md)
- Automation and review docs:
  [`automation/README.md`](./automation/README.md)

## By Task

### Understand The System

- High-level product and pipeline overview:
  [`README.md`](../README.md)
- Implementation-level runtime data flow:
  [`overview/data-flow.md`](./overview/data-flow.md)
- Governing technical direction and design constraints:
  [`DIRECTIVE.md`](../DIRECTIVE.md)
- Caption chain lifecycle and revision behavior:
  [`caption_chain_lifecycle_implementation.md`](./caption_chain_lifecycle_implementation.md)
- Topic-tracker semantic memory design and implementation status:
  [`plans/topic-tracker-semantic-memory-plan.md`](./plans/topic-tracker-semantic-memory-plan.md)

### Run Or Operate The System

- Deployment and production operations:
  [`DEPLOYMENT.md`](../DEPLOYMENT.md)
- Testing and benchmark runbook:
  [`TESTING_AND_BENCHMARKS.md`](../TESTING_AND_BENCHMARKS.md)

### Planning And Roadmap

- Planning index:
  [`plans/README.md`](./plans/README.md)
- Agent-coordinated general-purpose reimplementation program plan:
  [`plans/agent_coordinated_reimplementation_program_plan.md`](./plans/agent_coordinated_reimplementation_program_plan.md)
- Skill manifest specification:
  [`plans/skill_manifest_spec.md`](./plans/skill_manifest_spec.md)
- Church-service skill extraction plan:
  [`plans/church_service_skill_extraction_plan.md`](./plans/church_service_skill_extraction_plan.md)
- General-purpose repo and module layout plan:
  [`plans/general_purpose_repo_module_layout_plan.md`](./plans/general_purpose_repo_module_layout_plan.md)
- MVP planning:
  [`MVP_PLAN.md`](../MVP_PLAN.md)
- Production roadmap:
  [`PRODUCTION_PLAN.md`](../PRODUCTION_PLAN.md)

### Autonomous Evaluation And Review

- Automation and review index:
  [`automation/README.md`](./automation/README.md)
- Autonomous evaluation plan:
  [`AUTONOMOUS_EVALUATION_PLAN.md`](../AUTONOMOUS_EVALUATION_PLAN.md)
- Self-improvement directive:
  [`SELF_IMPROVEMENT_DIRECTIVE.md`](../SELF_IMPROVEMENT_DIRECTIVE.md)
- Self-improvement loop runbook:
  [`SELF_IMPROVEMENT_LOOP_RUNBOOK.md`](../SELF_IMPROVEMENT_LOOP_RUNBOOK.md)
- Review instructions:
  [`REVIEW_INSTRUCTIONS.md`](../REVIEW_INSTRUCTIONS.md)

## Current Docs Layout

- `docs/overview/`
  - architecture and design indexes
- `docs/operations/`
  - deployment and testing indexes
- `docs/automation/`
  - agent-process and evaluation indexes
- `docs/plans/`
  - active design and planning docs

These section folders now exist primarily as stable entry points. Some of the
linked source material still lives at the repository root while migration is
incremental.

## Cleanup Decisions In This Pass

- Keep the root `README.md` as the main human entry point.
- Do not move large root-level documents yet; first make discovery reliable.
- Use section indexes under `docs/` to point at the current source-of-truth
  files.
- Prefer updating docs in place over duplicating the same long-form content in
  two locations.

## Next Cleanup Opportunities

- Move deployment and testing runbooks under `docs/operations/` once link
  consumers are updated.
- Move directive and automation docs under `docs/overview/` and
  `docs/automation/`.
- Shorten the root `README.md` once operational detail has fully migrated into
  section docs.
