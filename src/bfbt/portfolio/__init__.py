"""Portfolio construction from point-in-time factor scores."""

from bfbt.portfolio.base import PortfolioError, PortfolioResult
from bfbt.portfolio.constraints import construct_portfolio
from bfbt.portfolio.history import (
    RankDescentTracker,
    RankHistoryBuffer,
    RankStateBudgetExceeded,
)
from bfbt.portfolio.ranking import build_rank_snapshots


def __getattr__(name: str):
    if name in {
        "IncrementalPositionEngine",
        "PositionInstructionError",
        "PositionStateBudgetExceeded",
    }:
        from bfbt.portfolio import instructions

        return getattr(instructions, name)
    raise AttributeError(name)

__all__ = [
    "PortfolioError",
    "PortfolioResult",
    "RankHistoryBuffer",
    "RankDescentTracker",
    "RankStateBudgetExceeded",
    "IncrementalPositionEngine",
    "PositionInstructionError",
    "PositionStateBudgetExceeded",
    "build_rank_snapshots",
    "construct_portfolio",
]
