"""Actionable comparison of Matrix and Event common economic fields."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class EquivalenceDifference:
    timestamp: object
    field: str
    matrix_value: float
    event_value: float
    absolute_error: float


def first_return_difference(
    matrix: pl.DataFrame,
    event: pl.DataFrame,
    *, tolerance: float = 1e-10,
) -> EquivalenceDifference | None:
    fields = (
        "gross_price_return", "fee_cost", "slippage_cost", "funding_return",
        "net_return", "equity", "drawdown", "gross_exposure", "net_exposure", "turnover",
    )
    joined = matrix.drop("run_id").join(
        event.drop("run_id"), on="timestamp", how="full", suffix="_event", coalesce=True
    ).sort("timestamp")
    for row in joined.to_dicts():
        for field in fields:
            left, right = row.get(field), row.get(f"{field}_event")
            if left is None or right is None or abs(float(left) - float(right)) > tolerance:
                return EquivalenceDifference(
                    row["timestamp"], field, float(left or float("nan")),
                    float(right or float("nan")),
                    abs(float(left or 0.0) - float(right or 0.0)),
                )
    return None
