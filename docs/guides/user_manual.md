# BFBT user manual

[简体中文](user_manual.zh-CN.md)

This manual covers local cross-sectional factor research and formal backtesting on Binance USDⓈ-M
perpetual futures. Start with the [beginner tutorial](beginner_tutorial.md) if you have not yet
produced a complete report.

## 1. Scope and safety boundary

BFBT is an offline research system, not a live-order application.

- It uses public Binance market data and requires no API key.
- It contains no exchange-account client and does not access balances or private order endpoints.
- It supports Binance USD-M, USDT-margined perpetual contracts.
- Base facts include one-minute trade bars, mark bars, funding, and contract metadata; higher
  intervals are derived causally.
- It supports point-in-time universes, registered cross-sectional factors, diagnostics, long/short
  portfolios, next-bar fills, fees, slippage, funding, mark valuation, chunked execution, and
  immutable publication.
- Download, normalization, and Catalog commands are available, but a project preparation script is
  still responsible for combining custom partitions into an exact `DatasetSnapshotManifest`.
- Full-market annual runs are capacity workloads, not the normal path on a low-memory machine.

Historical simulation is not investment advice or evidence of future performance.

## 2. Data and result identities

Formal runs use an immutable chain rather than an arbitrary folder of CSV files:

```text
Binance archive/REST
  → Raw files + RawObjectManifest
  → normalized Parquet + QualityReport + PartitionManifest
  → DuckDB Catalog
  → DatasetSnapshot with exact data versions and partitions
  → bfbt run
  → immutable run artifacts
```

Keep these identities distinct:

- `schema_version`: field, type, and semantic contract;
- fact-table `dataset_version`: derived from source checksums, normalization code, and parameters;
- `DatasetSnapshot.dataset_version`: the exact combination of bars, mark bars, funding, and
  contracts used by a formal run.

Formal execution rejects `latest`. Any change to data, resolved settings, source state, or the
dependency environment produces a different identity. All intervals use UTC left-closed,
right-open semantics: `[start, end)`. Supply `Z` or an explicit UTC offset.

## 3. Installation

```bash
cd /path/to/bfbt
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
bfbt --help
```

For later terminals, only `cd` into the repository and activate `.venv`. Generated data belongs
under `data/backtest/` and is excluded from Git.

## 4. Shortest complete workflow

After following the beginner tutorial, the local layout is:

```text
data/backtest/
├── datasets/tutorial/
├── catalogs/tutorial.duckdb
├── workspaces/tutorial/{configs,logs}/
├── runs/<run_id>/
└── reports/<run_id>/
```

### Prepare existing Raw data

```bash
DATA_ROOT=data/backtest
DATASET_ROOT="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
REPORT_ROOT="$DATA_ROOT/reports"

python tests/live/prepare_real_backtest_smoke.py \
  "$DATASET_ROOT" "$DATASET_ROOT" \
  --database "$DB" \
  --config-root "$CONFIG_ROOT" \
  --runs-root "$RUN_ROOT"
```

The preparation script re-verifies Raw sizes and SHA-256 values without downloading again.

### Run with an exact snapshot

```bash
DATASET_ID=binance-usdm-real-e2e-smoke-2026-06
DATASET_VERSION=live-smoke-REPLACE_WITH_EXACT_VALUE

bfbt run \
  "$DATASET_ID" "$DATASET_VERSION" momentum \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes \
  | tee "$LOG_ROOT/run-momentum.log"
```

Success prints `status=succeeded`, a globally unique `run_id`, and its path below
`data/backtest/runs/`.

### Validate and rebuild reports

```bash
RUN_ID=a09-REPLACE_WITH_EXACT_VALUE

python tests/live/validate_real_backtest_smoke.py "$RUN_ROOT/$RUN_ID"
bfbt performance inspect "$RUN_ID" --output-root "$RUN_ROOT"

mkdir -p "$REPORT_ROOT/$RUN_ID"
bfbt report "$RUN_ID" \
  --output-root "$RUN_ROOT" \
  --output "$REPORT_ROOT/$RUN_ID/report.html"
```

New reports publish default English, explicit `.en.html`, and independent `.zh-CN.html` files.
Rebuilding verifies the source artifact and writes outside its immutable run directory.

## 5. Configuration files

Templates live under `configs/`. A `null` value means that an economic decision is unresolved; do
not treat a draft as run-ready.

```bash
bfbt config validate \
  --data configs/data.yaml \
  --universe configs/universe.yaml \
  --factor configs/factor.yaml \
  --backtest configs/backtest.yaml \
  --run-ready
```

Use `bfbt config show` to inspect defaults and stable resolved paths.

### Data configuration

Important fields include the one-minute base interval, UTC core start/end, allowed derived
intervals, storage paths, partition-quality thresholds, and
`source.allow_authenticated_endpoints=false`.

### Universe configuration

The universe is rebuilt at every schedule timestamp. Filters include listing age, minimum history,
rolling quote volume, rolling missingness, and explicit exclusions.

Use historical contract snapshots only when they really cover the backtest period. If you have
current exchange information but historical bars, configure:

```yaml
point_in_time:
  enabled: true
  use_contract_snapshots: false
  use_first_last_valid_bar: true

filters:
  trading_status_only: false
```

This derives historical listing boundaries from valid bars instead of backfilling today's snapshot.

### Factor configuration

```bash
bfbt research list-factors
```

Built-in families include momentum, reversal, realized volatility, quote volume, active-buy ratio,
and Amihud illiquidity, together with additional registered factors. Preprocessing can apply
quantile winsorization, z-scores, and ranks in the configured order. Windows, preprocessing, and
compute intervals enter the factor fingerprint. Labels are evaluation inputs and never factor
inputs.

### Backtest configuration

A formal run must resolve its interval and data version, fee/slippage models, funding policy,
output root, and performance budget. Portfolio construction supports quantiles or fixed counts with
equal, score, or inverse-volatility weighting.

Execution uses next-bar-open fills. `signal_delay_bars=1` prevents a signal from filling on the bar
that produced it. Funding policies are:

- `error`: fail when required funding is missing;
- `exclude_symbol`: remove symbols without funding inputs;
- `assume_zero`: accept the explicit bias of treating missing funding as zero.

Valuation can use `mark_close` or `trade_close`. Mark valuation requires mark bars that cover the
core interval and execution tail.

## 6. Acquiring public data

No command in this section needs an API key. Start with one symbol and one day or month; use one
worker on a low-memory host.

### Initialize a Catalog

```bash
DATA_ROOT=data/my-dataset
RAW_ROOT="$DATA_ROOT/raw"
RAW_MANIFESTS="$DATA_ROOT/manifests/raw"
DB="$DATA_ROOT/catalog.duckdb"

bfbt catalog init --database "$DB"
bfbt catalog info --database "$DB"
```

### Plan and synchronize archives

```bash
bfbt data archive-plan bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly

bfbt data archive-sync bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly --workers 1 \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"

bfbt data archive-sync mark_bars BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --interval 1m --frequency monthly --workers 1 \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"

bfbt data archive-sync funding BTCUSDT \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --frequency monthly --workers 1 \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"
```

Archive synchronization verifies official checksums, ZIP CRCs, and local hashes. Inspect existing
coverage without network access with `bfbt data archive-coverage`.

Recent data can be paged through `rest-klines` and `rest-funding`; each response is stored as an
immutable Raw JSON object. Public metadata snapshots are available through:

```bash
bfbt data snapshot exchange-info \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" --database "$DB"
bfbt data snapshot funding-info \
  --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" --database "$DB"
```

An exchange-information snapshot is true only at its collection time.

### Normalize one batch

```bash
bfbt data normalize bars \
  "$RAW_MANIFESTS/archive-bars-BTCUSDT-1m-monthly-2025-01.json" \
  --raw-root "$RAW_ROOT" \
  --normalized-root "$DATA_ROOT/normalized" \
  --partition-manifest-root "$DATA_ROOT/manifests/partitions" \
  --quality-root "$DATA_ROOT/quality" \
  --database "$DB"
```

Normalization re-verifies Raw input, parses the schema, applies quality gates, and atomically
publishes Parquet, a quality report, and a partition manifest. For multiple symbols or months,
create one normalization release with all Raw object IDs and checksums before processing bounded
batches. Do not merge batches that independently invented different dataset versions.

Preview normalized data with:

```bash
bfbt data normalized-scan bars DATASET_VERSION \
  2025-01-01T00:00:00Z 2025-01-02T00:00:00Z \
  --interval 1m --columns open_time,symbol,close \
  --normalized-root "$DATA_ROOT/normalized" \
  --database "$DB" --verify-hashes
```

### Build a DatasetSnapshot

A project preparation script must select exact versions for bars, mark bars, funding, and
contracts; bind partition and quality identities; cover all warmup and fill tails; write the
snapshot; register it in the Catalog; and produce the four configuration files. Use
`tests/live/prepare_real_backtest_smoke.py` as the small real-data reference.

## 7. Formal artifacts

```bash
bfbt run DATASET_ID DATASET_VERSION FACTOR_NAME \
  --database /path/to/catalog.duckdb \
  --data-config /path/to/data.yaml \
  --universe-config /path/to/universe.yaml \
  --factor-config /path/to/factor.yaml \
  --backtest-config /path/to/backtest.yaml \
  --verify-hashes
```

Successful runs contain manifests, resolved settings, environment and run metadata, metrics,
warnings, reports, and Parquet tables for universe membership, factor values, targets, fills,
positions, costs, and returns. The manifest binds source state, Python and dependencies,
configuration, data snapshot, schemas, factor identity, and every artifact hash. Never edit a
published run. Failed formal runs publish a terminal failure manifest and diagnostic evidence.

## 8. Reading reports and tables

The Event report explains factor meaning, selection, execution, terminal positions, and headline
metrics. Its equity chart links selected timestamps to positions, fills, risk events, exposure,
turnover, and costs. A terminal position is a real position at the final valuation timestamp; BFBT
does not invent a forced-close fill for presentation.

Use the report and `metrics.json` first, then inspect tables by question:

- `universe`: eligibility and reason codes;
- `factor_values`: raw/processed values and invalidity reasons;
- `targets`: desired direction and weight;
- `trades`: signal time, fill time, reference price, slipped price, and turnover;
- `positions`: quantity, notional, actual weight, mark, and unrealized PnL;
- `costs`: fees, slippage, funding cash flow, and total cost;
- `returns`: gross return, cost components, net return, equity, drawdown, exposure, and turnover.

Fees and slippage are recorded as positive costs and deducted from net return. Funding income is
positive and funding expense is negative. Treat annualized metrics from short intervals as an
operational check, not strategy evidence.

## 9. Preview commands

Preview commands debug one stage without publishing a formal run:

- `bfbt data resample-preview`;
- `bfbt universe preview`;
- `bfbt research preview` for labels, IC, quantiles, coverage, and turnover;
- `bfbt backtest preview` for targets, fills, positions, costs, and returns.

Supply explicit `history_start` and, where required, `future_end`. BFBT does not silently download
missing history.

## 10. Performance and bounded memory

Use `in_memory` only for small fixtures and equivalence checks. Formal workloads should use
`chunked`, which computes time blocks while carrying the chronological economic state.

```yaml
performance:
  mode: chunked
  chunk_interval: 1d
  max_input_rows_per_chunk: 250000
  max_incremental_rss_mib: 512
  collect_diagnostics: true
```

Plan and inspect execution with:

```bash
bfbt performance plan \
  2025-01-01T00:00:00Z 2025-02-01T00:00:00Z \
  --chunk-interval 1d --overlap-seconds 90000
bfbt performance inspect RUN_ID --output-root /path/to/runs
```

Clean abandoned workspaces with a dry run first:

```bash
bfbt performance clean-work \
  --output-root /path/to/runs \
  --older-than-hours 24 --dry-run
```

Use `--apply` only after confirming the exact targets. Published runs are not deletion candidates.

## 11. Catalog operations

```bash
bfbt catalog info --database /path/catalog.duckdb
bfbt catalog coverage bars DATASET_VERSION --database /path/catalog.duckdb
bfbt catalog resolve DATASET_ID SNAPSHOT_VERSION --database /path/catalog.duckdb
bfbt catalog rebuild /path/to/manifests --database /path/to/new-catalog.duckdb
```

The Catalog is a rebuildable control plane, not the primary market-data store. Rebuild validates
Raw, partition, snapshot, and run references before atomic replacement.

## 12. Troubleshooting

- `config validate --run-ready`: resolve missing dates, data identity, fees/slippage, and mutually
  exclusive portfolio fields.
- `no partitions overlap`: inspect exact snapshot version, interval, coverage, and Catalog bounds.
- `dataset member ... does not cover`: extend bars through warmup and the next-fill tail.
- `target input has no rows`: inspect universe reason codes and cross-sectional size.
- Missing funding: obtain the data; use `exclude_symbol` or `assume_zero` only with explicit bias.
- Binance 403/451, DNS, or timeout: retry public downloads from a reachable network; do not add an
  account key.
- Changed run ID: inspect source changes, untracked files, dependencies, resolved settings, and the
  data snapshot.
- Low memory: reduce symbols/time, shorten chunks, lower rows per chunk, and avoid concurrent
  normalization or formal execution.

## 13. Fast Matrix portfolio research

Fast Matrix is the research backend for conventional portfolio paths, not a replacement for a
formal Event Engine run. It requires versioned market Parquet, a normalized `TargetSchedule`, exact
rebalance times, a parent signal-snapshot hash, and a research configuration:

```bash
bfbt research matrix-run targets.parquet bars.parquet \
  --rebalance-times rebalance_times.json \
  --parent-manifest-sha256 <sha256> \
  --market-identity <dataset-identity> \
  --backtest-config backtest.yaml
```

Results publish under `data/backtest/research_runs/fm-*/` and are labelled as research. Dynamic
margin, risk exits, cooldowns, and event arbitration fail closed instead of being approximated.
After human selection, promote a candidate to an Event Engine configuration and run the formal
workflow.

Rebuild a successful study's searchable reports with:

```bash
bfbt research study-report data/backtest/research_studies/<study_id> \
  --matrix-runs-root data/backtest/research_runs
```

Outputs include a navigation-only `report.html`, `quick_research.html`, `fast_matrix.html`, and one
enhanced detail page under `fast_matrix_reports/` for every referenced `fm-*` run. Rebuilding these
views never overwrites immutable research artifacts.

## 14. Further reading

- [Real end-to-end acceptance](../acceptance/real_e2e.md)
- [Configuration reference](../reference/configuration.md)
- [Data contract](../reference/data_contract.md)
- [Data management](../reference/data_management.md)
- [Interface reference](../reference/interfaces.md)

Use the locally installed CLI as the source of truth for current options:

```bash
bfbt --help
bfbt data --help
bfbt run --help
```
