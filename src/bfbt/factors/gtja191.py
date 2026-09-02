"""Selected bar-count GTJA Alpha191 formulas used by quick research."""

from __future__ import annotations

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


SUPPORTED_ALPHAS = (18, 20, 24, 31, 40, 53, 66, 71, 88, 89, 112, 151)


def _sma(expr: pl.Expr, n: int, m: int, groups: list[str]) -> pl.Expr:
    """Chinese-market SMA: Y=(m*X+(n-m)*Y[-1])/n."""

    return expr.ewm_mean(
        alpha=m / n,
        adjust=False,
        min_samples=n,
        ignore_nulls=False,
    ).over(groups)


def gtja191_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
    alpha: int,
) -> pl.LazyFrame:
    """Compute one selected Alpha191 formula using literal K-line counts."""

    if alpha not in SUPPORTED_ALPHAS:
        raise FactorError(f"unsupported GTJA Alpha191 formula: Alpha{alpha}")
    volume_field = definition.parameters.get("volume_field", "quote_volume")
    if alpha == 40 and volume_field not in {"quote_volume", "volume"}:
        raise FactorError("Alpha40 volume_field must be quote_volume or volume")

    expected_ms = duration_seconds(base_interval) * 1_000
    previous_time = pl.col("close_time").shift(1).over("symbol")
    previous_complete = pl.col("is_complete").shift(1).over("symbol")
    new_segment = (
        previous_time.is_null()
        | ((pl.col("close_time").cast(pl.Int64) - previous_time.cast(pl.Int64)) != expected_ms)
        | ~pl.col("is_complete")
        | ~previous_complete.fill_null(False)
    )
    prepared = bars.with_columns(
        new_segment.cast(pl.UInt32).cum_sum().over("symbol").alias("_segment")
    )
    groups = ["symbol", "_segment"]
    close = pl.col("close")
    delay1 = close.shift(1).over(groups)

    if alpha == 18:
        raw = close / close.shift(5).over(groups)
    elif alpha == 20:
        delayed = close.shift(6).over(groups)
        raw = (close - delayed) / delayed * 100.0
    elif alpha == 24:
        raw = _sma(close - close.shift(5).over(groups), 5, 1, groups)
    elif alpha == 31:
        mean = close.rolling_mean(12, min_samples=12).over(groups)
        raw = (close - mean) / mean * 100.0
    elif alpha == 40:
        volume = pl.col(str(volume_field))
        up = pl.when(close > delay1).then(volume).otherwise(0.0)
        down = pl.when(close <= delay1).then(volume).otherwise(0.0)
        up_sum = up.rolling_sum(26, min_samples=26).over(groups)
        down_sum = down.rolling_sum(26, min_samples=26).over(groups)
        raw = pl.when(down_sum > 0).then(up_sum / down_sum * 100.0)
    elif alpha == 53:
        raw = (
            (close > delay1)
            .cast(pl.Float64)
            .rolling_sum(12, min_samples=12)
            .over(groups)
            / 12.0
            * 100.0
        )
    elif alpha == 66:
        mean = close.rolling_mean(6, min_samples=6).over(groups)
        raw = (close - mean) / mean * 100.0
    elif alpha == 71:
        mean = close.rolling_mean(24, min_samples=24).over(groups)
        raw = (close - mean) / mean * 100.0
    elif alpha == 88:
        delayed = close.shift(20).over(groups)
        raw = (close - delayed) / delayed * 100.0
    elif alpha == 89:
        fast = _sma(close, 13, 2, groups)
        slow = _sma(close, 27, 2, groups)
        difference = fast - slow
        raw = 2.0 * (difference - _sma(difference, 10, 2, groups))
    elif alpha == 112:
        change = close - delay1
        up_sum = (
            pl.when(change > 0).then(change).otherwise(0.0)
            .rolling_sum(12, min_samples=12).over(groups)
        )
        down_sum = (
            pl.when(change < 0).then(change.abs()).otherwise(0.0)
            .rolling_sum(12, min_samples=12).over(groups)
        )
        denominator = up_sum + down_sum
        raw = pl.when(denominator > 0).then((up_sum - down_sum) / denominator * 100.0)
    else:  # Alpha151
        raw = _sma(close - close.shift(20).over(groups), 20, 1, groups)

    return (
        prepared.with_columns(
            pl.when(
                pl.col("is_complete")
                & close.is_not_null()
                & close.is_finite()
                & (close > 0)
            )
            .then(raw)
            .otherwise(None)
            .alias("raw_value")
        )
        .select(pl.col("close_time").alias("timestamp"), "symbol", "raw_value")
    )
