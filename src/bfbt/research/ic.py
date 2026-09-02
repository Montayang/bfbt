"""Per-timestamp Pearson IC and Spearman rank IC."""

from __future__ import annotations

import polars as pl


def aligned_valid_samples(
    factors: pl.LazyFrame, labels: pl.LazyFrame
) -> pl.LazyFrame:
    return (
        factors.filter(pl.col("is_valid"))
        .select("timestamp", "symbol", "value")
        .join(
            labels.filter(pl.col("is_valid")).select(
                "timestamp", "symbol", "forward_return"
            ),
            on=["timestamp", "symbol"],
            how="inner",
        )
    )


def information_coefficient(
    factors: pl.LazyFrame, labels: pl.LazyFrame
) -> pl.LazyFrame:
    """Return IC and rank IC with the exact aligned sample count."""

    aligned = aligned_valid_samples(factors, labels).with_columns(
        pl.col("value")
        .rank(method="average")
        .over("timestamp")
        .alias("_x_rank"),
        pl.col("forward_return")
        .rank(method="average")
        .over("timestamp")
        .alias("_y_rank"),
    )
    return (
        aligned.group_by("timestamp")
        .agg(
            pl.len().alias("sample_count"),
            pl.corr("value", "forward_return").alias("ic"),
            pl.corr("_x_rank", "_y_rank").alias("rank_ic"),
        )
        .sort("timestamp")
    )
