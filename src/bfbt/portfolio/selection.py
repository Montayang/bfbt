"""Deterministic top/bottom count and quantile selection."""

from __future__ import annotations

import polars as pl

from bfbt.config.backtest import (
    PortfolioConfig,
    RankSelectionConfig,
    RankSideConfig,
)
from bfbt.data.v2_contracts import V2ReasonCode
from bfbt.portfolio.base import PortfolioError


def _side_expression(side: RankSideConfig) -> pl.Expr:
    expression = pl.lit(False)
    if side.ranks:
        expression = expression | pl.col("ordinal_rank").is_in(side.ranks)
    for start, end in side.ranges:
        expression = expression | pl.col("ordinal_rank").is_between(
            start, end, closed="both"
        )
    return expression


def _requested_ranks(side: RankSideConfig) -> tuple[int, ...]:
    values = set(side.ranks)
    for start, end in side.ranges:
        values.update(range(start, end + 1))
    return tuple(sorted(values))


def select_exact_ranks(
    rankings: pl.LazyFrame,
    selection: RankSelectionConfig,
) -> tuple[pl.LazyFrame, pl.LazyFrame]:
    """Select independent exact ranks and return auditable missing-rank rows."""

    if selection.lag != 0:
        raise PortfolioError("A13 exact Rank selection requires lag=0")
    long_match = _side_expression(selection.long)
    short_match = _side_expression(selection.short)
    selected = rankings.filter(long_match | short_match).with_columns(
        pl.when(long_match)
        .then(pl.lit("LONG"))
        .otherwise(pl.lit("SHORT"))
        .alias("side"),
        pl.col("raw_score").alias("score"),
        pl.col("timestamp").alias("signal_time"),
    )

    requested = [
        {"side": side, "requested_rank": rank}
        for side, values in (
            ("LONG", _requested_ranks(selection.long)),
            ("SHORT", _requested_ranks(selection.short)),
        )
        for rank in values
    ]
    request_frame = pl.DataFrame(
        requested,
        schema={"side": pl.String, "requested_rank": pl.Int32},
    ).lazy()
    samples = rankings.select("timestamp", "sample_count").unique()
    out_of_range = (
        samples.join(request_frame, how="cross")
        .filter(pl.col("requested_rank") > pl.col("sample_count"))
        .with_columns(
            pl.lit(None, dtype=pl.String).alias("symbol"),
            pl.lit(V2ReasonCode.RANK_OUT_OF_RANGE.value).alias("reason_code"),
        )
    )
    selected_reasons = selected.select(
        "timestamp",
        "symbol",
        "side",
        pl.col("ordinal_rank").alias("requested_rank"),
        "sample_count",
    ).with_columns(
        pl.lit(V2ReasonCode.SELECTED_CURRENT_RANK.value).alias("reason_code")
    )
    diagnostics = pl.concat(
        [
            selected_reasons,
            out_of_range.select(
                "timestamp",
                "symbol",
                "side",
                "requested_rank",
                "sample_count",
                "reason_code",
            ),
        ],
        how="vertical_relaxed",
    ).sort(["timestamp", "side", "requested_rank", "symbol"])
    return selected, diagnostics


def v1_rank_counts(
    config: PortfolioConfig,
    sample_count: pl.Expr,
) -> tuple[pl.Expr, pl.Expr]:
    """Map legacy Top/Bottom rules to descending continuous Rank ranges."""

    if config.construction == "long_short_count":
        assert config.long_count is not None and config.short_count is not None
        return pl.lit(config.long_count), pl.lit(config.short_count)
    return (
        pl.max_horizontal(
            pl.lit(1),
            (sample_count * config.long_quantile).floor().cast(pl.Int64),
        ),
        pl.max_horizontal(
            pl.lit(1),
            (sample_count * config.short_quantile).floor().cast(pl.Int64),
        ),
    )


def select_long_short(
    scores: pl.LazyFrame,
    config: PortfolioConfig,
) -> pl.LazyFrame:
    """Select disjoint low-score shorts and high-score longs per timestamp."""

    required = {
        "timestamp",
        "symbol",
        "value",
        "is_valid",
        "factor_version",
        "universe_version",
    }
    names = scores.collect_schema().names()
    missing = required - set(names)
    if missing:
        raise PortfolioError(f"score input is missing columns: {sorted(missing)}")
    optional = ["volatility"] if "volatility" in names else []
    ranked = (
        scores.filter(
            pl.col("is_valid")
            & pl.col("value").is_not_null()
            & pl.col("value").is_finite()
        )
        .select(
            pl.col("timestamp").alias("signal_time"),
            "symbol",
            pl.col("value").alias("score"),
            "factor_version",
            "universe_version",
            *optional,
        )
        .sort(["signal_time", "score", "symbol"])
        .with_columns(
            pl.len().over("signal_time").alias("_sample_count"),
            pl.col("score")
            .rank(method="ordinal")
            .over("signal_time")
            .alias("_rank"),
        )
    )
    if config.construction == "long_short_count":
        assert config.long_count is not None and config.short_count is not None
        long_count = pl.lit(config.long_count)
        short_count = pl.lit(config.short_count)
    else:
        long_count = pl.max_horizontal(
            pl.lit(1),
            (pl.col("_sample_count") * config.long_quantile)
            .floor()
            .cast(pl.Int64),
        )
        short_count = pl.max_horizontal(
            pl.lit(1),
            (pl.col("_sample_count") * config.short_quantile)
            .floor()
            .cast(pl.Int64),
        )
    selected = ranked.with_columns(
        long_count.alias("_long_count"),
        short_count.alias("_short_count"),
    ).filter(
        (pl.col("_sample_count") >= 2)
        & (pl.col("_long_count") + pl.col("_short_count") <= pl.col("_sample_count"))
        & (
            (pl.col("_rank") <= pl.col("_short_count"))
            | (
                pl.col("_rank")
                > pl.col("_sample_count") - pl.col("_long_count")
            )
        )
    )
    return selected.with_columns(
        pl.when(pl.col("_rank") <= pl.col("_short_count"))
        .then(pl.lit("SHORT"))
        .otherwise(pl.lit("LONG"))
        .alias("side")
    ).drop("_rank", "_long_count", "_short_count")
