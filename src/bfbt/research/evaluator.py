"""Composition of lazy factor research diagnostics."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from bfbt.research.diagnostics import coverage_report
from bfbt.research.ic import information_coefficient
from bfbt.research.quantiles import quantile_returns
from bfbt.research.turnover import factor_rank_turnover


@dataclass(frozen=True)
class FactorEvaluation:
    ic: pl.LazyFrame
    quantile_returns: pl.LazyFrame
    coverage: pl.LazyFrame
    turnover: pl.LazyFrame


def evaluate_factor(
    factors: pl.LazyFrame,
    labels: pl.LazyFrame,
    universe: pl.LazyFrame,
    *,
    universe_version: str,
    quantiles: int = 5,
) -> FactorEvaluation:
    """Build lazy aligned research outputs without mutating factor values."""

    return FactorEvaluation(
        ic=information_coefficient(factors, labels),
        quantile_returns=quantile_returns(
            factors, labels, quantiles=quantiles
        ),
        coverage=coverage_report(
            factors, labels, universe, universe_version=universe_version
        ),
        turnover=factor_rank_turnover(factors),
    )
