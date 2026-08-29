"""A32 economic kernel and point-in-time Event/V2 equivalence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
from polars.testing import assert_frame_equal

from bianbt.config.backtest import BacktestConfig
from bianbt.engine.fast_matrix.kernel import run_fast_matrix
from bianbt.engine.fast_matrix.target_schedule import build_target_schedule
from bianbt.engine.v2 import run_v2_backtest

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")
VERSION = "bars-a32"


def _config() -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "config_version": "v2",
            "engine": {"backend": "fast_matrix", "purpose": "research", "equivalence_audit": True},
            "run": {"start": START, "end": START + timedelta(minutes=5), "dataset_version": "snapshot-a32"},
            "schedule": {"factor_interval": "1m", "rebalance_interval": "2m", "signal_delay_bars": 1},
            "portfolio": {
                "selection": {"long": {"ranks": [1]}, "short": {"ranks": [2]}},
                "sizing": {"mode": "target_weight", "weighting": "equal", "target_gross_exposure": 1.0, "target_net_exposure": 0.0},
                "constraints": {},
                "holding": {"mode": "independent", "existing_signal": "add"},
            },
            "execution": {
                "fee": {"model": "fixed_bps", "taker_bps": 4.0},
                "slippage": {"model": "fixed_bps", "bps": 2.0},
                "funding": {"enabled": False},
            },
            "valuation": {"price": "trade_close"},
            "risk": {"leverage": 2.0, "evaluation_interval": "1m", "trigger_price": "trade", "fill_model": "next_bar_open", "intrabar_conflict": "worst_case", "reentry_policy": "next_scheduled_rebalance"},
            "capital": {"initial_equity": 10_000.0},
            "performance": {"mode": "in_memory", "max_input_rows_per_chunk": 1000},
        }
    )


def _bars() -> pl.DataFrame:
    rows = []
    prices = {
        "BTCUSDT": [(100, 101), (101, 103), (103, 102), (102, 104), (104, 105)],
        "ETHUSDT": [(50, 49), (49, 48), (48, 49), (49, 47), (47, 46)],
    }
    for minute in range(5):
        for symbol, values in prices.items():
            opened, closed = values[minute]
            rows.append(
                {"open_time": START + timedelta(minutes=minute), "close_time": START + timedelta(minutes=minute + 1), "symbol": symbol, "interval": "1m", "open": float(opened), "high": float(max(opened, closed)), "low": float(min(opened, closed)), "close": float(closed), "is_complete": True, "dataset_version": VERSION}
            )
    return pl.DataFrame(rows).with_columns(pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS))


def _targets() -> pl.DataFrame:
    rows = []
    for signal_minute, targets in ((0, {"BTCUSDT": 0.5, "ETHUSDT": -0.5}), (2, {"BTCUSDT": -0.5, "ETHUSDT": 0.5})):
        for symbol, weight in targets.items():
            rows.append({"signal_time": START + timedelta(minutes=signal_minute), "fill_time": START + timedelta(minutes=signal_minute + 1), "symbol": symbol, "target_weight": weight, "source_signal_id": "signal-a32", "factor_version": "factor-a32", "universe_version": "universe-a32", "portfolio_version": "portfolio-a32"})
    return pl.DataFrame(rows)


def _event_inputs(targets: pl.DataFrame) -> tuple[pl.LazyFrame, pl.LazyFrame]:
    strategy = targets.with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.lit(None, dtype=UTC_MS).alias("rank_source_time"),
        pl.when(pl.col("target_weight") > 0).then(pl.lit("LONG")).otherwise(pl.lit("SHORT")).alias("side"),
        pl.col("target_weight").abs().alias("score"),
    ).select("signal_time", "rank_source_time", "symbol", "score", "side", "target_weight")
    artifact = strategy.with_columns(
        pl.col("target_weight").alias("unconstrained_weight"),
        pl.lit("").alias("constraint_flags"),
        pl.lit("portfolio-a32").alias("portfolio_version"),
        pl.lit("").alias("run_id"),
    ).select("signal_time", "symbol", "score", "side", "unconstrained_weight", "target_weight", "constraint_flags", "portfolio_version", "run_id")
    return strategy.lazy(), artifact.lazy()


def test_matrix_matches_event_v2_for_drift_rebalance_reversal_and_costs() -> None:
    config = _config()
    target_rows = _targets()
    schedule = build_target_schedule(
        target_rows,
        rebalance_times=(START + timedelta(minutes=1), START + timedelta(minutes=3)),
        parent_manifest_sha256="b" * 64,
    )
    matrix = run_fast_matrix(schedule, _bars(), config=config, market_identity=VERSION)
    strategy, targets = _event_inputs(target_rows)
    event = run_v2_backtest(
        strategy, targets, pl.DataFrame().lazy(), _bars().lazy(), None, None,
        config=config, base_interval="1m", portfolio_version="portfolio-a32",
        bars_dataset_version=VERSION, mark_dataset_version=None, funding_dataset_version=None,
    )
    event_returns = event.result.returns.collect().drop("run_id")
    matrix_returns = matrix.returns.drop("run_id")
    assert_frame_equal(matrix_returns, event_returns, check_dtypes=True, rel_tol=1e-11, abs_tol=1e-11)
    assert matrix.rebalance_summary.height == event.result.trades.collect().height
    assert matrix.rebalance_summary["delta_notional"].abs().sum() > 20_000
