# Current project state

Updated: 2026-10-03.

## Repository identity

- Brand: BFBT — an auditable research and backtesting framework for crypto perpetual futures.
  Package, import namespace, and CLI: `bfbt`, version `0.1.0`.
- Current market/data compatibility: public historical data for Binance USDⓈ-M perpetual futures;
  this is a factual support boundary, not part of the product name or a claim of affiliation.
- Standalone repository root; no parent-repository or `bianbot` runtime dependency.
- Initial standalone commit: `2f3a4d2e0170cffa0c0d121e3654b89b1882b32a`, migrated from
  the former mixed-repository `backtest/` snapshot at
  `d30e27d65b9fef1e844390d3d7b43f0deb0a2acf`.
- Local runtime data and generated artifacts are excluded from Git under `data/backtest/`.

## Implemented architecture

- A01-A11: configuration, schemas, catalog, ingestion, normalization, resampling, point-in-time
  universe, factor research, portfolio economics, metrics, artifacts, chunking, and reports.
- A12-A18: Event/V2 configuration and artifacts, exact and historical Rank, incremental sizing,
  margin, risk exits, unified event arbitration, and formal execution.
- A19-A25: recoverable low-memory chunk workers, streaming publication, full-market Rank descent,
  single-position exits, and interactive audit reports.
- A26-A30: intrabar EMA factors, reusable analysis/signal snapshots, sparse replay, parameter
  sweep, per-symbol crossover instructions, and missing-bar valuation.
- A31-A35: Fast Matrix capability planning, TargetSchedule, columnar economics, funding/mark,
  chunked checkpoints, research artifacts, and explicit Event promotion.
- A36-A40: sampled-mean factors, Event parameter studies, activated trailing exits, rolling-margin
  state, complete trade/position audit navigation, a verified offline Agent Showcase thin slice,
  full BFBT identity migration, and independent English/Simplified-Chinese HTML outputs.
- A41: Fast Matrix multi-candidate batches share one market preparation, recurse only at joint
  rebalance/funding boundaries, value complete intervals together, and retain sparse non-zero
  holdings while preserving candidate-level economics, audits, checkpoints, and identities.
- A42: DE-v1 exposes versioned research-data requirements, deterministic read-only plans, explicit
  network/write actions, recorded resumable preparation, exact snapshot readiness, and minimal
  lineage while reusing the existing local data layers.
- A43: the general supervised Agent control plane provides strict natural-language intent,
  semantic/data/backend/cost preflight, plan-bound grants, causal expression factors, resumable
  evidence stages, human Matrix promotion, Event source checks, and claim-level citations.
- A44: hash-locked universal runtime/development environments, immutable GitHub Action references,
  reviewed dependency updates, tag-gated GitHub Releases, inspected wheel/sdist/checksums, and
  explicit bilingual release and extension-compatibility policies complete the open-source
  packaging contract.
- A45: four frozen one-shot model submissions have deterministic, result-blind factor contracts and
  isolated Event account state machines over one shared chronological market scan. Their source,
  dataset and submission hashes, causal timing, costs, recovery parts and risk-event audit trail are
  explicit; formal result production remains a separate background operation.
- The Quick Research registry also includes 14 source-pinned Qlib/`ta` trend and momentum factors. Exact
  source formulas remain distinct from BFBT adaptations; formula windows are literal source-bar
  counts, gaps reset history, and invalid or zero-denominator inputs fail closed.

The intended workflow is:

```text
Quick Research -> Fast Matrix -> Event/V2 formal run
```

V1 remains for compatibility and historical reproduction, not for new daily strategy work.

## Research and strategy records

- Factor research registry and promotion rules: `docs/research/`.
- Stable strategy identities and formal run mappings: `strategies/`.
- Current recorded families include full-market Rank-descent variants R1-R6, R5-T4 trailing and
  rolling-margin variants (including independent H1 and H2 sampling identities), and the C1
  full-market EMA crossover family.
- Formal run files and HTML reports are local generated assets; Git stores their identities,
  specifications, and recorded summaries, not the artifacts themselves.
- `R5-T4-H2-ROLLING` May, June, and July formal runs are complete and registered under
  `strategies/full_market_rank_descent_long/`; their immutable environments record commit
  `3b0a32e` with `git_dirty=true`, which remains an explicit audit qualification.
- The 1x-leverage May presentation identity `R5-T4-H2-L1-ROLLING` is complete as immutable run
  `a17-ca0c6d168e37c07b06239452`; its environment records clean commit `bf136b5`, and its verified
  bilingual reports are the current published GitHub Pages Event example.

## Verification baseline

- The former mixed-repository cut point recorded `321 passed` before standalone extraction.
- The standalone repository changed path semantics and added a standalone Git-fingerprint test.
- The standalone offline suite completed on 2026-08-29 against HEAD `69e8588` with only maintainer
  documentation changes uncommitted: `322 passed in 35.23s` on Python 3.12.3 and pytest 8.4.2.
- The earlier `321 passed` result remains migration history; `322 passed` is the current verified
  independent-repository migration baseline.
- The H2 runner/identity/result registration branch completed the full offline suite on
  2026-08-30: `325 passed in 37.65s` on Python 3.12.3 and pytest 8.4.2.
- The Showcase implementation candidate completed the full offline suite on 2026-09-02:
  `331 passed in 21.58s` on Python 3.12.3 and pytest 8.4.2. Its focused A39 suite passed 6 tests,
  and the real local H2 preparation verified all three immutable runs with one expected provenance
  warning and no failures.
- The BFBT public-release candidate completed the full offline suite on 2026-09-02:
  `335 passed in 37.81s` on Python 3.12.3 and pytest 8.4.2. Its focused report, Showcase, and
  language-contract suite passed 48 tests, and the real derived Showcase rebuild verified all
  three immutable H2 runs. The editable install, CLI entry point, dependency check, and distributable
  wheel were also verified; the wheel contains `bfbt` only and no legacy Python package.
- Declared Python 3.10 support uses a small standard-library compatibility surface for `StrEnum`
  and UTC. GitHub's 3.10/3.12 matrix compiles every source, test and script before running the full
  offline suite, making the supported runtimes—not a newer parser's grammar emulation—the release
  gate.
- Migration-time static checks covered Python AST parsing, TOML/YAML parsing, shell syntax,
  imports, project-root discovery, Markdown links, secret/path scanning, and Git integrity.
- The open-source trend/momentum factor implementation passed 40 focused formula, recursive-reference,
  warmup, causality, gap, parameter, and registry tests, followed by the complete offline suite:
  `360 passed in 38.24s` on 2026-09-05. No Quick Research or backtest was run by this verification.
- Fast Matrix phase-two focused A32-A34/A41 verification passed 8 tests on 2026-09-29, followed by
  the complete offline suite (`361 passed in 24.72s`). Its fixed
  offline 6-candidate synthetic benchmark measured 4.488 s for independent execution and 0.296 s
  for joint execution (15.14× on that shape).
- DE-v1 A42 focused verification passed 7 tests, its Catalog/normalization regression set passed
  23 tests, and the complete offline suite passed `368 passed in 39.73s` on 2026-09-29. No network
  request, data download, research run, or formal backtest occurred.
- A43 focused verification passed 10 tests; the combined factor/DE/Agent regression set passed 34
  tests, and the complete offline suite passed `378 passed in 24.23s` on 2026-09-29. No model API,
  network request, data download, research execution, or formal Event run occurred.
- A44 focused verification passed 3 tests and the complete offline suite passed on both supported
  runtimes: `381 passed in 24.28s` on Python 3.10 and `381 passed in 23.71s` on Python 3.12 on
  2026-09-30. Fresh hash-locked runtime and release environments,
  editable and wheel-installed CLI startup, warning-free wheel/sdist construction, Twine metadata,
  archive contents and SHA-256 checksums were verified. Dependency packages were downloaded only
  for this packaging acceptance; no market data, research run or formal Event run occurred.
- A45 focused verification passed 12 tests and the complete offline suite passed `393 passed` on
  2026-10-03 before the formal benchmark launch. These tests used synthetic fixtures and did not
  access the evaluation-period results.
- The corrected A45 formal benchmark completed four clean-source Event runs on commit `2b1ca8f`.
  ChatGPT, Grok, Kimi and Claude respectively map to immutable runs
  `evt-a01175782a0fa6997144d0da`, `evt-c7bdf2892da257893f46be6c`,
  `evt-498d8f170a3ee9272dbbb66c` and `evt-ed4f9aeb18febee2690b12d4`. The tracked result record retains
  the frozen ranking, attribution and audit qualifications; large artifacts and reports remain
  ignored local evidence.

## Known boundaries

- Market-history downloads use public Binance archive/market-data endpoints; no authenticated
  trading endpoint is supported.
- Full exchange liquidation tiers, ADL, order-book queueing, and tick-level fills are outside the
  current execution model.
- Fast Matrix supports conventional target-weight paths and linear economics. Path-dependent risk
  state and event arbitration require Event/V2.
- Minute-level full-market schedules can create very large target sets and extreme turnover; they
  require an explicit cost warning before execution.

## Data subsystem

- The repository separates the data path from research and execution through immutable Raw
  objects, normalized Parquet, quality gates, Catalog identities and exact DatasetSnapshots.
- DE-v1 now gives ordinary users one read-only `data plan/inspect` surface and one recorded
  `data prepare` path over the existing lower layers. It is not an independently operated data
  platform, scheduler, or feature service.
- The accepted long-term target is a low-maintenance, local-first preparation workflow for an
  individual researcher, not an enterprise data stack. `DATA_ENGINE_PLAN.md` freezes that boundary
  and the implemented DE-v1 planning/prepare/readiness sequence.
- Offline A42 evidence is complete. The optional D4 controlled real-public-data preparation record
  still requires separate authorization and is not substituted by unit tests.
- Binance USD-M archive Klines remain minute-or-coarser in BFBT. Futures `trades`/`aggTrades` and
  derived second bars are demand-gated future work, not part of DE-v1.

## AI Agent readiness

- The general supervised control plane is implemented. An external AI Agent translates natural
  language into the public strict contract; BFBT validates ambiguity, semantics, data, backend,
  costs, permissions, stage evidence, human promotion, and result claims.
- BFBT deliberately does not embed a particular LLM, run generated shell/Python, automatically
  choose Matrix candidates, or turn a plan into implicit authority.
- The recorded workflow is a cross-stage evidence protocol. Generic PID/heartbeat/safe-cancel
  process management remains an AG05 enhancement; DE/Event retain their existing recovery paths.
- The durable gap register and implementation order are maintained in
  `docs/maintainer/AI_AGENT_READINESS.md`.
- The bounded Showcase remains a curated presentation surface; A43, not Showcase, is the general
  Agent contract and workflow acceptance.

## Showcase surface

- Public discovery is now a self-guided tour of the reports produced by Quick Research, Fast Matrix,
  and the Event Engine. The root README uses bilingual report-surface previews rather than strategy
  performance from selected runs; `showcase/README.md` is English-first with an independent Chinese
  sibling.
- The H2 three-month Showcase remains an optional verified case for machines that already hold its
  exact local runs. It is not the default fresh-checkout experience.
- `bfbt doctor` performs stable read-only runtime, storage, catalog, intent, artifact, provenance,
  and optional loopback-port checks.
- `bfbt showcase inspect/build/prepare` verifies exact immutable runs and publishes only derived
  output under `data/backtest/showcases/`.
- Showcase preflight proves each run's period, factor parameters, strategy name, and shared frozen
  economic identity match the ResearchIntent before rendering.
- The initial H2 three-month Showcase exposes all dirty-provenance and funding-warning qualifications,
  includes loss and profit months, and derives opening margin directly from verified trades.
- The English repository front door and standalone Chinese README reflect A01-A44 and include CI,
  contribution, security, changelog, MIT license, and Binance-independence disclosures.
- English-first contribution, security, changelog, documentation-map, and Showcase-preview surfaces
  link to independent Simplified-Chinese counterparts where applicable.
- The public documentation map, no-programming Agent guide, beginner tutorial, user manual,
  custom-factor tutorial, and Showcase guide use unsuffixed English entry points with independent
  `.zh-CN.md` siblings.
- The no-programming route now starts from a fresh Ubuntu server. The idempotent
  `scripts/install_ubuntu.sh` helper installs the local runtime, prepares ignored workspace
  directories, and runs the read-only doctor without downloading data or starting research.
- Human-facing generated HTML publishes default English, explicit English, and independent
  Simplified-Chinese files. Machine-readable artifacts remain language-neutral and single-copy.
