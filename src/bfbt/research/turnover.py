"""Cross-sectional factor-rank turnover between observations."""

from __future__ import annotations

import polars as pl


def factor_rank_turnover(factors: pl.LazyFrame) -> pl.LazyFrame:
    valid = factors.filter(pl.col("is_valid"))
    rank = pl.col("value").rank(method="average").over("timestamp")
    count = pl.len().over("timestamp")
    ranked = (
        valid.with_columns(
            pl.when(count == 1)
            .then(0.5)
            .otherwise((rank - 1) / (count - 1))
            .alias("_rank")
        )
        .sort(["symbol", "timestamp"])
        .with_columns(
            pl.col("_rank").shift(1).over("symbol").alias("_previous_rank"),
            pl.col("timestamp")
            .shift(1)
            .over("symbol")
            .alias("previous_timestamp"),
        )
        .with_columns(
            (pl.col("_rank") - pl.col("_previous_rank"))
            .abs()
            .alias("_absolute_change")
        )
    )
    return (
        ranked.group_by("timestamp")
        .agg(
            pl.col("_absolute_change").count().alias("sample_count"),
            pl.col("_absolute_change").mean().alias("rank_turnover"),
        )
        .sort("timestamp")
    )
