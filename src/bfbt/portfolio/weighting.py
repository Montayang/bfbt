"""Long/short equal, score, and inverse-volatility sizing."""

from __future__ import annotations

import polars as pl

from bfbt.config.backtest import PortfolioConfig
from bfbt.portfolio.base import PortfolioError


def weight_selected(
    selected: pl.LazyFrame,
    config: PortfolioConfig,
) -> pl.LazyFrame:
    names = selected.collect_schema().names()
    if config.weighting == "inverse_volatility" and "volatility" not in names:
        raise PortfolioError(
            "inverse_volatility weighting requires a volatility column"
        )
    if config.weighting == "equal":
        strength = pl.lit(1.0)
    elif config.weighting == "score":
        strength = pl.col("score").abs()
    else:
        strength = pl.when(
            pl.col("volatility").is_not_null()
            & pl.col("volatility").is_finite()
            & (pl.col("volatility") > 0)
        ).then(1.0 / pl.col("volatility")).otherwise(0.0)
    long_budget = (config.gross_exposure + config.net_exposure) / 2.0
    short_budget = (config.gross_exposure - config.net_exposure) / 2.0
    prepared = selected.with_columns(strength.alias("_strength")).with_columns(
        pl.col("_strength").sum().over(["signal_time", "side"]).alias("_total"),
        pl.len().over(["signal_time", "side"]).alias("_side_count"),
    )
    normalized = pl.when(pl.col("_total") > 0).then(
        pl.col("_strength") / pl.col("_total")
    ).otherwise(1.0 / pl.col("_side_count"))
    return prepared.with_columns(
        pl.when(pl.col("side") == "LONG")
        .then(normalized * long_budget)
        .otherwise(-normalized * short_budget)
        .alias("unconstrained_weight")
    ).drop("_strength", "_total", "_side_count")
