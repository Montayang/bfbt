"""Factor protocol, canonical inputs, and versioned result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import polars as pl

from bianbt.config.factor import FactorDefinition


class FactorError(ValueError):
    """A factor cannot be computed with unambiguous point-in-time semantics."""


@dataclass(frozen=True)
class FactorResult:
    frame: pl.LazyFrame
    factor_name: str
    factor_version: str
    bars_dataset_version: str
    universe_version: str
    base_interval: str
    state: pl.DataFrame | None = None


class Factor(Protocol):
    name: str
    version: str
    required_columns: tuple[str, ...]

    def compute_raw(
        self,
        bars: pl.LazyFrame,
        definition: FactorDefinition,
        *,
        base_interval: str,
    ) -> pl.LazyFrame:
        """Return timestamp, symbol, raw_value using no future rows."""


def require_columns(frame: pl.LazyFrame, columns: tuple[str, ...]) -> None:
    missing = set(columns) - set(frame.collect_schema().names())
    if missing:
        raise FactorError(f"bar input is missing columns: {sorted(missing)}")
