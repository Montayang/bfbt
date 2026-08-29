"""Cross-sectional factor quantile return analysis."""

from __future__ import annotations

import polars as pl

from bianbt.research.ic import aligned_valid_samples


def quantile_returns(
    factors: pl.LazyFrame,
    labels: pl.LazyFrame,
    *,
    quantiles: int = 5,
) -> pl.LazyFrame:
    if quantiles < 2:
        raise ValueError("quantiles must be at least 2")
    aligned = aligned_valid_samples(factors, labels)
    rank = pl.col("value").rank(method="ordinal").over("timestamp")
    count = pl.len().over("timestamp")
    assigned = aligned.with_columns(
        (((rank - 1) * quantiles / count).floor() + 1)
        .cast(pl.Int64)
        .alias("quantile")
    )
    return (
        assigned.group_by("timestamp", "quantile")
        .agg(
            pl.len().alias("sample_count"),
            pl.col("forward_return").mean().alias("mean_forward_return"),
        )
        .sort(["timestamp", "quantile"])
    )
