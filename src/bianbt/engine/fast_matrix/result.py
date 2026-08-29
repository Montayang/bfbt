"""Public Fast Matrix research result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import polars as pl


@dataclass(frozen=True)
class MatrixCheckpoint:
    identity_sha256: str
    symbols: tuple[str, ...]
    quantities: tuple[float, ...]
    average_entry_prices: tuple[float, ...]
    last_close_prices: tuple[float | None, ...]
    cash: float
    previous_equity: float
    peak_equity: float
    sequence: int
    processed_bars: int


@dataclass(frozen=True)
class MatrixResult:
    run_id: str
    result_hash: str
    returns: pl.DataFrame
    rebalance_summary: pl.DataFrame
    checkpoint: MatrixCheckpoint
    warnings: tuple[str, ...]
    diagnostics: Mapping[str, object]
