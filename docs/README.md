# BFBT documentation

[简体中文文档导航](README.zh-CN.md)

This page is the English map for BFBT's architecture, contracts, acceptance evidence, research
records, and user guides. Most detailed engineering records currently retain their original Chinese
text; filenames, commands, schemas, and immutable identities are language-neutral.

## Start here

- [No-programming AI Agent guide](guides/ai_agent_guide.md): prepare an Ubuntu server, install BFBT,
  connect an Agent, and complete research through reviewed natural language and staged permission.
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
- [Fast Matrix phase-two design](design/fast_matrix_phase2.md): joint candidate intervals, sparse
  holdings, complexity, and reproducible benchmark contract.
- [DE-v1 data preparation](design/data_prepare_de_v1.md): read-only planning, recorded preparation,
  exact snapshots, readiness, and local-first boundaries.
- [General Agent workflow](design/agent_workflow.md): natural-language intent contracts, semantic
  confirmation, action grants, safe expressions, human promotion, and evidence hand-offs.
- [Configuration reference](reference/configuration.md): fields, defaults, and validation rules.
- [Data contract](reference/data_contract.md): fact tables, derived tables, and artifact schemas.
- [Data management](reference/data_management.md): local layout, partitions, versions, and catalog.
- [Interfaces](reference/interfaces.md): public module responsibilities and boundaries.
- [Open-source release policy](reference/open_source_release.md): locked environments, dependency
  updates, versioning, tags, distribution gates, and GitHub Releases.
- [Extension compatibility policy](reference/extension_policy.md): supported extension paths,
  stable contracts, security boundaries, and the current no-plugin-loader decision.

## Verification and audit evidence

- [Acceptance overview](acceptance/plan.md): A01–A11 foundation.
- [Event Engine acceptance](acceptance/v2_plan.md): A12–A18.
- [Low-memory acceptance](acceptance/v3_plan.md): A19–A25.
- [Reusable analysis acceptance](acceptance/v4_plan.md): A27–A30.
- [Fast Matrix acceptance](acceptance/v5_plan.md): A31–A35.
- [Verified Showcase](acceptance/A39.md): ResearchIntent, read-only doctor, immutable evidence, and
  deterministic presentation.
- [Public-release contract](acceptance/A40.md): BFBT identity and independent English/Chinese HTML.
- [Fast Matrix phase-two acceptance](acceptance/A41.md): joint execution, sparse state, economic
  equivalence, and offline performance evidence.
- [DE-v1 acceptance](acceptance/A42.md): deterministic planning, resumable preparation, readiness,
  lineage, and authorization gates.
- [General Agent acceptance](acceptance/A43.md): safe factor expressions, unified preflight,
  plan-bound authorization, resumable stages, human promotion, and cited evidence.
- [Open-source release acceptance](acceptance/A44.md): lock integrity, tag/version equality,
  package content, checksums, dependency updates, and release automation.

## Research and real strategy records

- [Research registry](research/registry.md): factor candidates, QR-v1 decisions, and promotion state.
- [Open-source trend/momentum batch](research/open_source_trend_momentum_candidates.md): pinned
  Qlib/`ta` formulas, BFBT adaptations, de-duplication, parameters, and implementation state.
- [Research rules](research/rules/QR-v1.md): current versioned Quick Research decision rule.
- [Strategy identities](../strategies/README.md): real strategy specifications and formal-run maps.

## Maintainer records

- [Start here](maintainer/START_HERE.md): required reading order and authorization boundaries.
- [Current state](maintainer/CURRENT_STATE.md): implemented capabilities and verified baselines.
- [Active work](maintainer/ACTIVE_WORK.md): current research and development facts.
- [Development backlog](maintainer/DEVELOPMENT_BACKLOG.md): confirmed unfinished work, owner
  decisions, and conditional directions.
- [Data subsystem plan](maintainer/DATA_ENGINE_PLAN.md): local-first data preparation goals and the
  next bounded development phase.
- [Architecture decisions](maintainer/ARCHITECTURE_DECISIONS.md): durable cross-session decisions.
- [AI Agent readiness](maintainer/AI_AGENT_READINESS.md): implemented thin slice and remaining gaps.
- [Showcase plan](maintainer/SHOWCASE_PLAN.md): implemented bounded showcase and retained boundaries.
- [Operations](maintainer/OPERATIONS.md): data, artifacts, reports, jobs, and formal-run handling.
