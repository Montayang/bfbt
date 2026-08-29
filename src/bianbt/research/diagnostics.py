"""Eligible-universe factor and label coverage diagnostics."""

from __future__ import annotations

import polars as pl


def coverage_report(
    factors: pl.LazyFrame,
    labels: pl.LazyFrame,
    universe: pl.LazyFrame,
    *,
    universe_version: str,
) -> pl.LazyFrame:
    eligible = (
        universe.filter(
            pl.col("is_eligible")
            & (pl.col("universe_version") == universe_version)
        )
        .select("timestamp", "symbol")
        .unique()
    )
    factor_flags = factors.select(
        "timestamp",
        "symbol",
        pl.lit(True).alias("_has_factor"),
        pl.col("is_valid").alias("_factor_valid"),
    )
    label_flags = labels.select(
        "timestamp",
        "symbol",
        pl.lit(True).alias("_has_label"),
        pl.col("is_valid").alias("_label_valid"),
    )
    return (
        eligible.join(factor_flags, on=["timestamp", "symbol"], how="left")
        .join(label_flags, on=["timestamp", "symbol"], how="left")
        .group_by("timestamp")
        .agg(
            pl.len().alias("eligible_count"),
            pl.col("_has_factor").fill_null(False).sum().alias("factor_count"),
            pl.col("_factor_valid")
            .fill_null(False)
            .sum()
            .alias("valid_factor_count"),
            pl.col("_has_label").fill_null(False).sum().alias("label_count"),
            pl.col("_label_valid")
            .fill_null(False)
            .sum()
            .alias("valid_label_count"),
            (
                pl.col("_factor_valid").fill_null(False)
                & pl.col("_label_valid").fill_null(False)
            )
            .sum()
            .alias("aligned_valid_count"),
        )
        .sort("timestamp")
    )
