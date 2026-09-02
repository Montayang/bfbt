"""Deterministic cross-sectional Rank snapshots for V1 and V2 selection."""

from __future__ import annotations

import polars as pl

from bfbt.portfolio.base import PortfolioError

UTC_MS = pl.Datetime("ms", "UTC")
RANKING_SCHEMA = {
    "timestamp": UTC_MS,
    "rank_clock": pl.String,
    "symbol": pl.String,
    "factor_name": pl.String,
    "raw_score": pl.Float64,
    "ordinal_rank": pl.Int32,
    "percentile_rank": pl.Float64,
    "sample_count": pl.Int32,
    "factor_version": pl.String,
    "universe_version": pl.String,
    "run_id": pl.String,
}


def build_rank_snapshots(
    scores: pl.LazyFrame,
    *,
    factor_name: str,
    factor_version: str,
    universe_version: str,
    rank_clock: str = "rebalance",
) -> pl.LazyFrame:
    """Return one stable, complete Rank row for every eligible score."""

    required = {
        "timestamp", "symbol", "value", "is_valid",
        "factor_version", "universe_version",
    }
    names = scores.collect_schema().names()
    missing = required - set(names)
    if missing:
        raise PortfolioError(f"score input is missing columns: {sorted(missing)}")
    if not factor_name:
        raise PortfolioError("factor_name must not be empty")
    if rank_clock not in {"factor", "rebalance"}:
        raise PortfolioError("rank_clock must be factor or rebalance")

    ranked = (
        scores.filter(
            (pl.col("factor_version") == factor_version)
            & (pl.col("universe_version") == universe_version)
            & pl.col("is_valid")
            & pl.col("value").is_not_null()
            & pl.col("value").is_finite()
        )
        .select(
            pl.col("timestamp").cast(UTC_MS),
            pl.col("symbol").cast(pl.String),
            pl.col("value").cast(pl.Float64).alias("raw_score"),
            pl.col("factor_version").cast(pl.String),
            pl.col("universe_version").cast(pl.String),
        )
        .sort(["timestamp", "raw_score", "symbol"], descending=[False, True, False])
        .with_columns(
            pl.col("raw_score").rank(method="ordinal", descending=True)
            .over("timestamp").cast(pl.Int32).alias("ordinal_rank"),
            pl.len().over("timestamp").cast(pl.Int32).alias("sample_count"),
        )
        .with_columns(
            pl.when(pl.col("sample_count") == 1).then(1.0).otherwise(
                1.0 - (pl.col("ordinal_rank") - 1)
                / (pl.col("sample_count") - 1)
            ).cast(pl.Float64).alias("percentile_rank"),
            pl.lit(rank_clock).alias("rank_clock"),
            pl.lit(factor_name).alias("factor_name"),
            pl.lit("").alias("run_id"),
        )
    )
    return ranked.select(
        "timestamp", "rank_clock", "symbol", "factor_name", "raw_score",
        "ordinal_rank", "percentile_rank", "sample_count", "factor_version",
        "universe_version", "run_id",
    ).sort(["timestamp", "factor_name", "ordinal_rank", "symbol"])
