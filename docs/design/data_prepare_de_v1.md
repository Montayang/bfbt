# DE-v1 local data preparation

DE-v1 turns BFBT's existing data components into one bounded workflow for an individual
researcher. It is not a second data engine and does not introduce a scheduler, service database,
feature store, or automatic market-data refresh.

## Contracts

`ResearchDataRequirement` freezes the market, symbols, core interval, stored interval, derived
intervals, required fact types, warmup, universe history, future labels, execution tail, purpose,
and exact version pins. Its effective interval is:

```text
required_start = core_start - max(factor_warmup, universe_history) * base_interval
required_end   = core_end   + max(label_future, execution_tail) * base_interval
```

`DataPlan` is a canonical, content-hashed read-only result. It reports compatible exact versions,
verified local archives, missing remote objects, resource ranges, issues, and every action's
authorization class. It never resolves a floating `latest` version. Multiple compatible versions
require an explicit pin.

`DataReadiness` binds one exact `DatasetSnapshot` to coverage, quality references, applicable
research layers, warnings, scan size, and a minimal lineage graph. Engines consume this identity;
they do not download or repair data.

## Execution and recovery

`DataPreparationService` records `acquire_raw`, `normalize`, `publish_snapshot`, and `readiness`
under `data/backtest/jobs/de1-*/job.json`. A succeeded step is not repeated after restart. Raw
checksum checks, immutable publication, quality gates, Catalog references, and Parquet hashes stay
owned by the existing lower layers.

Network and writes are separate approvals. Current `exchangeInfo` is never silently treated as
historical contract state; without a compatible historical contracts version the plan fails
closed, unless the requirement explicitly accepts the current-snapshot limitation.

Derived intervals are deterministic virtual derivations over the pinned base interval. DE-v1
records that lineage but does not materialize a general feature store.

## Boundaries

- Planning and inspection perform no network calls and do not create the workspace.
- A failed quality gate cannot publish a DatasetSnapshot.
- Raw and normalized facts remain immutable; revisions produce different identities.
- Real downloads and research/backtest execution are not implied by a plan.
- Second bars, trades, aggregate trades, order books, and enterprise orchestration remain outside
  DE-v1.

