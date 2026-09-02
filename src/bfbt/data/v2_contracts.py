"""Stable V2 event priorities and auditable reason codes."""

from __future__ import annotations

from enum import IntEnum, unique
from typing import Any

from bfbt.compat import StrEnum
from bfbt.data.hashing import content_sha256

V2_EVENT_CONTRACT_VERSION = "events/v3"


@unique
class EventPriority(IntEnum):
    """Lower numeric values win intent arbitration."""

    LIQUIDATION_RESERVED = 0
    PORTFOLIO_RISK = 100
    SYMBOL_RISK = 200
    UNIVERSE_FORCED_EXIT = 300
    SCHEDULED_STRATEGY = 400


@unique
class V2ReasonCode(StrEnum):
    """Closed reason-code vocabulary shared by artifacts and reports."""

    SELECTED_CURRENT_RANK = "SELECTED_CURRENT_RANK"
    SELECTED_HISTORICAL_RANK = "SELECTED_HISTORICAL_RANK"
    RANK_OUT_OF_RANGE = "RANK_OUT_OF_RANGE"
    INSUFFICIENT_RANK_HISTORY = "INSUFFICIENT_RANK_HISTORY"
    HISTORICAL_RANK_NOT_CURRENTLY_ELIGIBLE = (
        "HISTORICAL_RANK_NOT_CURRENTLY_ELIGIBLE"
    )
    NOT_SELECTED = "NOT_SELECTED"
    RANK_DESCENT_TRIGGERED = "RANK_DESCENT_TRIGGERED"

    ACCEPTED = "ACCEPTED"
    SCALED_MAX_GROSS_EXPOSURE = "SCALED_MAX_GROSS_EXPOSURE"
    SCALED_MAX_NET_EXPOSURE = "SCALED_MAX_NET_EXPOSURE"
    SCALED_MAX_SYMBOL_WEIGHT = "SCALED_MAX_SYMBOL_WEIGHT"
    SCALED_MAX_SYMBOL_NOTIONAL = "SCALED_MAX_SYMBOL_NOTIONAL"
    REJECTED_MAX_CONSECUTIVE_ADDS = "REJECTED_MAX_CONSECUTIVE_ADDS"
    SCALED_MAX_TURNOVER = "SCALED_MAX_TURNOVER"
    REJECTED_INSUFFICIENT_MARGIN = "REJECTED_INSUFFICIENT_MARGIN"
    ZERO_POSITION_SKIPPED = "ZERO_POSITION_SKIPPED"
    ALREADY_HELD = "ALREADY_HELD"
    REPLACED_BY_SIGNAL = "REPLACED_BY_SIGNAL"

    STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
    TAKE_PROFIT_TRIGGERED = "TAKE_PROFIT_TRIGGERED"
    TRAILING_STOP_TRIGGERED = "TRAILING_STOP_TRIGGERED"
    PORTFOLIO_STOP_LOSS_TRIGGERED = "PORTFOLIO_STOP_LOSS_TRIGGERED"
    PORTFOLIO_TAKE_PROFIT_TRIGGERED = "PORTFOLIO_TAKE_PROFIT_TRIGGERED"
    PORTFOLIO_MAX_DRAWDOWN_TRIGGERED = "PORTFOLIO_MAX_DRAWDOWN_TRIGGERED"
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"
    SUPPRESSED_BY_HIGHER_PRIORITY = "SUPPRESSED_BY_HIGHER_PRIORITY"
    UNIVERSE_FORCED_EXIT = "UNIVERSE_FORCED_EXIT"
    END_OF_DATA_UNFILLED = "END_OF_DATA_UNFILLED"


def event_contract_descriptor() -> dict[str, Any]:
    """Return the canonical, JSON-safe V2 event contract."""

    return {
        "contract_version": V2_EVENT_CONTRACT_VERSION,
        "priority_semantics": "lower_numeric_value_wins",
        "priorities": [
            {"name": item.name, "value": int(item)}
            for item in EventPriority
        ],
        "reason_codes": [item.value for item in V2ReasonCode],
        "rank_semantics": {
            "direction": "score_descending",
            "first_rank": 1,
            "tie_break": ["score_desc", "symbol_asc"],
        },
    }


def event_contract_fingerprint() -> str:
    """Return a stable SHA-256 over priority, reasons, and rank semantics."""

    return content_sha256(event_contract_descriptor())
