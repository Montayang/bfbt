# Showcase version plan

Updated: 2026-09-01.

Implementation status (2026-09-02): S0–S5 are implemented on `codex/showcase-plan`. Six focused
A39 tests and the 331-test full offline suite pass; the real local preparation verifies all three
H2 runs with one expected provenance warning and no failures. The current presentation uses the
existing qualified H2 `r01` evidence and displays `git_dirty=true`. Clean `r02` evidence remains a
separately authorized future run, not a hidden prerequisite or rewrite of this release.

## Product decision

The showcase is a short, local, evidence-backed demonstration of the research workflow. It is not
the finished open-source release and must not pretend that the general no-code Agent control plane
already exists.

The primary audience is a technically curious visitor watching a 8–12 minute demonstration on the
maintainer's prepared machine. The secondary audience is someone opening the repository afterward.
The presentation should answer four questions without requiring them to understand the codebase:

1. What research question did the user express?
2. How did the system freeze ambiguous economic semantics before execution?
3. What did the deterministic engines and real public market data produce?
4. Can every headline result be traced back to exact data, configuration, code, costs, and trades?

The showcase must remain offline after preparation, must never contact an account or trading API,
and must not require a long backtest during the presentation.

## Current assessment

The computation layer is already stronger than the current presentation layer. Quick Research,
Fast Matrix, Event/V2, bounded-memory recovery, immutable artifacts, detailed audit reports, and
325 offline tests are implemented. The gaps that make the repository awkward to demonstrate are:

- The first-use path starts with installation, a long public-data download, preparation scripts,
  four configuration files, and several commands. There is no single showcase entry point.
- Reports are strong run-level audit workbenches, but there is no executive page that connects the
  research request, engine workflow, multi-period comparison, costs, risks, and evidence links.
- The Agent workflow exists as maintainer practice rather than a versioned product contract. A
  visitor cannot see a structured intent, ambiguity list, frozen semantics, preflight, or exact
  authorization boundary.
- The root README is materially behind the implemented A01–A38 architecture and does not lead with
  a visual workflow, current capabilities, a short demo, or current limitations.
- There is no `doctor`/showcase readiness command, so environment and artifact problems are found
  piecemeal during a presentation.
- The only H2 artifacts present on this machine are useful real full-market results, but their
  environment records say `git_dirty=true`. This fact is already documented and must remain visible;
  clean replacement runs would require a separately authorized `r02` execution.
- CI, a dependency lock, CONTRIBUTING, SECURITY, CHANGELOG, and release automation are still absent.
  These matter before an open-source launch, but most are not prerequisites for a local showcase.

## Showcase experience

The preferred presentation surface is a deterministic static HTML hub generated from verified
local artifacts. It should reuse the report visual language and link into existing deep audit pages;
it should not introduce a web framework, database service, or cloud dependency.

```text
Natural-language request (spoken to the Agent)
  -> versioned ResearchIntent
  -> ambiguity / authorization / capability preflight
  -> human-readable frozen research card
  -> existing completed research and Event artifacts
  -> showcase comparison hub
  -> exact run report, fill, position, risk event, manifest, and config evidence
```

The Agent remains outside the economic engines. For the showcase it may translate natural language
into a structured intent and explain results, but validation, planning, artifact verification, and
all numerical summaries must be deterministic application code.

## Required development scope

### SC01 — Showcase contract and curated scenario

- Add a versioned `ShowcaseSpec` that identifies the title, narrative, factor/strategy identity,
  ordered immutable run IDs, comparison dimensions, disclosure text, and optional derived views.
- Store only the spec and schema in Git. Resolved local paths and generated pages remain under
  `data/backtest/showcases/<showcase_id>/` and stay ignored.
- Use `R5-T4-H2-ROLLING` May–July as the initial scenario because it demonstrates a cross-sectional
  factor, path-dependent Event execution, rolling margin, costs, different market regimes, and
  complete trade/risk audit. Do not frame positive months as evidence of future profitability.
- Show each run's provenance state. Dirty source, warnings, missing funding policy, or a failed
  manifest verification must never be hidden behind a green summary card.

### SC02 — Minimal Agent-facing intent and freeze sheet

- Implement a versioned `ResearchIntent` for the showcase thin slice: operation kind, user text
  hash, market, date range, factor identity/parameters, direction, universe, Rank rule, clocks,
  fill, sizing, costs, risk exits, terminal handling, requested output, and user decisions.
- Represent unresolved choices explicitly. Rendering is allowed; execution is rejected while any
  economically material ambiguity remains.
- Produce a deterministic, bilingual freeze sheet and action plan with stable reason codes.
- Encode action classes for read-only inspection, data/network, research execution, formal Event
  execution, and source-control changes. The showcase demonstrates the boundary but does not need
  to build the complete production authorization service.
- Natural-language parsing itself remains an Agent responsibility in this iteration. Do not add an
  embedded LLM dependency and do not execute arbitrary generated Python or shell.

This is a coherent thin slice of AG01–AG04. Their global readiness status must remain `partial` or
`missing` until the general contracts and end-to-end workflow are implemented beyond the curated
showcase.

### SC03 — Read-only preflight and readiness command

- Add a side-effect-free planner that validates the intent/spec, resolves exact artifacts, verifies
  every manifest hash, checks required tables, reports source/dependency provenance, and calculates
  the output paths it would use.
- Add `bianbt doctor` with machine-readable JSON and human output for Python/package identity,
  required dependencies, writable local output root, disk headroom, catalog availability, selected
  run availability, manifest validity, and presentation port availability when requested.
- Use stable check IDs, severities, exit codes, and repair suggestions. `doctor` must not download,
  mutate data, rebuild reports, or run tests.

### SC04 — Deterministic showcase hub

- Add `bianbt showcase build --spec <path>` and a read-only `showcase inspect` command.
- Generate a self-contained local hub with these sections:
  1. one-screen product proposition and explicit “offline research, not live trading” boundary;
  2. Quick Research → Fast Matrix → Event/V2 architecture and current support boundary;
  3. original request, normalized intent, frozen semantics, preflight, and authorization timeline;
  4. May/June/July result comparison with return, drawdown, equity, turnover, costs, trade count,
     warnings, and provenance badges;
  5. rolling-margin trajectories and regime comparison;
  6. evidence links to each immutable report, resolved config, metrics, warnings, and manifest.
- All metrics must be loaded from verified artifacts. Narrative labels may be curated in the spec,
  but numeric result fields must not be manually copied into the renderer.
- Avoid leading with extrapolated one-month annualized return. Headline metrics are total return,
  maximum drawdown, ending equity, explicit cost drag, turnover, and sample interval.
- Preserve responsive layout, Chinese-first bilingual labels, keyboard navigation, and a printable
  view. The existing deep report remains the audit surface rather than being duplicated.

### SC05 — One-command local presentation

- Add a documented preparation command that runs `doctor`, verifies artifacts, builds the hub, and
  prints the exact entry page. It may rebuild only derived display output.
- Optionally add a narrowly scoped local static-file server command. It must bind to loopback by
  default, expose only the selected showcase directory, and never start automatically.
- Add a `showcase/README.md` containing the 8–12 minute script, expected screens, fallback screenshots,
  and a no-network rehearsal checklist.
- Add a sanitized, deterministic synthetic mini fixture only if a live sub-minute execution is
  useful. It must be labelled as an operational demonstration and kept separate from real-market
  evidence. The presentation must not depend on it.

### SC06 — Credibility and repository front door

- Rewrite the root README around the current A01–A38 system: value proposition, workflow diagram,
  screenshots, supported/unsupported behavior, quick showcase path, architecture links, test
  baseline, and investment-risk disclaimer.
- Remove stale statements such as “A01–A10 implemented” and the incomplete built-in factor list.
- Add a small architecture image or repository-native diagram and two screenshots generated from
  the showcase hub. Screenshots are documentation assets, not result truth.
- Add minimal `CONTRIBUTING.md`, `SECURITY.md`, and `CHANGELOG.md` before sharing the repository link.
  A dependency lock, multi-version CI, packaging/release automation, and plugin compatibility policy
  remain part of the later engineering-grade open-source phase unless the showcase will be publicly
  distributed rather than shown locally.

### SC07 — Clean evidence decision

Before the final rehearsal, choose one of these explicit paths:

1. Preferred: after implementation is committed and the worktree is clean, separately authorize
   May–July H2 `r02` formal runs, verify the manifests, and point the ShowcaseSpec to them.
2. Acceptable for an internal preview: retain the existing `r01` runs and display a prominent
   `git_dirty=true` provenance badge plus the exact source fingerprint.

Never rewrite `r01`, suppress its provenance, or claim that a rerun is identical until equality has
actually been checked. A clean rerun is evidence preparation, not a prerequisite for developing the
showcase renderer.

## Acceptance gates

The showcase is ready only when all of the following hold:

- From a prepared machine, one documented command produces the entry page without network access.
- A missing run, hash mismatch, failed run, or unresolved intent fails closed with a useful reason.
- Rebuilding twice from identical inputs produces byte-identical JSON evidence and semantically
  deterministic HTML.
- Every displayed number is covered by a test that traces it to a verified artifact field; run
  period, factor parameters, strategy name, and shared economic identity match the ResearchIntent.
- Dirty provenance and warnings are visible in both human and machine-readable summaries.
- The generated hub contains no absolute machine path, credential-like value, `.env` access, live
  client import, or external JavaScript/CSS dependency.
- Links from the hub reach all three run reports and their exact config/metrics/manifest evidence.
- Layout is reviewed at desktop presentation size and a narrow viewport; keyboard-only navigation
  reaches all sections.
- Focused showcase tests pass, then the complete offline suite passes under explicit authorization.
- A rehearsal succeeds with networking disabled and without launching a formal backtest.

## Suggested implementation order

| Milestone | Scope | Dependency | Deliverable |
|---|---|---|---|
| S0 | SC01 and fixtures/contracts | none | Frozen scenario, schema, failure cases |
| S1 | SC02–SC03 | S0 | Intent, freeze sheet, preflight, `doctor` |
| S2 | SC04 | S0–S1 | Deterministic comparison hub and evidence index |
| S3 | SC05 | S2 | One-command build, optional loopback server, demo script |
| S4 | SC06 | S2 | Accurate README, screenshots, minimum public-facing files |
| S5 | acceptance and rehearsal | S1–S4 | Focused/full offline evidence and rehearsal record |
| S6 | SC07 if authorized | committed clean source | Clean `r02` formal evidence and final spec update |

S0–S3 form the minimum useful showcase. S4 is required if the visitor will receive the repository
link. S6 is strongly preferred for an external or recorded presentation.

## Explicitly deferred

- General-purpose natural-language parsing inside `bianbt`.
- Arbitrary factor DSL/plugins and generated-code execution.
- A production background-job daemon, queue, cancellation service, or multi-user quotas.
- Automatic Fast Matrix candidate selection; the user remains the promotion decision maker.
- Cloud hosting, authentication, account connectivity, live trading, or exchange order simulation.
- Full AG01–AG16 completion and full engineering-grade open-source release automation.

## Decisions needed before implementation

The maintainer should confirm two presentation choices before S2 visual work begins:

1. Is the primary event an in-person/local demo, a screen recording, or a repository link sent to
   others? This controls whether S4 is mandatory now.
2. Should the first showcase present the existing H2 `r01` results with their dirty-provenance badge,
   or should clean H2 `r02` runs be scheduled after the code is committed?
