"""Portfolio construction result and error contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import polars as pl

if TYPE_CHECKING:
    from bfbt.portfolio.crossover import FactorCrossoverTracker
    from bfbt.portfolio.history import RankDescentTracker, RankHistoryBuffer


class PortfolioError(ValueError):
    """Factor scores cannot produce an unambiguous target portfolio."""


@dataclass(frozen=True)
class PortfolioResult:
    frame: pl.LazyFrame
    portfolio_version: str
    factor_version: str
    universe_version: str
    rankings: pl.LazyFrame | None = None
    full_rankings: pl.LazyFrame | None = None
    selections: pl.LazyFrame | None = None
    selection_diagnostics: pl.LazyFrame | None = None
    rank_state: RankHistoryBuffer | RankDescentTracker | FactorCrossoverTracker | None = None


PORTFOLIO_CODE_VERSION = "a13-portfolio-v1"
