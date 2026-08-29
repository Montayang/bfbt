"""A33 funding, mark valuation, chunk checkpoint, and hard-row gates."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from bianbt.config.backtest import BacktestConfig
from bianbt.engine.fast_matrix.chunked import run_fast_matrix_chunked
from bianbt.engine.fast_matrix.kernel import MatrixExecutionError, run_fast_matrix
from bianbt.engine.fast_matrix.target_schedule import build_target_schedule

START = datetime(2026, 2, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")


def _config(*, mode: str = "chunked", valuation: str = "mark_close") -> BacktestConfig:
    performance = {"mode": mode, "chunk_interval": "2m", "max_input_rows_per_chunk": 1000, "max_rank_lag": 24}
    if mode == "chunked":
        performance["max_process_rss_mib"] = 1024
    return BacktestConfig.model_validate({
        "config_version": "v2", "engine": {"backend": "fast_matrix", "purpose": "research"},
        "schedule": {"factor_interval": "1m", "rebalance_interval": "2m", "signal_delay_bars": 1},
        "portfolio": {"selection": {"long": {"ranks": [1]}}, "sizing": {"mode": "target_weight", "weighting": "equal", "target_gross_exposure": 0.5, "target_net_exposure": 0.5}},
        "execution": {"fee": {"model": "zero"}, "slippage": {"model": "zero"}, "funding": {"enabled": True, "missing_policy": "error"}},
        "valuation": {"price": valuation},
        "risk": {"leverage": 2.0, "evaluation_interval": "1m", "trigger_price": "trade", "fill_model": "next_bar_open", "intrabar_conflict": "worst_case", "reentry_policy": "next_scheduled_rebalance"},
        "capital": {"initial_equity": 1000.0}, "performance": performance,
    })


def _bars(*, mark: bool = False) -> pl.DataFrame:
    rows = []
    for minute in range(6):
        base = 100.0 + minute
        rows.append({"open_time": START + timedelta(minutes=minute), "close_time": START + timedelta(minutes=minute + 1), "symbol": "BTCUSDT", "open": base, "close": base + (1.0 if not mark else 0.5)})
    return pl.DataFrame(rows).with_columns(pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS))


def _schedule():
    rows = pl.DataFrame({
        "signal_time": [START, START + timedelta(minutes=2)],
        "fill_time": [START + timedelta(minutes=1), START + timedelta(minutes=3)],
        "symbol": ["BTCUSDT", "BTCUSDT"], "target_weight": [0.5, 0.0],
        "source_signal_id": ["signal-a33"] * 2, "factor_version": ["factor-a33"] * 2,
        "universe_version": ["universe-a33"] * 2, "portfolio_version": ["portfolio-a33"] * 2,
    })
    return build_target_schedule(rows, rebalance_times=(START + timedelta(minutes=1), START + timedelta(minutes=3)), parent_manifest_sha256="c" * 64)


def _funding() -> pl.DataFrame:
    return pl.DataFrame({"funding_time": [START + timedelta(minutes=2)], "symbol": ["BTCUSDT"], "funding_rate": [0.001], "mark_price": [102.0]}).with_columns(pl.col("funding_time").cast(UTC_MS))


def test_chunked_checkpoint_matches_continuous_with_mark_and_funding() -> None:
    config = _config()
    full = run_fast_matrix(_schedule(), _bars(), config=config, market_identity="a33", mark_bars=_bars(mark=True), funding=_funding())
    chunked = run_fast_matrix_chunked(_schedule(), _bars().lazy(), config=config, market_identity="a33", mark_bars=_bars(mark=True).lazy(), funding=_funding().lazy())
    assert_frame_equal(full.returns, chunked.returns, rel_tol=1e-12, abs_tol=1e-12)
    assert full.checkpoint == chunked.checkpoint
    assert chunked.diagnostics["chunk_count"] == 3
    assert chunked.returns["funding_return"].abs().sum() > 0


def test_hard_market_row_gate_fails_before_success() -> None:
    with pytest.raises(MatrixExecutionError, match="hard limit"):
        run_fast_matrix(_schedule(), _bars(), config=_config(), market_identity="a33", mark_bars=_bars(mark=True), funding=_funding(), max_market_rows=2)


def test_held_symbol_without_current_bar_defers_rebalance_without_fake_fill() -> None:
    bars = _bars().filter(
        ~((pl.col("open_time") == START + timedelta(minutes=3)) & (pl.col("symbol") == "BTCUSDT"))
    )
    # Preserve the minute-3 market clock with an unrelated executable symbol.
    extra = pl.DataFrame([{
        "open_time": START + timedelta(minutes=3),
        "close_time": START + timedelta(minutes=4),
        "symbol": "ETHUSDT", "open": 50.0, "close": 50.0,
    }]).with_columns(pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS))
    result = run_fast_matrix(
        _schedule(), pl.concat([bars, extra]), config=_config(valuation="trade_close"),
        market_identity="a33-missing-held", funding=_funding(),
    )
    assert result.rebalance_summary.filter(
        pl.col("fill_time") == START + timedelta(minutes=3)
    ).is_empty()
    btc = result.checkpoint.symbols.index("BTCUSDT")
    assert result.checkpoint.quantities[btc] != 0.0
