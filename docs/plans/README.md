# Planning Docs

Use this section for roadmap, future-state design, and focused implementation
plans.

## Current Documents

- Agent-coordinated general-purpose reimplementation program plan:
  [`agent_coordinated_reimplementation_program_plan.md`](./agent_coordinated_reimplementation_program_plan.md)
- Bilingual display and pair-generation plan:
  [`docs/bilingual_display_and_pair_generation_plan.md`](../bilingual_display_and_pair_generation_plan.md)
- Chunk identity retention exploration plan:
  [`chunk_identity_retention_exploration_plan.md`](./chunk_identity_retention_exploration_plan.md)
- Skill manifest specification:
  [`skill_manifest_spec.md`](./skill_manifest_spec.md)
- Church-service skill extraction plan:
  [`church_service_skill_extraction_plan.md`](./church_service_skill_extraction_plan.md)
- General-purpose repo and module layout plan:
  [`general_purpose_repo_module_layout_plan.md`](./general_purpose_repo_module_layout_plan.md)
- Topic-tracker semantic memory plan:
  [`topic-tracker-semantic-memory-plan.md`](./topic-tracker-semantic-memory-plan.md)
- MVP planning:
  [`MVP_PLAN.md`](../../MVP_PLAN.md)
- Production roadmap:
  [`PRODUCTION_PLAN.md`](../../PRODUCTION_PLAN.md)

## Current Proven Design References

Use these when a planning conversation needs the current websocket and display
design that is already working in code, not just future-state intent:

- Runtime data flow and live event model:
  [`docs/overview/data-flow.md`](../overview/data-flow.md)
- Caption merge, revision, and feed lifecycle behavior:
  [`docs/caption_chain_lifecycle_implementation.md`](../caption_chain_lifecycle_implementation.md)
- Bilingual display, post-commit alignment, and linked interaction baseline:
  [`docs/bilingual_display_and_pair_generation_plan.md`](../bilingual_display_and_pair_generation_plan.md)
- Chunk-lineage and continuity tuning work:
  [`chunk_identity_retention_exploration_plan.md`](./chunk_identity_retention_exploration_plan.md)

## Notes

- The topic-tracker plan already lives inside `docs/plans/`.
- The bilingual display and chunk-identity docs now serve a dual role:
  they capture the implemented baseline and the remaining tuning roadmap.
- Broader product-planning documents still live at the repository root.
