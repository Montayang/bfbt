"""Fixed-bps fee and slippage contribution helpers."""

from __future__ import annotations

from bfbt.config.backtest import FeeConfig, SlippageConfig


class CostModelError(ValueError):
    """Execution cost configuration is incomplete."""


def fee_rate(config: FeeConfig) -> float:
    if config.model == "zero":
        return 0.0
    if config.taker_bps is None:
        raise CostModelError("fixed_bps fee requires taker_bps")
    return config.taker_bps / 10_000.0


def slippage_rate(config: SlippageConfig) -> float:
    if config.model == "zero":
        return 0.0
    if config.bps is None:
        raise CostModelError("fixed_bps slippage requires bps")
    return config.bps / 10_000.0


def fee_cost(turnover_notional: float, config: FeeConfig) -> float:
    return abs(turnover_notional) * fee_rate(config)


def slippage_cost(
    turnover_notional: float, config: SlippageConfig
) -> float:
    return abs(turnover_notional) * slippage_rate(config)
