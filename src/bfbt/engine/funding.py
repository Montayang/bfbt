"""Perpetual funding cash-flow sign convention."""

from __future__ import annotations

from math import isfinite


def funding_cashflow(
    quantity: float,
    mark_price: float,
    funding_rate: float,
) -> float:
    """Return quote cash: positive-rate longs pay and shorts receive."""

    if not isfinite(quantity):
        raise ValueError("funding quantity must be finite")
    if not isfinite(mark_price) or mark_price <= 0:
        raise ValueError("funding mark_price must be positive and finite")
    if not isfinite(funding_rate):
        raise ValueError("funding_rate must be finite")
    signed_notional = quantity * mark_price
    return -signed_notional * funding_rate
