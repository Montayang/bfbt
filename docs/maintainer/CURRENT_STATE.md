# Current project state

Updated: 2026-08-29.

## Repository identity

- Package: `bianbt`, version `0.1.0`.
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
- A36-A38: sampled-mean factors, Event parameter studies, activated trailing exits, rolling-margin
  state, and complete trade/position audit navigation.

The intended workflow is:

```text
Quick Research -> Fast Matrix -> Event/V2 formal run
```

V1 remains for compatibility and historical reproduction, not for new daily strategy work.

## Research and strategy records

- Factor research registry and promotion rules: `docs/research/`.
- Stable strategy identities and formal run mappings: `strategies/`.
- Current recorded families include full-market Rank-descent variants R1-R6, R5-T4 trailing and
  rolling-margin variants, and the C1 full-market EMA crossover family.
- Formal run files and HTML reports are local generated assets; Git stores their identities,
  specifications, and recorded summaries, not the artifacts themselves.

## Verification baseline

- The former mixed-repository cut point recorded `321 passed` before standalone extraction.
- The standalone repository changed path semantics and added a standalone Git-fingerprint test.
- The standalone offline suite completed on 2026-08-29 against HEAD `69e8588` with only maintainer
  documentation changes uncommitted: `322 passed in 35.23s` on Python 3.12.3 and pytest 8.4.2.
- The earlier `321 passed` result remains migration history; `322 passed` is the current verified
  independent-repository baseline.
- Migration-time static checks covered Python AST parsing, TOML/YAML parsing, shell syntax,
  imports, project-root discovery, Markdown links, secret/path scanning, and Git integrity.

## Known boundaries

- Market-history downloads use public Binance archive/market-data endpoints; no authenticated
  trading endpoint is supported.
- Full exchange liquidation tiers, ADL, order-book queueing, and tick-level fills are outside the
  current execution model.
- Fast Matrix supports conventional target-weight paths and linear economics. Path-dependent risk
  state and event arbitration require Event/V2.
- Minute-level full-market schedules can create very large target sets and extreme turnover; they
  require an explicit cost warning before execution.

## AI Agent readiness

- The deterministic research and Event engines, immutable identities, reports, and CLI provide a
  strong execution foundation for supervised Agent use.
- A general no-code Agent control plane is not implemented. In particular, there is no versioned
  ResearchIntent, authorization action contract, safe factor-expression boundary, unified research
  orchestrator, or recorded background-job service.
- The durable gap register and implementation order are maintained in
  `docs/maintainer/AI_AGENT_READINESS.md`.
