# Add a cross-sectional factor using the Amihud implementation

[简体中文](custom_factor_tutorial.zh-CN.md)

This tutorial uses the built-in `amihud_illiquidity` factor as a complete reference for computing,
registering, testing, configuring, and running a new formula. Do not register Amihud a second time;
use a new stable name, implementation file, and tests for your own factor.

The workflow is offline, does not connect to an exchange account, and never places an order. Finish
the [beginner tutorial](beginner_tutorial.md) first if
`data/backtest/datasets/tutorial` does not exist. If you only need a different window or
preprocessing chain for an existing factor, edit its configuration instead of adding code.

## 1. Reference formula

The Amihud-style rolling illiquidity factor is:

```text
single-bar illiquidity = abs(log(close_t / close_t-1)) / quote_volume_t
factor value = mean(single-bar illiquidity over the latest N bars)
```

A larger value means that less quote volume accompanies a larger price change: the contract is
relatively less liquid. The formula uses only `close`, `quote_volume`, timestamps, and completeness
fields already present in tutorial bars. The example computes a 24-hour window once per hour while
the portfolio continues to rebalance every four hours.

## 2. Enter the environment

```bash
cd /path/to/bfbt
source .venv/bin/activate
bfbt research list-factors
```

The list should already contain `amihud_illiquidity` at version `v1`. If it does not, check your
branch and editable installation rather than duplicating the registration.

## 3. Study the computation

The implementation is in `src/bfbt/factors/illiquidity.py`. Its structure demonstrates timing,
contiguous windows, complete-bar checks, and finite-value handling required by a rolling factor:

```python
"""Amihud-style rolling illiquidity factor."""

from __future__ import annotations

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


def amihud_illiquidity_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
) -> pl.LazyFrame:
    value = definition.parameters.get("window")
    if not isinstance(value, str):
        raise FactorError("window must be a duration string")

    seconds = duration_seconds(value)
    base_seconds = duration_seconds(base_interval)
    if seconds % base_seconds:
        raise FactorError("window must be a multiple of base_interval")
    window = seconds // base_seconds

    log_return = (
        pl.col("close").log()
        - pl.col("close").shift(1).over("symbol").log()
    )
    first_time = pl.col("open_time").shift(window).over("symbol")
    contiguous = (
        pl.col("open_time").cast(pl.Int64)
        - first_time.cast(pl.Int64)
        == window * base_seconds * 1_000
    )

    return (
        bars.with_columns(log_return.alias("_log_return"))
        .with_columns(
            pl.when((pl.col("close") > 0) & (pl.col("quote_volume") > 0))
            .then(
                pl.col("_log_return").abs()
                / pl.col("quote_volume")
                * 1_000_000
            )
            .otherwise(None)
            .alias("_amihud")
        )
        .with_columns(
            pl.when(
                contiguous
                & (
                    pl.col("is_complete")
                    .cast(pl.Int64)
                    .rolling_sum(window + 1)
                    .over("symbol")
                    == window + 1
                )
                & (
                    pl.col("_amihud")
                    .is_finite()
                    .fill_null(False)
                    .cast(pl.Int64)
                    .rolling_sum(window)
                    .over("symbol")
                    == window
                )
            )
            .then(pl.col("_amihud").rolling_mean(window).over("symbol"))
            .otherwise(None)
            .alias("raw_value")
        )
        .select(
            pl.col("close_time").alias("timestamp"),
            "symbol",
            "raw_value",
        )
    )
```

The `1_000_000` scale improves readability without changing cross-sectional order. A factor
function returns exactly `timestamp`, `symbol`, and `raw_value`. Rolling work must be grouped by
symbol and reject gaps, incomplete bars, and non-finite inputs.

## 4. Register identity and dependencies

`src/bfbt/factors/registry.py` imports the function and registers it:

```python
"amihud_illiquidity": RegisteredFactor(
    "amihud_illiquidity",
    "v1",
    _BASE + ("quote_volume",),
    amihud_illiquidity_raw,
    display_name_en="Amihud Illiquidity",
    display_name_zh="Amihud 非流动性因子",
    formula=(
        "mean(abs(log(close(t) / close(t-1))) / quote_volume(t), window) "
        "× 1,000,000"
    ),
    description_zh=(
        "衡量单位成交额引起的价格变化。值越高表示较小成交额也会带来"
        "较大价格波动，即合约相对更不流动。"
    ),
),
```

Declare every additional market column in `required_columns`. English/Chinese names, the formula,
and the description appear in HTML reports, so they are part of the public factor definition. An
unregistered implementation fails with `unknown factor`.

## 5. Add formula-level tests

Use the Amihud cases in
`tests/acceptance/test_acceptance_07_factors_research.py` as a template. The current expected-value
case includes:

```python
(
    "amihud_illiquidity",
    {"window": "2m"},
    math.log(1.1) / 10 * 1_000_000,
),
```

Run the focused suite:

```bash
pytest tests/acceptance/test_acceptance_07_factors_research.py -q
```

New factors should also cover insufficient history, time gaps, incomplete bars, non-finite values,
and the rule that later data cannot change an earlier factor value.

## 6. Create a separate factor configuration

Keep the original tutorial configuration and create
`data/backtest/workspaces/tutorial/configs/factor-amihud.json`:

```json
{
  "factors": [
    {
      "name": "amihud_illiquidity",
      "version": "v1",
      "parameters": {
        "window": "24h"
      },
      "compute_interval": "1h",
      "preprocess": [
        {
          "name": "rank"
        }
      ]
    }
  ],
  "labels": [
    {
      "name": "forward_return_4h",
      "signal_delay_bars": 1,
      "horizon": "4h",
      "entry_field": "open",
      "exit_field": "open"
    }
  ],
  "cache": {
    "enabled": true
  }
}
```

The recognized `window` parameter lets the run planner load the required 24-hour warmup. A new
duration parameter name requires a corresponding planner extension.

## 7. Run on the tutorial dataset

Use the exact identity printed by your preparation step, not the example value below:

```bash
DATA_ROOT=data/backtest
DB="$DATA_ROOT/catalogs/tutorial.duckdb"
CONFIG_ROOT="$DATA_ROOT/workspaces/tutorial/configs"
LOG_ROOT="$DATA_ROOT/workspaces/tutorial/logs"
RUN_ROOT="$DATA_ROOT/runs"
DATASET_ID="binance-usdm-real-e2e-smoke-2026-06"
DATASET_VERSION="live-smoke-REPLACE_WITH_EXACT_VALUE"

bfbt run \
  "$DATASET_ID" \
  "$DATASET_VERSION" \
  amihud_illiquidity \
  --database "$DB" \
  --data-config "$CONFIG_ROOT/data.json" \
  --universe-config "$CONFIG_ROOT/universe.json" \
  --factor-config "$CONFIG_ROOT/factor-amihud.json" \
  --backtest-config "$CONFIG_ROOT/backtest.json" \
  --verify-hashes \
  | tee "$LOG_ROOT/run-amihud.log"
```

After `status=succeeded`, validate and open the exact run:

```bash
RUN_ID="a09-REPLACE_WITH_EXACT_VALUE"
python tests/live/validate_real_backtest_smoke.py "$RUN_ROOT/$RUN_ID"
echo "$RUN_ROOT/$RUN_ID/report.html"
```

New source code changes the source fingerprint, so a different run ID is expected.

## 8. Understand score direction

Rank preprocessing gives the largest raw value the highest score. The tutorial portfolio buys the
two highest scores and shorts the two lowest, so the Amihud example means:

```text
long relatively illiquid contracts; short relatively liquid contracts
```

If your hypothesis has the opposite direction, do not silently change the published
`amihud_illiquidity:v1` definition. Register a new name/version and explicitly negate the value,
then rerun formula tests before formal execution.

## 9. Replace the formula with your own

Every custom factor follows the same sequence:

1. Freeze its formula, time window, score direction, and required fields.
2. Implement `raw_value` using only information available at that timestamp.
3. Register a stable name, version, dependencies, display metadata, and compute function.
4. Test formula values, gaps, completeness, finite values, and future-data isolation.
5. Create a separate configuration and preserve the baseline.
6. Run a small deterministic fixture before reusing the real tutorial dataset.
7. Inspect factor values, fills, costs, metrics, and the report—not total return alone.

See the [data contract](../reference/data_contract.md) for field semantics and the
[configuration reference](../reference/configuration.md) for factor settings.
