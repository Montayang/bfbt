"""User-run A18 offline acceptance for the formal V2 event loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bianbt.config.backtest import BacktestConfig
from bianbt.data.v2_contracts import V2ReasonCode
from bianbt.engine.v2 import run_v2_backtest
from bianbt.engine.vectorized import BacktestError

START = datetime(2025, 1, 8, tzinfo=timezone.utc)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")
VERSION = "bars-a18"
PORTFOLIO_VERSION = "portfolio-a18"
UTC_MS = pl.Datetime("ms", "UTC")


def _config() -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "config_version": "v2",
            "run": {
                "name": "a18-offline",
                "start": START,
                "end": START + timedelta(minutes=6),
                "dataset_version": "snapshot-a18",
                "random_seed": 42,
            },
            "schedule": {
                "factor_interval": "1m",
                "rebalance_interval": "1m",
                "signal_delay_bars": 1,
            },
            "capital": {
                "currency": "USDT",
                "initial_equity": 1000.0,
                "margin_model": "simple_cross",
                "reserved_cost_buffer": 10.0,
            },
            "portfolio": {
                "selection": {
                    "mode": "rank_set",
                    "rank_order": "descending",
                    "clock": "rebalance",
                    "lag": 1,
                    "long": {"ranks": [2], "ranges": []},
                    "short": {"ranks": [1], "ranges": []},
                },
                "sizing": {
                    "mode": "fixed_margin",
                    "margin_amount": 100.0,
                    "reverse_policy": "flatten_then_open",
                },
                "constraints": {
                    "max_gross_exposure": 1.5,
                    "max_net_exposure": 1.0,
                    "max_symbol_weight": 0.8,
                    "max_symbol_notional": 600.0,
                    "max_consecutive_adds": 3,
                    "max_turnover": 1.0,
                },
            },
            "execution": {
                "fill_price": "next_bar_open",
                "partial_fill": False,
                "fee": {"model": "zero"},
                "slippage": {"model": "zero"},
                "funding": {"enabled": False, "missing_policy": "error"},
            },
            "valuation": {"price": "trade_close"},
            "risk": {
                "leverage": 2.0,
                "enforce_liquidation": False,
                "evaluation_interval": "1m",
                "trigger_price": "trade",
                "fill_model": "next_bar_open",
                "intrabar_conflict": "worst_case",
                "symbol_exits": {
                    "stop_loss": {
                        "enabled": True,
                        "distance": 0.01,
                        "action": "close",
                    },
                    "take_profit": {"enabled": False},
                    "trailing_stop": {"enabled": False},
                },
                "portfolio_exits": {
                    "stop_loss": None,
                    "take_profit": None,
                    "max_drawdown": None,
                },
                "cooldown_bars": 1,
                "reentry_policy": "next_scheduled_rebalance",
                "max_triggers_per_symbol": 1,
            },
            "output": {
                "root": "data/backtest/runs",
                "save_factor_values": True,
                "save_universe": True,
                "save_positions": True,
                "save_trades": True,
                "save_costs": True,
                "render_html": True,
            },
            "performance": {
                "mode": "in_memory",
                "chunk_interval": "1d",
                "max_input_rows_per_chunk": 10000,
                "max_incremental_rss_mib": 128,
                "collect_diagnostics": True,
                "max_rank_lag": 2,
                "max_rank_state_rows": 100,
                "max_position_state_rows": 10,
                "max_pending_instructions": 20,
                "max_risk_state_rows": 10,
                "max_pending_risk_intents": 20,
            },
        }
    )


def _bars() -> pl.LazyFrame:
    rows = []
    for index in range(7):
        opened = START + timedelta(minutes=index)
        for offset, symbol in enumerate(SYMBOLS):
            price = 100.0 + offset * 20.0
            low = 98.0 if symbol == "BTCUSDT" and index == 1 else price - 0.5
            rows.append(
                {
                    "open_time": opened,
                    "close_time": opened + timedelta(minutes=1),
                    "symbol": symbol,
                    "interval": "1m",
                    "open": price,
                    "high": price + 0.5,
                    "low": low,
                    "close": price,
                    "is_complete": True,
                    "dataset_version": VERSION,
                }
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    ).lazy()


def _strategy() -> pl.LazyFrame:
    rows = [
        {
            "signal_time": START,
            "rank_source_time": START - timedelta(minutes=1),
            "symbol": "BTCUSDT",
            "score": 2.0,
            "side": "LONG",
        },
        {
            "signal_time": START,
            "rank_source_time": START - timedelta(minutes=1),
            "symbol": "ETHUSDT",
            "score": 3.0,
            "side": "SHORT",
        },
        {
            "signal_time": START + timedelta(minutes=1),
            "rank_source_time": START,
            "symbol": "BTCUSDT",
            "score": 2.1,
            "side": "LONG",
        },
        {
            "signal_time": START + timedelta(minutes=1),
            "rank_source_time": START,
            "symbol": "ETHUSDT",
            "score": 3.1,
            "side": "SHORT",
        },
        {
            "signal_time": START + timedelta(minutes=2),
            "rank_source_time": START + timedelta(minutes=1),
            "symbol": "BTCUSDT",
            "score": 2.2,
            "side": "LONG",
        },
    ]
    return pl.DataFrame(rows).with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.col("rank_source_time").cast(UTC_MS),
    ).lazy()


def _targets() -> pl.LazyFrame:
    return _strategy().with_columns(
        pl.lit(0.0).alias("unconstrained_weight"),
        pl.lit(0.0).alias("target_weight"),
        pl.lit("INCREMENTAL_SIZING").alias("constraint_flags"),
        pl.lit(PORTFOLIO_VERSION).alias("portfolio_version"),
    )


def _rankings() -> pl.LazyFrame:
    rows = []
    for index in range(3):
        timestamp = START + timedelta(minutes=index)
        for rank, symbol in enumerate(("ETHUSDT", "BTCUSDT", "BNBUSDT"), start=1):
            rows.append(
                {
                    "timestamp": timestamp,
                    "rank_clock": "rebalance",
                    "symbol": symbol,
                    "factor_name": "momentum",
                    "raw_score": float(4 - rank),
                    "ordinal_rank": rank,
                    "percentile_rank": float(3 - rank) / 2.0,
                    "sample_count": 3,
                    "factor_version": "momentum-v1",
                    "universe_version": "universe-v1",
                    "run_id": "placeholder",
                }
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("timestamp").cast(UTC_MS)
    ).lazy()


def _run(strategy: pl.LazyFrame | None = None):
    return run_v2_backtest(
        strategy if strategy is not None else _strategy(),
        _targets(),
        _rankings(),
        _bars(),
        None,
        None,
        config=_config(),
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=VERSION,
        mark_dataset_version=None,
        funding_dataset_version=None,
    )


def test_a18_v2_full_event_loop_is_deterministic_and_auditable() -> None:
    first = _run()
    second = _run()

    assert first.result.result_hash == second.result.result_hash
    assert first.audit_result_hash == second.audit_result_hash
    assert first.result.returns.collect().height == 7
    assert first.linked_trades.filter(
        pl.col("source_event_id").is_not_null()
    ).height == 1
    assert first.risk_events.filter(
        pl.col("reason_code") == V2ReasonCode.STOP_LOSS_TRIGGERED.value
    ).height == 1
    assert first.position_instructions.filter(
        pl.col("reason_code")
        == V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value
    ).height == 1
    lagged = first.position_instructions.filter(
        (pl.col("priority") == 400)
        & pl.col("rank_source_time").is_not_null()
    )
    assert lagged.height
    assert not lagged.filter(
        pl.col("rank_source_time") >= pl.col("decision_time")
    ).height
    assert first.result.diagnostics == {
        "max_position_state_rows_observed": 2,
        "max_risk_state_rows_observed": 2,
        "max_pending_risk_intents_observed": 1,
        "input_trade_bar_rows": 21,
        "input_risk_bar_rows": 21,
    }


def test_a18_next_open_without_market_data_is_terminal_failure() -> None:
    invalid = pl.DataFrame(
        {
            "signal_time": [START + timedelta(minutes=6)],
            "rank_source_time": [START + timedelta(minutes=5)],
            "symbol": ["BTCUSDT"],
            "score": [1.0],
            "side": ["LONG"],
        }
    ).with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.col("rank_source_time").cast(UTC_MS),
    ).lazy()
    with pytest.raises(BacktestError, match="fill times have no trade bar"):
        _run(invalid)
