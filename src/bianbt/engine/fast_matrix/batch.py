"""Multi-candidate runner sharing one immutable market materialization."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import polars as pl

from bianbt.config.backtest import BacktestConfig
from bianbt.engine.fast_matrix.kernel import MatrixExecutionError, run_fast_matrix
from bianbt.engine.fast_matrix.result import MatrixResult
from bianbt.engine.fast_matrix.target_schedule import TargetSchedule


@dataclass(frozen=True)
class MatrixBatchResult:
    candidates: Mapping[str, MatrixResult]
    diagnostics: Mapping[str, object]


def run_fast_matrix_batch(
    candidates: Mapping[str, TargetSchedule],
    trade_bars: pl.DataFrame | pl.LazyFrame,
    *, config: BacktestConfig, market_identity: str,
    max_candidates: int = 64,
) -> MatrixBatchResult:
    if not candidates or len(candidates) > max_candidates:
        raise MatrixExecutionError(
            f"candidate count must be within 1..{max_candidates}"
        )
    shared = trade_bars.collect(engine="streaming") if isinstance(trade_bars, pl.LazyFrame) else trade_bars
    results = {
        name: run_fast_matrix(
            schedule, shared, config=config,
            market_identity=f"{market_identity}:{name}",
        )
        for name, schedule in sorted(candidates.items())
    }
    return MatrixBatchResult(
        candidates=results,
        diagnostics={
            "candidate_count": len(results), "shared_market_loads": 1,
            "shared_market_rows": shared.height,
        },
    )
