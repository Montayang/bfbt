# Architecture decisions

This file summarizes durable choices that must survive across sessions. Detailed contracts remain
in `docs/design/` and `docs/reference/`.

## Engine roles

1. Quick Research computes factor diagnostics such as Rank IC, quantile spread, coverage, and Rank
   turnover without simulating an account.
2. Fast Matrix performs constrained, columnar portfolio research for conventional cross-sectional
   target schedules. It produces research artifacts, not formal strategy truth.
3. Event/V2 maintains chronological account, position, risk, and rolling state. It is the formal
   engine for path-dependent strategies and terminal run artifacts.
4. V1 remains readable and regression-compatible but is not the default new-strategy interface.

Unsupported Fast Matrix behavior fails closed or is explicitly promoted to Event; it must not be
silently approximated.

Fast Matrix multi-candidate execution prepares each market input once, keeps only non-zero candidate
holdings, and recurses at the union of rebalance and funding boundaries. Complete intervals between
those boundaries are valued jointly. This changes batch complexity and control depth, not economic
semantics: each candidate retains independent cash, costs, funding, audit rows, checkpoint, identity,
and result hash equivalent to its standalone execution. Dense worst-case arithmetic remains
`O(candidate × time × symbol)`; the practical sparse case scales with active holdings.

## Time and causality

- All intervals use UTC left-closed/right-open semantics.
- Factor availability, Rank, decisions, fills, risk triggers, funding, and valuation have explicit
  clocks. Future bars must never enter an earlier decision.
- Next-bar-open execution and intrabar conflict policy are part of the strategy identity.
- Point-in-time universe membership and missing-bar handling must be explicit and auditable.

## Low-memory execution

- Event/V2 formal full-market runs use chronological chunks with bounded warmup and serialized
  state checkpoints.
- Resume must be economically equivalent to continuous execution.
- Worker memory gates and staged publication are correctness constraints, not optional tuning.
- Reusable analysis and signal snapshots are immutable and content-addressed; cache reuse must not
  change economic output.

## Identity and artifacts

- Dataset snapshot, resolved configuration, factor version, source state, dependency state, and
  economic result jointly determine run identity.
- Successful and failed terminal artifacts are immutable. Revisions receive new IDs.
- Reports are deterministic views built from artifacts and may be rebuilt outside an immutable run.
- Display downsampling cannot delete trades, position changes, or risk events from audit navigation.

## Data subsystem scope

- Data acquisition/preparation and research/execution are separate paths joined only by immutable,
  verified DatasetSnapshots; an engine must never download or silently repair inputs while running.
- BFBT targets a local-first data preparation subsystem that one researcher can maintain. It stays
  in the same repository and process boundary until multiple independent consumers or continuous
  service operation create evidence for a split.
- The next data phase organizes existing Archive/REST, Raw, normalization, quality, Catalog,
  resampling and point-in-time capabilities behind a requirement, side-effect-free plan,
  recoverable preparation job and readiness result. It does not replace those components.
- On-demand bounded preparation is preferred to retaining every market, period, interval and
  feature. Enterprise schedulers, clusters, a universal feature store and permanent online services
  are not architectural goals.
- Futures trades/aggregate trades and derived second bars require a separate demand-backed data and
  execution contract; generic duration parsing alone does not make them supported inputs.
- DE-v1 planning is a pure local observation: it cannot create the workspace, contact a source, or
  select a floating/latest version. Network acquisition and local mutation are distinct recorded
  action classes and both require explicit approval.
- Historical contract state fails closed. A current public `exchangeInfo` snapshot may be used only
  when the requirement explicitly accepts that limitation, which remains in readiness evidence.
- DE-v1 uses one four-step recorded job (`acquire_raw`, `normalize`, `publish_snapshot`,
  `readiness`) and resumes succeeded steps by plan hash. This is the data-specific first slice of a
  future shared job service, not a competing general scheduler.

## Strategy research governance

- Quick Research rules may be versioned and automated.
- Fast Matrix has no universal automatic promotion rule; the user reviews its reports and chooses
  candidates and Event overlays.
- Event strategy acceptance is manual economically, while technical correctness remains covered by
  shared contracts and acceptance tests.

## Agent and showcase boundary

- Natural language is translated into a versioned ResearchIntent before deterministic application
  code is invoked. Unresolved economic ambiguity fails closed.
- The public Showcase entry is a self-guided report tour: Quick Research, Fast Matrix, and Event
  Engine reports are the primary product surfaces. A selected multi-run comparison is an optional
  evidence case, not the repository's main product preview or a required fresh-checkout path.
- The bounded showcase implements this contract for a curated result-query scenario; it is not a
  general no-code Agent service and never embeds arbitrary code execution.
- Showcase pages are deterministic derived views of verified immutable artifacts. They may compare
  runs and derive opening margin from audited notional/leverage, but cannot rewrite result truth.
- Public hosted report examples are byte-for-byte copies of generated, self-contained language
  variants after path/secret/network-reference screening. They contain no datasets or mutable run
  directories, and their labels must preserve the underlying research/formal-run distinction.
- Dirty source provenance, data warnings, and authorization action classes are presentation facts,
  not details the renderer may suppress.
- The general Agent API begins at a strict `AgentResearchIntent`; natural-language interpretation is
  supplied by the integrating Agent and remains reviewable. BFBT does not embed a model vendor or
  accept generated shell/Python as a research specification.
- Authorization is capability-specific, expiring, and bound to one exact workflow-plan hash.
  Acknowledging semantic or cost warnings does not authorize network, data writes, research, Event,
  tests, or Git unless the matching action is separately granted.
- Novel formula factors use the bounded causal expression language. The parser never calls
  `eval`/`exec`; rolling state is per symbol and continuous segment, gaps reset history, and future
  lags are structurally impossible.
- Fast Matrix promotion is a human artifact, not an Agent ranking decision. Event evidence must
  retain the selected `fm-*` identity in resolved configuration.
- Agent explanations are data products: each fact, qualification, or warning cites admitted
  evidence hashes. Free-form prose cannot become workflow truth without those references.

## Public identity and languages

- The public brand is the standalone name **BFBT**. Its public descriptor is “an auditable research
  and backtesting framework for crypto perpetual futures”; distribution, import namespace, module
  entry point, and CLI use `bfbt`. No pre-release `bianbt` compatibility package is retained.
- Third-party venue names may appear only as factual compatibility or data-source references, not
  as part of BFBT's product name or in language that suggests affiliation.
- User-facing surfaces call the chronological formal engine the **Event Engine**. Internal module,
  configuration, schema, and compatibility identities may retain `v2`; they are implementation
  contracts and must not leak into README, guides, Showcase copy, or generated report headings.
- The historical `bianbt.*` Arrow metadata namespace is frozen as part of v1 wire-schema and run
  fingerprints. Brand migration must not invalidate immutable evidence by renaming those keys.
- English is the default repository and generated-report language. Human-facing HTML also publishes
  an independent Simplified-Chinese sibling. Machine-readable identities and evidence are not
  translated or duplicated.
- HTML localization may translate visible text and JavaScript presentation string literals, but it
  must preserve JavaScript syntax, JSON payloads, CSS, preformatted code, and machine-readable
  evidence byte-for-byte. Executable or literal blocks must not pass through HTML text-node regexes.
- Public Markdown entry points follow the same convention: the unsuffixed file is English and an
  independent `.zh-CN.md` sibling contains Simplified Chinese. This includes the documentation map,
  no-programming Agent guide, beginner tutorial, user manual, custom-factor tutorial, and Showcase
  guide.
- BFBT is independent from Binance and has no affiliation, endorsement, sponsorship, or financial
  relationship with Binance. This disclaimer must remain visible in the public front door.

## Packaging, releases, and extensions

- End-user and CI environments install hash-locked dependency sets before installing BFBT with
  dependency resolution disabled. `pyproject.toml`, the bootstrap input, both locks, and their
  exact `uv` generator version form one verified contract.
- A release is an owner-controlled `vMAJOR.MINOR.PATCH` tag whose version matches package metadata
  and dated bilingual changelogs. CI verifies the tag, full supported-Python matrix, wheel/sdist
  contents, and checksums before publishing one GitHub Release.
- GitHub Releases are the authoritative downloadable packages. PyPI stays disabled until Trusted
  Publishing and a protected release environment are configured; long-lived upload tokens are not
  an accepted shortcut.
- Dependency and GitHub Actions updates are review-only. Actions are pinned to immutable commit
  SHAs, and neither Dependabot nor another bot may auto-merge a dependency change.
- The supported external extension surfaces are the versioned causal expression contract and
  documented CLI/schema/artifact protocols. Reviewed in-repository factors are source
  contributions; arbitrary Python plugin discovery and internal monkey-patching are not public APIs.
