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
- No pytest suite has yet been authorized or run against the standalone repository. Therefore
  `321 passed` is historical evidence, not a claim about the current standalone HEAD.
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

