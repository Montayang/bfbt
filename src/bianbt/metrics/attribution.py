"""Additive return contribution checks and totals."""

from __future__ import annotations

from dataclasses import dataclass
import polars as pl

from bianbt.metrics.performance import MetricsError

IDENTITY_TOLERANCE = 1e-12


@dataclass(frozen=True)
class ReturnAttribution:
    gross_price_contribution: float
    fee_contribution: float
    slippage_contribution: float
    funding_contribution: float
    net_contribution: float
    maximum_identity_error: float


def compute_return_attribution(returns: pl.LazyFrame) -> ReturnAttribution:
    required = {
        "gross_price_return",
        "fee_cost",
        "slippage_cost",
        "funding_return",
        "net_return",
    }
    missing = required - set(returns.collect_schema().names())
    if missing:
        raise MetricsError(f"return ledger is missing columns: {sorted(missing)}")
    identity_error = (
        pl.col("gross_price_return")
        - pl.col("fee_cost")
        - pl.col("slippage_cost")
        + pl.col("funding_return")
        - pl.col("net_return")
    ).abs()
    summary = returns.select(
        pl.len().alias("count"),
        pl.any_horizontal(
            *[
                pl.col(name).is_null() | pl.col(name).is_finite().not_()
                for name in sorted(required)
            ]
        ).any().alias("bad_value"),
        identity_error.max().alias("maximum_error"),
        pl.col("gross_price_return").sum().alias("gross"),
        pl.col("fee_cost").sum().alias("fee"),
        pl.col("slippage_cost").sum().alias("slippage"),
        pl.col("funding_return").sum().alias("funding"),
        pl.col("net_return").sum().alias("net"),
    ).collect(engine="streaming").row(0, named=True)
    if int(summary["count"]) == 0:
        raise MetricsError("return ledger is empty")
    if bool(summary["bad_value"]):
        raise MetricsError("return attribution values must be finite")
    if float(summary["maximum_error"]) > IDENTITY_TOLERANCE:
        raise MetricsError("return contribution identity is violated")
    return ReturnAttribution(
        gross_price_contribution=float(summary["gross"]),
        fee_contribution=float(summary["fee"]),
        slippage_contribution=float(summary["slippage"]),
        funding_contribution=float(summary["funding"]),
        net_contribution=float(summary["net"]),
        maximum_identity_error=float(summary["maximum_error"]),
    )
