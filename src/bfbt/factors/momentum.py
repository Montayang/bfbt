"""Point-in-time momentum and short-term reversal factors."""

from __future__ import annotations

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


def _bars(value: object, base_interval: str, name: str) -> int:
    if not isinstance(value, str):
        raise FactorError(f"{name} must be a duration string")
    seconds = duration_seconds(value)
    base = duration_seconds(base_interval)
    if seconds % base:
        raise FactorError(f"{name} must be a multiple of base_interval")
    return seconds // base


def momentum_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
    reverse: bool = False,
) -> pl.LazyFrame:
    lookback = _bars(
        definition.parameters.get("lookback"), base_interval, "lookback"
    )
    skip_value = definition.parameters.get("skip_recent")
    skip = (
        0
        if skip_value is None
        else _bars(skip_value, base_interval, "skip_recent")
    )
    current = pl.col("close").shift(skip).over("symbol")
    past = pl.col("close").shift(skip + lookback).over("symbol")
    current_time = pl.col("close_time").shift(skip).over("symbol")
    past_time = pl.col("close_time").shift(skip + lookback).over("symbol")
    expected = lookback * duration_seconds(base_interval) * 1_000
    complete_window = (
        pl.col("is_complete")
        .cast(pl.Int64)
        .rolling_sum(lookback + 1)
        .shift(skip)
        .over("symbol")
        == lookback + 1
    )
    value = current / past - 1.0
    if reverse:
        value = -value
    return (
        bars.with_columns(
            pl.when(
                current.is_not_null()
                & past.is_not_null()
                & current.is_finite()
                & past.is_finite()
                & (current > 0)
                & (past > 0)
                & complete_window
                & (
                    current_time.cast(pl.Int64) - past_time.cast(pl.Int64)
                    == expected
                )
            )
            .then(value)
            .otherwise(None)
            .alias("raw_value")
        )
        .select(pl.col("close_time").alias("timestamp"), "symbol", "raw_value")
    )
