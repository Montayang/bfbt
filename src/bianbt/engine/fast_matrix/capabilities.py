"""Fail-closed capability planning for the Fast Matrix research backend."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from bianbt.config.backtest import BacktestConfig, PortfolioV2Config, RiskV2Config


class MatrixCapabilityError(ValueError):
    """An explicit Fast Matrix request cannot preserve configured semantics."""


class ReasonCode(StrEnum):
    LEGACY_DEFAULT = "LEGACY_DEFAULT"
    EXPLICIT_EVENT = "EXPLICIT_EVENT"
    MATRIX_SUPPORTED = "MATRIX_SUPPORTED"
    FORMAL_REQUIRES_EVENT = "FORMAL_REQUIRES_EVENT"
    UNSUPPORTED_CONFIG_VERSION = "UNSUPPORTED_CONFIG_VERSION"
    UNSUPPORTED_STATE_DEPENDENT_SIZING = "UNSUPPORTED_STATE_DEPENDENT_SIZING"
    UNSUPPORTED_PORTFOLIO_CONSTRAINT = "UNSUPPORTED_PORTFOLIO_CONSTRAINT"
    UNSUPPORTED_HOLDING_POLICY = "UNSUPPORTED_HOLDING_POLICY"
    UNSUPPORTED_SYMBOL_EXIT = "UNSUPPORTED_SYMBOL_EXIT"
    UNSUPPORTED_PORTFOLIO_EXIT = "UNSUPPORTED_PORTFOLIO_EXIT"
    UNSUPPORTED_DYNAMIC_REENTRY = "UNSUPPORTED_DYNAMIC_REENTRY"
    UNSUPPORTED_PARTIAL_FILL = "UNSUPPORTED_PARTIAL_FILL"
    UNSUPPORTED_LIQUIDATION = "UNSUPPORTED_LIQUIDATION"
    UNSUPPORTED_SAME_BAR_TRIGGER = "UNSUPPORTED_SAME_BAR_TRIGGER"
    UNSUPPORTED_SIGNAL_DELAY = "UNSUPPORTED_SIGNAL_DELAY"
    UNSUPPORTED_MARGIN_SATURATION = "UNSUPPORTED_MARGIN_SATURATION"


@dataclass(frozen=True)
class BackendDecision:
    requested_backend: str
    selected_backend: Literal["legacy_v1", "fast_matrix", "event"]
    supported: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_backend": self.requested_backend,
            "selected_backend": self.selected_backend,
            "supported": self.supported,
            "reason_codes": list(self.reason_codes),
        }


def _unsupported(config: BacktestConfig) -> list[ReasonCode]:
    reasons: list[ReasonCode] = []
    if config.config_version != "v2":
        return [ReasonCode.UNSUPPORTED_CONFIG_VERSION]
    portfolio = config.portfolio
    risk = config.risk
    assert isinstance(portfolio, PortfolioV2Config)
    assert isinstance(risk, RiskV2Config)
    if portfolio.sizing.mode != "target_weight":
        reasons.append(ReasonCode.UNSUPPORTED_STATE_DEPENDENT_SIZING)
    if any(value is not None for value in portfolio.constraints.model_dump().values()):
        reasons.append(ReasonCode.UNSUPPORTED_PORTFOLIO_CONSTRAINT)
    if portfolio.holding.mode != "independent" or portfolio.holding.existing_signal != "add":
        reasons.append(ReasonCode.UNSUPPORTED_HOLDING_POLICY)
    if any(
        getattr(risk.symbol_exits, name).enabled
        for name in ("stop_loss", "take_profit", "trailing_stop")
    ):
        reasons.append(ReasonCode.UNSUPPORTED_SYMBOL_EXIT)
    if any(value is not None for value in risk.portfolio_exits.model_dump().values()):
        reasons.append(ReasonCode.UNSUPPORTED_PORTFOLIO_EXIT)
    if risk.cooldown_bars or risk.reentry_policy != "next_scheduled_rebalance":
        reasons.append(ReasonCode.UNSUPPORTED_DYNAMIC_REENTRY)
    if config.execution.partial_fill:
        reasons.append(ReasonCode.UNSUPPORTED_PARTIAL_FILL)
    if risk.enforce_liquidation:
        reasons.append(ReasonCode.UNSUPPORTED_LIQUIDATION)
    if risk.fill_model != "next_bar_open":
        reasons.append(ReasonCode.UNSUPPORTED_SAME_BAR_TRIGGER)
    if config.schedule.signal_delay_bars < 1:
        reasons.append(ReasonCode.UNSUPPORTED_SIGNAL_DELAY)
    if (
        portfolio.sizing.target_gross_exposure is not None
        and portfolio.sizing.target_gross_exposure >= risk.leverage
    ):
        reasons.append(ReasonCode.UNSUPPORTED_MARGIN_SATURATION)
    return reasons


def plan_backend(config: BacktestConfig) -> BackendDecision:
    """Choose deterministically; explicit matrix requests fail with stable codes."""

    engine = config.engine
    if engine is None:
        selected = "legacy_v1" if config.config_version == "v1" else "event"
        return BackendDecision("legacy_default", selected, True, (ReasonCode.LEGACY_DEFAULT,))
    if engine.backend == "event":
        return BackendDecision("event", "event", True, (ReasonCode.EXPLICIT_EVENT,))
    if engine.purpose == "formal":
        reasons = [ReasonCode.FORMAL_REQUIRES_EVENT]
    else:
        reasons = _unsupported(config)
    if not reasons:
        return BackendDecision(
            engine.backend, "fast_matrix", True, (ReasonCode.MATRIX_SUPPORTED,)
        )
    if engine.backend == "fast_matrix":
        joined = ",".join(reason.value for reason in reasons)
        raise MatrixCapabilityError(f"fast_matrix is unsupported: {joined}")
    return BackendDecision(
        "auto", "event", True, tuple(reason.value for reason in reasons)
    )
