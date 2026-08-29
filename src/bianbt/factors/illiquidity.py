"""Amihud-style rolling illiquidity factor."""

from __future__ import annotations

import polars as pl

from bianbt.config.durations import duration_seconds
from bianbt.config.factor import FactorDefinition
from bianbt.factors.base import FactorError


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