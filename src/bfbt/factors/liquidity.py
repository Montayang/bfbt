"""Rolling quote-volume and taker-buy participation factors."""

from __future__ import annotations

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


def _window(definition: FactorDefinition, base_interval: str) -> int:
    value = definition.parameters.get("window")
    if not isinstance(value, str):
        raise FactorError("window must be a duration string")
    seconds, base = duration_seconds(value), duration_seconds(base_interval)
    if seconds % base:
        raise FactorError("window must be a multiple of base_interval")
    return seconds // base


def _window_is_contiguous(window: int, base_interval: str) -> pl.Expr:
    expected = (window - 1) * duration_seconds(base_interval) * 1_000
    first_time = pl.col("open_time").shift(window - 1).over("symbol")
    return (
        (
            pl.col("open_time").cast(pl.Int64) - first_time.cast(pl.Int64)
            == expected
        )
        & (
            pl.col("is_complete")
            .cast(pl.Int64)
            .rolling_sum(window)
            .over("symbol")
            == window
        )
    )


def quote_volume_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
) -> pl.LazyFrame:
    window = _window(definition, base_interval)
    total = pl.col("quote_volume").rolling_sum(window).over("symbol")
    return bars.with_columns(
        pl.when(_window_is_contiguous(window, base_interval))
        .then(total)
        .otherwise(None)
        .alias("raw_value")
    ).select(pl.col("close_time").alias("timestamp"), "symbol", "raw_value")


def taker_buy_ratio_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
) -> pl.LazyFrame:
    window = _window(definition, base_interval)
    numerator = (
        pl.col("taker_buy_quote_volume").rolling_sum(window).over("symbol")
    )
    denominator = pl.col("quote_volume").rolling_sum(window).over("symbol")
    return bars.with_columns(
        pl.when(
            _window_is_contiguous(window, base_interval) & (denominator > 0)
        )
        .then(numerator / denominator)
        .otherwise(None)
        .alias("raw_value")
    ).select(pl.col("close_time").alias("timestamp"), "symbol", "raw_value")
