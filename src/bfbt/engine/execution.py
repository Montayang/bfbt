"""Next-bar-open fills and state-dependent turnover limiting."""

from __future__ import annotations

from datetime import datetime, timedelta

from bfbt.config.durations import duration_seconds


def fill_time(
    signal_time: datetime,
    *,
    signal_delay_bars: int,
    base_interval: str,
) -> datetime:
    if signal_delay_bars < 1:
        raise ValueError("next_bar_open requires signal_delay_bars >= 1")
    return signal_time + timedelta(
        seconds=signal_delay_bars * duration_seconds(base_interval)
    )


def adverse_fill_price(
    reference_price: float,
    quantity_delta: float,
    slippage_rate: float,
) -> float:
    if quantity_delta > 0:
        return reference_price * (1.0 + slippage_rate)
    if quantity_delta < 0:
        return reference_price * (1.0 - slippage_rate)
    return reference_price


def limit_turnover(
    old_weights: dict[str, float],
    requested_weights: dict[str, float],
    maximum: float | None,
) -> tuple[dict[str, float], float, float]:
    """Scale all target deltas uniformly and return weights/scale/turnover."""

    symbols = sorted(set(old_weights) | set(requested_weights))
    deltas = {
        symbol: requested_weights.get(symbol, 0.0)
        - old_weights.get(symbol, 0.0)
        for symbol in symbols
    }
    requested_turnover = sum(abs(value) for value in deltas.values())
    scale = (
        1.0
        if maximum is None or requested_turnover <= maximum or requested_turnover == 0
        else maximum / requested_turnover
    )
    limited = {
        symbol: old_weights.get(symbol, 0.0) + scale * deltas[symbol]
        for symbol in symbols
    }
    return limited, scale, requested_turnover * scale


def updated_average_entry(
    old_quantity: float,
    new_quantity: float,
    old_average: float | None,
    trade_price: float,
) -> float | None:
    if abs(new_quantity) < 1e-15:
        return None
    delta = new_quantity - old_quantity
    if old_average is None or old_quantity * new_quantity <= 0:
        return trade_price
    if abs(new_quantity) <= abs(old_quantity):
        return old_average
    return (
        abs(old_quantity) * old_average + abs(delta) * trade_price
    ) / abs(new_quantity)
