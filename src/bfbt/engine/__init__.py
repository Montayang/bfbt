"""Backtest execution engines and market frictions."""

from bfbt.engine.events import (
    ArbitrationBatch,
    EventArbitrationError,
    EventArbitrator,
    TradeLinkBatch,
    link_risk_event_fills,
)
from bfbt.engine.streaming import LedgerChunk, LedgerState, StreamingLedger
from bfbt.engine.vectorized import (
    BacktestError,
    BacktestResult,
    run_vectorized_backtest,
)


def __getattr__(name: str):
    if name in {
        "V2BacktestResult",
        "V2ExecutionCheckpoint",
        "run_v2_backtest",
        "run_v2_backtest_chunk",
    }:
        from bfbt.engine import v2

        return getattr(v2, name)
    if name == "run_v2_backtest_chunked":
        from bfbt.engine import v2_chunked

        return v2_chunked.run_v2_backtest_chunked
    raise AttributeError(name)


__all__ = [
    "ArbitrationBatch",
    "BacktestError",
    "BacktestResult",
    "EventArbitrationError",
    "EventArbitrator",
    "LedgerChunk",
    "LedgerState",
    "StreamingLedger",
    "TradeLinkBatch",
    "V2BacktestResult",
    "V2ExecutionCheckpoint",
    "link_risk_event_fills",
    "run_vectorized_backtest",
    "run_v2_backtest",
    "run_v2_backtest_chunk",
    "run_v2_backtest_chunked",
]
