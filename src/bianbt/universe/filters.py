"""Stable point-in-time universe eligibility reason codes."""

from __future__ import annotations

from enum import Enum


class UniverseReason(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    EXPLICITLY_EXCLUDED = "EXPLICITLY_EXCLUDED"
    NO_CONTRACT_SNAPSHOT = "NO_CONTRACT_SNAPSHOT"
    NOT_PERPETUAL = "NOT_PERPETUAL"
    WRONG_QUOTE_ASSET = "WRONG_QUOTE_ASSET"
    WRONG_MARGIN_ASSET = "WRONG_MARGIN_ASSET"
    NOT_LISTED = "NOT_LISTED"
    DELISTED = "DELISTED"
    NOT_TRADING = "NOT_TRADING"
    WARMUP = "WARMUP"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    MISSING_DATA = "MISSING_DATA"
    ILLIQUID = "ILLIQUID"


EXCLUSION_PRIORITY = tuple(
    item for item in UniverseReason if item is not UniverseReason.ELIGIBLE
)
