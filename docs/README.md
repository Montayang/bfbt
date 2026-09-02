# BFBT documentation

[简体中文文档导航](README.zh-CN.md)

This page is the English map for BFBT's architecture, contracts, acceptance evidence, research
records, and user guides. Most detailed engineering records currently retain their original Chinese
text; filenames, commands, schemas, and immutable identities are language-neutral.

## Start here

- [Beginner tutorial](guides/beginner_tutorial.md): prepare public data and produce a first report.
- [User manual](guides/user_manual.md): CLI, configuration, outputs, interpretation, and
  troubleshooting.
- [Custom factor tutorial](guides/custom_factor_tutorial.md): implement, register, test, and run a
  cross-sectional factor.
- [Self-guided report tour](../showcase/README.md): explore the Quick Research, Fast Matrix, and
  Event Engine reports; the optional verified case study is documented separately on that page.

## Architecture and contracts

- [Architecture overview](design/architecture.md): module boundaries and end-to-end data flow.
- [System design](design/system_design.md): goals, timing, correctness constraints, inputs, and
  outputs.
- [Event Engine design](design/v2_design.md): chronological execution, state, risk, and artifact model.
- [Fast Matrix design](design/v5_fast_matrix_engine.md): capability boundary, columnar economics,
  research artifacts, and Event promotion.
- [Configuration reference](reference/configuration.md): fields, defaults, and validation rules.
- [Data contract](reference/data_contract.md): fact tables, derived tables, and artifact schemas.
- [Data management](reference/data_management.md): local layout, partitions, versions, and catalog.
- [Interfaces](reference/interfaces.md): public module responsibilities and boundaries.

## Verification and audit evidence

- [Acceptance overview](acceptance/plan.md): A01–A11 foundation.
- [Event Engine acceptance](acceptance/v2_plan.md): A12–A18.
- [Low-memory acceptance](acceptance/v3_plan.md): A19–A25.
- [Reusable analysis acceptance](acceptance/v4_plan.md): A27–A30.
- [Fast Matrix acceptance](acceptance/v5_plan.md): A31–A35.
- [Verified Showcase](acceptance/A39.md): ResearchIntent, read-only doctor, immutable evidence, and
  deterministic presentation.
- [Public-release contract](acceptance/A40.md): BFBT identity and independent English/Chinese HTML.

## Research and real strategy records

- [Research registry](research/registry.md): factor candidates, QR-v1 decisions, and promotion state.
- [Research rules](research/rules/QR-v1.md): current versioned Quick Research decision rule.
- [Strategy identities](../strategies/README.md): real strategy specifications and formal-run maps.

## Maintainer records

- [Start here](maintainer/START_HERE.md): required reading order and authorization boundaries.
- [Current state](maintainer/CURRENT_STATE.md): implemented capabilities and verified baselines.
- [Architecture decisions](maintainer/ARCHITECTURE_DECISIONS.md): durable cross-session decisions.
- [AI Agent readiness](maintainer/AI_AGENT_READINESS.md): implemented thin slice and remaining gaps.
