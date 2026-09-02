"""Exposure, turnover, and drawdown summaries."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from bfbt.metrics.performance import MetricsError


@dataclass(frozen=True)
class RiskMetrics:
    average_gross_exposure: float
    maximum_gross_exposure: float
    average_absolute_net_exposure: float
    maximum_absolute_net_exposure: float
    average_turnover: float
    maximum_turnover: float
    total_turnover: float


def compute_risk_metrics(returns: pl.LazyFrame) -> RiskMetrics:
    required = {"gross_exposure", "net_exposure", "turnover"}
    missing = required - set(returns.collect_schema().names())
    if missing:
        raise MetricsError(f"return ledger is missing columns: {sorted(missing)}")
    summary = returns.select(
        pl.len().alias("count"),
        (
            pl.col("gross_exposure").is_null()
            | pl.col("gross_exposure").is_finite().not_()
        ).any().alias("bad_gross"),
        (
            pl.col("net_exposure").is_null()
            | pl.col("net_exposure").is_finite().not_()
        ).any().alias("bad_net"),
        (
            pl.col("turnover").is_null()
            | pl.col("turnover").is_finite().not_()
        ).any().alias("bad_turnover"),
        (pl.col("gross_exposure") < 0.0).any().alias("negative_gross"),
        (pl.col("turnover") < 0.0).any().alias("negative_turnover"),
        pl.col("gross_exposure").mean().alias("average_gross"),
        pl.col("gross_exposure").max().alias("maximum_gross"),
        pl.col("net_exposure").abs().mean().alias("average_net"),
        pl.col("net_exposure").abs().max().alias("maximum_net"),
        pl.col("turnover").mean().alias("average_turnover"),
        pl.col("turnover").max().alias("maximum_turnover"),
        pl.col("turnover").sum().alias("total_turnover"),
    ).collect(engine="streaming").row(0, named=True)
    if int(summary["count"]) == 0:
        raise MetricsError("return ledger is empty")
    if any(bool(summary[name]) for name in (
        "bad_gross", "bad_net", "bad_turnover", "negative_gross",
        "negative_turnover",
    )):
        raise MetricsError("exposure and turnover values must be finite and non-negative")
    return RiskMetrics(
        average_gross_exposure=float(summary["average_gross"]),
        maximum_gross_exposure=float(summary["maximum_gross"]),
        average_absolute_net_exposure=float(summary["average_net"]),
        maximum_absolute_net_exposure=float(summary["maximum_net"]),
        average_turnover=float(summary["average_turnover"]),
        maximum_turnover=float(summary["maximum_turnover"]),
        total_turnover=float(summary["total_turnover"]),
    )
