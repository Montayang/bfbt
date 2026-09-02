# BFBT beginner tutorial

[简体中文](beginner_tutorial.zh-CN.md)

This tutorial has one goal: help a first-time user produce a complete backtest report from real
Binance perpetual-futures data.

You do not need an API key. BFBT does not connect to a trading account or place real orders. Run
the baseline once before changing the strategy, factor, or data layout. Experienced users can go
directly to the [user manual](user_manual.md).

## What you will produce

The completed run directory contains:

- `report.html`: the default English report; `report.en.html` is the explicit English copy and
  `report.zh-CN.html` is the independent Simplified-Chinese report;
- `metrics.json`: return, drawdown, Sharpe ratio, and other summary metrics;
- `tables/trades.parquet`: every simulated fill;
- `tables/positions.parquet`: minute-level positions;
- `tables/returns.parquet`: minute-level returns and equity;
- `manifest.json`: the exact data, configuration, code, and artifact hashes used by the run.

The tutorial uses eight contracts (BTC, ETH, BNB, SOL, XRP, DOGE, ADA, and LINK), one-minute trade
and mark-price bars, and observed funding. It backtests days 8–15 of one month. The sample strategy
ranks 24-hour momentum every four hours, holds two longs and two shorts, fills at the next one-minute
open, and charges 4 bps fees plus 2 bps slippage. These settings verify the workflow; they are not a
strategy recommendation.

## 1. Open the repository

```bash
cd /path/to/bfbt
pwd
```

The second command should print `/path/to/bfbt`. Run all remaining commands from this directory.

## 2. Start the Python environment

If `.venv` already exists:

```bash
source .venv/bin/activate
bfbt --help
```

For a first installation:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
bfbt --help
```

Continue when the help output includes commands such as `run`, `data`, and `catalog`.

## 3. Choose a data source

Use exactly one of the following paths.

### Path A: reuse existing tutorial data

```bash
DATA_ROOT=data/backtest
SOURCE="$DATA_ROOT/datasets/tutorial"
TARGET="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
REPORT_ROOT="$DATA_ROOT/reports"

test -d "$SOURCE/raw" && echo "data found; continue"
```

If the last line prints the message, continue to step 4. Otherwise use Path B.

### Path B: download a small public sample (recommended for a fresh checkout)

This path accesses Binance public market-data services but requires no API key.

```bash
DATA_ROOT=data/backtest
SOURCE="$DATA_ROOT/datasets/tutorial"
TARGET="$DATA_ROOT/datasets/tutorial"
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
REPORT_ROOT="$DATA_ROOT/reports"
RAW_ROOT="$SOURCE/raw"
RAW_MANIFESTS="$SOURCE/manifests/raw"

mkdir -p "$DATA_ROOT/catalogs" "$LOG_ROOT"
bfbt catalog init --database "$DB"
```

Download June 2026 for all eight symbols. If you choose another complete month, change all three
date ranges together so trade bars, mark bars, and funding cover the same period.

```bash
for SYMBOL in BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT DOGEUSDT ADAUSDT LINKUSDT
do
  echo "downloading $SYMBOL trade bars"
  bfbt data archive-sync bars "$SYMBOL" \
    2026-06-01T00:00:00Z 2026-07-01T00:00:00Z \
    --interval 1m --frequency monthly --workers 1 \
    --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
    --database "$DB" || break

  echo "downloading $SYMBOL mark bars"
  bfbt data archive-sync mark_bars "$SYMBOL" \
    2026-06-01T00:00:00Z 2026-07-01T00:00:00Z \
    --interval 1m --frequency monthly --workers 1 \
    --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
    --database "$DB" || break

  echo "downloading $SYMBOL funding"
  bfbt data archive-sync funding "$SYMBOL" \
    2026-06-01T00:00:00Z 2026-07-01T00:00:00Z \
    --frequency monthly --workers 1 \
    --raw-root "$RAW_ROOT" --manifest-root "$RAW_MANIFESTS" \
    --database "$DB" || break
done
```

`downloaded` and `skipped` are both successful outcomes. A rerun will reuse verified downloads.
Then fetch public contract metadata:

```bash
bfbt data snapshot exchange-info \
  --raw-root "$RAW_ROOT" \
  --manifest-root "$RAW_MANIFESTS" \
  --database "$DB"
```

Confirm that each market-data type has eight manifests:

```bash
find "$RAW_MANIFESTS" -name 'archive-bars-*.json' | wc -l
find "$RAW_MANIFESTS" -name 'archive-mark_bars-*.json' | wc -l
find "$RAW_MANIFESTS" -name 'archive-funding-*.json' | wc -l
```

Do not continue unless all three commands print `8`.

## 4. Prepare normalized backtest data

```bash
mkdir -p "$LOG_ROOT"
python tests/live/prepare_real_backtest_smoke.py "$SOURCE" "$TARGET" \
  --database "$DB" \
  --config-root "$CONFIG_ROOT" \
  --runs-root "$RUN_ROOT" \
  | tee "$LOG_ROOT/prepare.log"
```

The preparation command verifies sizes and SHA-256 hashes, converts ZIP/JSON inputs to normalized
Parquet, checks data quality, builds the DuckDB catalog, and writes four ready-to-use configuration
files. Do not run a second preparation process in parallel.

Successful output ends with values similar to:

```text
dataset_id=binance-usdm-real-e2e-smoke-2025-01
dataset_version=live-smoke-xxxxxxxxxxxxxxxxxxxxxxxx
database=/.../data/backtest/catalogs/tutorial.duckdb
config_root=/.../data/backtest/workspaces/tutorial/configs
runs_root=/.../data/backtest/runs
```

Copy the complete `dataset_id` and `dataset_version`. If the source contains more than one complete
month, add an explicit option such as `--month 2026-01`. If a previous incomplete preparation used
the target directory, choose a new target rather than deleting published data:

```bash
TARGET="$DATA_ROOT/datasets/tutorial-02"
DB="$DATA_ROOT/catalogs/tutorial-02.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial-02/configs"
```

## 5. Set the exact data identity

Replace both placeholders with the values printed in step 4:

```bash
DATASET_ID="binance-usdm-real-e2e-smoke-REPLACE_WITH_MONTH"
DATASET_VERSION="live-smoke-REPLACE_WITH_EXACT_VALUE"

echo "$DATASET_ID"
echo "$DATASET_VERSION"
```

## 6. Run the formal backtest

```bash
bfbt run \
  "$DATASET_ID" \
  "$DATASET_VERSION" \
  momentum \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes \
  | tee "$LOG_ROOT/run-momentum.log"
```

Success ends with:

```text
run_id=a09-xxxxxxxxxxxxxxxxxxxxxxxx
status=succeeded
run_path=/.../runs/a09-xxxxxxxxxxxxxxxxxxxxxxxx
publication=published
catalog=inserted
```

Save the exact run ID:

```bash
RUN_ID="a09-REPLACE_WITH_EXACT_VALUE"
echo "$RUN_ID"
```

For a failed run, read the message after `Run failed:` and inspect
`$LOG_ROOT/run-momentum.log` rather than relying on its final line.

## 7. Validate the completed artifact

```bash
python tests/live/validate_real_backtest_smoke.py \
  "$RUN_ROOT/$RUN_ID" \
  | tee "$LOG_ROOT/validate.log"
```

The command should emit JSON without an `AssertionError` or traceback. Confirm at least:

```text
"status": "succeeded"
"memory_budget_passed": true
"trades": 180
```

With a complete monthly input, `universe` and `factor_values` normally contain 1,344 rows,
`targets` contains 140 rows, and `trades` contains 180 rows. Position, cost, and return tables must
be non-empty. Exact values can vary by month; seven-day performance is not strategy evidence.

## 8. Open the report

```bash
echo "$RUN_ROOT/$RUN_ID/report.html"
test -f "$RUN_ROOT/$RUN_ID/report.html" && echo "report generated"
```

On a headless server, download the file through your editor or file browser and open it locally.
Never edit the report or another file inside the immutable run directory.

## 9. Check deterministic reuse

Run the command from step 6 again without changing code, data, or configuration. It should report
the same run ID together with:

```text
publication=already_published
catalog=already_registered
```

This confirms that identical inputs do not overwrite or duplicate a result. A changed run ID most
often means that tracked, untracked, or modified repository files changed the source fingerprint.

## 10. Read the four most important outputs

### `metrics.json`

```bash
cat "$RUN_ROOT/$RUN_ID/metrics.json"
```

Start with `total_return`, `ending_equity`, `max_drawdown`, and `sharpe_ratio`. Annualized ratios
from a seven-day sample are not reliable evidence of a strategy's quality.

### `report.html`

Use the interactive report to inspect the equity path and summary without reading Parquet directly.

### `tables/trades.parquet`

This table retains every simulated BUY and SELL. Fill timestamps must be later than their signal
timestamps.

### `tables/costs.parquet`

This table records fees, slippage, and funding. Non-zero values confirm that those costs entered the
economic path.

## 11. Change the strategy safely

After the baseline succeeds, prepare a new target directory or copy the working configuration. Do
not modify `runs/<run_id>/`. The most common files to change are:

```text
$CONFIG_ROOT/factor.json
$CONFIG_ROOT/universe.json
$CONFIG_ROOT/backtest.json
```

Examples include changing the momentum `lookback`, `rebalance_interval`, fee/slippage values, or the
`long_count` and `short_count` fields while retaining `construction=long_short_count`. A changed
configuration should create a new run ID.

Do not manually edit dataset snapshots, manifests, normalized Parquet, published run directories,
or any live-trading client from another project.

## Troubleshooting

### `bfbt: command not found`

```bash
cd /path/to/bfbt
source .venv/bin/activate
bfbt --help
```

### `No such file or directory`

Confirm that `pwd` is the repository root and inspect `SOURCE` and `TARGET` with `echo`.

### Missing Raw manifest in step 4

```bash
find "$SOURCE/manifests/raw" -type f | sort
```

Every symbol needs bars, mark bars, and funding, plus a
`rest-contracts-exchangeInfo-...json` manifest.

### Binance returns 403, 451, DNS, or timeout errors

This is a public-data network or regional-access problem, not a strategy error. Retry unchanged
from a network that can reach Binance public market-data services.

### `no partitions overlap the requested constraints`

`TARGET`, the data identity, and the configuration probably came from different preparation runs.
Use one target and the exact identity printed for it.

### `target input has no rows`

The universe filtered every symbol or the cross-section is too small. Restore the generated
tutorial configuration, prove the baseline, and then change one filter at a time.

### High memory use

Do not run two preparations or backtests concurrently. This tutorial already limits downloads to
one worker and the formal run to eight symbols, seven days, and one-day chunks.

### Do I need an API key?

No. The tutorial uses public archives and public exchange information only. Never add a trading
account key to a backtest configuration.

## Completion checklist

- [ ] `bfbt --help` works.
- [ ] Preparation prints exact `dataset_id` and `dataset_version` values.
- [ ] The formal run prints `status=succeeded`.
- [ ] Validation prints `memory_budget_passed=true` without an assertion failure.
- [ ] `report.html` exists.
- [ ] The identical rerun prints `already_published`.

After all six checks pass, continue with the [user manual](user_manual.md), the
[real acceptance record](../acceptance/real_e2e.md), the
[configuration reference](../reference/configuration.md), and the
[data contract](../reference/data_contract.md).
