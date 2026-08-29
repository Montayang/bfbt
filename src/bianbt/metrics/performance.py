"""Deterministic equity and return performance metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import expm1, log1p, sqrt

import polars as pl

from bianbt.config.durations import duration_seconds

SECONDS_PER_YEAR = 365.25 * 24 * 60 * 60
EPSILON = 1e-10


class MetricsError(ValueError):
    """A return ledger cannot produce trustworthy metrics."""


@dataclass(frozen=True)
class PerformanceMetrics:
    observations: int
    start_time: datetime
    end_time: datetime
    initial_equity: float
    ending_equity: float
    total_return: float
    annualized_return: float | None
    annualized_volatility: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    max_drawdown: float
    calmar_ratio: float | None
    hit_rate: float


def _summary(returns: pl.LazyFrame) -> dict[str, object]:
    required = {"timestamp", "net_return", "equity"}
    missing = required - set(returns.collect_schema().names())
    if missing:
        raise MetricsError(f"return ledger is missing columns: {sorted(missing)}")
    ordered = returns.select(sorted(required)).sort("timestamp")
    checked = ordered.with_columns(
        pl.col("timestamp").shift(1).alias("_previous_timestamp"),
        pl.col("equity").shift(1).alias("_previous_equity"),
    ).with_columns(
        pl.when(pl.col("_previous_equity").is_null())
        .then(pl.col("equity") / (1.0 + pl.col("net_return")))
        .otherwise(pl.col("_previous_equity"))
        .alias("_compound_base"),
        (pl.col("equity") / (1.0 + pl.col("net_return")))
        .first()
        .alias("_initial_equity"),
    ).with_columns(
        (
            pl.col("equity")
            / pl.max_horizontal(
                pl.col("equity").cum_max(), pl.col("_initial_equity")
            )
            - 1.0
        ).alias("_drawdown"),
    )
    row = checked.select(
        pl.len().alias("observations"),
        pl.col("timestamp").first().alias("start_time"),
        pl.col("timestamp").last().alias("end_time"),
        pl.col("timestamp").eq(pl.col("_previous_timestamp")).any().alias(
            "duplicate_timestamp"
        ),
        pl.col("timestamp").is_null().any().alias("null_timestamp"),
        (
            pl.col("net_return").is_null()
            | pl.col("net_return").is_finite().not_()
        ).any().alias("bad_return"),
        (pl.col("net_return") <= -1.0).any().alias("return_below_floor"),
        (
            pl.col("equity").is_null()
            | pl.col("equity").is_finite().not_()
        ).any().alias("bad_equity"),
        (pl.col("equity") <= 0.0).any().alias("nonpositive_equity"),
        (
            (
                (
                    pl.col("_compound_base")
                    * (1.0 + pl.col("net_return"))
                    - pl.col("equity")
                ).abs()
            )
            > EPSILON
            * pl.max_horizontal(
                pl.lit(1.0),
                (
                    pl.col("_compound_base")
                    * (1.0 + pl.col("net_return"))
                ).abs(),
                pl.col("equity").abs(),
            )
        ).any().alias("compound_mismatch"),
        pl.col("_initial_equity").first().alias("initial_equity"),
        pl.col("equity").last().alias("ending_equity"),
        pl.col("net_return").mean().alias("mean_return"),
        pl.col("net_return").var(ddof=0).fill_null(0.0).alias("variance"),
        pl.when(pl.col("net_return") < 0.0)
        .then(pl.col("net_return").pow(2))
        .otherwise(0.0)
        .mean()
        .alias("downside_square_mean"),
        (pl.col("net_return") > 0.0).mean().alias("hit_rate"),
        pl.col("_drawdown").min().alias("max_drawdown"),
    ).collect(engine="streaming")
    if row.height == 0 or int(row.item(0, "observations")) == 0:
        raise MetricsError("return ledger is empty")
    return row.row(0, named=True)


def compute_performance_metrics(
    returns: pl.LazyFrame,
    *,
    base_interval: str,
) -> PerformanceMetrics:
    """Compute crypto-calendar annualized metrics from a return ledger."""

    summary = _summary(returns)
    if bool(summary["duplicate_timestamp"]):
        raise MetricsError("return ledger contains duplicate timestamps")
    if bool(summary["null_timestamp"]):
        raise MetricsError("return ledger timestamps cannot be null")
    if bool(summary["bad_return"]) or bool(summary["return_below_floor"]):
        raise MetricsError("net returns must be finite and greater than -1")
    if bool(summary["bad_equity"]) or bool(summary["nonpositive_equity"]):
        raise MetricsError("equity must be positive and finite")
    if bool(summary["compound_mismatch"]):
        raise MetricsError("equity path does not compound from net_return")
    observations = int(summary["observations"])
    initial_equity = float(summary["initial_equity"])
    ending_equity = float(summary["ending_equity"])
    periods_per_year = SECONDS_PER_YEAR / duration_seconds(base_interval)
    total_return = ending_equity / initial_equity - 1.0
    exponent = periods_per_year / observations
    annualized_log_return = exponent * log1p(total_return)
    annualized_return = (
        expm1(annualized_log_return)
        if annualized_log_return < 700
        else None
    )
    mean = float(summary["mean_return"])
    variance = float(summary["variance"])
    volatility = sqrt(variance)
    annualized_volatility = volatility * sqrt(periods_per_year)
    sharpe = (
        mean / volatility * sqrt(periods_per_year)
        if volatility > EPSILON
        else None
    )
    downside = sqrt(float(summary["downside_square_mean"]))
    sortino = (
        mean / downside * sqrt(periods_per_year)
        if downside > EPSILON
        else None
    )
    max_drawdown = float(summary["max_drawdown"])
    calmar = (
        annualized_return / abs(max_drawdown)
        if annualized_return is not None and max_drawdown < -EPSILON
        else None
    )
    start = summary["start_time"]
    end = summary["end_time"]
    assert isinstance(start, datetime) and isinstance(end, datetime)
    return PerformanceMetrics(
        observations=observations,
        start_time=start,
        end_time=end,
        initial_equity=initial_equity,
        ending_equity=ending_equity,
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        max_drawdown=max_drawdown,
        calmar_ratio=calmar,
        hit_rate=float(summary["hit_rate"]),
    )
