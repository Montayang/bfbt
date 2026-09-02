"""A30 acceptance for minute-live factor crossings and strategy flat intents."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bfbt.config.backtest import BacktestConfig, FactorCrossoverConfig, RankSelectionConfig
from bfbt.engine.v2 import run_v2_backtest
from bfbt.portfolio.constraints import finalize_v2_selections
from bfbt.portfolio.crossover import FactorCrossoverTracker
from bfbt.portfolio.ranking import RANKING_SCHEMA

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")
FACTOR_VERSION = "ema-ratio-a30"
UNIVERSE_VERSION = "universe-a30"
BARS_VERSION = "bars-a30"


def _selection() -> RankSelectionConfig:
    return RankSelectionConfig.model_validate(
        {
            "mode": "factor_crossover",
            "clock": "factor",
            "lag": 0,
            "long": {"ranks": [], "ranges": []},
            "short": {"ranks": [], "ranges": []},
            "crossover": {
                "entry_threshold": 0.0,
                "exit_threshold": 0.0,
                "entry_when": "cross_above",
                "exit_when": "cross_below",
                "gap_policy": "reset",
                "initial_policy": "wait_for_cross",
            },
        }
    )


def _scores(values: list[tuple[int, str, float]]) -> pl.LazyFrame:
    return pl.DataFrame(
        [
            {
                "timestamp": START + timedelta(minutes=minute),
                "symbol": symbol,
                "value": value,
                "is_valid": True,
                "factor_version": FACTOR_VERSION,
                "universe_version": UNIVERSE_VERSION,
            }
            for minute, symbol, value in values
        ]
    ).with_columns(pl.col("timestamp").cast(UTC_MS)).lazy()


def _tracker(state: pl.DataFrame | None = None) -> FactorCrossoverTracker:
    return FactorCrossoverTracker(
        config=FactorCrossoverConfig(),
        max_state_rows=20,
        restored_state=state,
    )


def test_crossover_config_requires_factor_clock_and_no_rank_sides() -> None:
    with pytest.raises(ValueError, match="clock=factor"):
        RankSelectionConfig.model_validate(
            {
                "mode": "factor_crossover",
                "clock": "rebalance",
                "crossover": {},
            }
        )
    with pytest.raises(ValueError, match="must not configure rank_set"):
        RankSelectionConfig.model_validate(
            {
                "mode": "factor_crossover",
                "clock": "factor",
                "long": {"ranks": [1]},
                "crossover": {},
            }
        )


def test_genuine_crosses_emit_long_then_flat_without_initial_entry() -> None:
    scores = _scores(
        [(0, "BTCUSDT", 0.02), (1, "BTCUSDT", -0.01),
         (2, "BTCUSDT", 0.01), (3, "BTCUSDT", 0.02),
         (4, "BTCUSDT", -0.02)]
    )
    selected = _tracker().select(
        scores,
        decision_times=scores.select("timestamp"),
        selection=_selection(),
    ).collect()
    assert selected.select("signal_time", "side").to_dicts() == [
        {"signal_time": START + timedelta(minutes=1), "side": "FLAT"},
        {"signal_time": START + timedelta(minutes=2), "side": "LONG"},
        {"signal_time": START + timedelta(minutes=4), "side": "FLAT"},
    ]


def test_gap_resets_cross_state_and_chunk_checkpoint_is_exact() -> None:
    first_scores = _scores([(0, "BTCUSDT", -0.01), (1, "BTCUSDT", 0.01)])
    first_tracker = _tracker()
    first = first_tracker.select(
        first_scores,
        decision_times=first_scores.select("timestamp"),
        selection=_selection(),
    ).collect()
    assert first["side"].to_list() == ["LONG"]

    second_scores = _scores([(2, "ETHUSDT", -0.01), (3, "BTCUSDT", 0.02)])
    restored = _tracker(first_tracker.export_state())
    second = restored.select(
        second_scores,
        decision_times=second_scores.select("timestamp"),
        selection=_selection(),
    ).collect()
    assert second.is_empty()
    assert restored.export_state()["symbol"].to_list() == ["BTCUSDT"]


def _config() -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "config_version": "v2",
            "run": {
                "name": "a30-crossover",
                "start": START,
                "end": START + timedelta(minutes=6),
                "dataset_version": "snapshot-a30",
            },
            "schedule": {
                "factor_interval": "1m",
                "rebalance_interval": "1m",
                "signal_delay_bars": 1,
            },
            "capital": {
                "initial_equity": 10_000.0,
                "margin_model": "simple_cross",
                "reserved_cost_buffer": 0.0,
            },
            "portfolio": {
                "selection": _selection().model_dump(mode="json"),
                "sizing": {
                    "mode": "fixed_margin",
                    "margin_amount": 10.0,
                    "reverse_policy": "net_delta",
                },
                "constraints": {
                    "max_gross_exposure": 5.0,
                    "max_net_exposure": 5.0,
                    "max_symbol_notional": 50.0,
                },
                "holding": {"mode": "independent", "existing_signal": "ignore"},
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
                "leverage": 5.0,
                "enforce_liquidation": False,
                "evaluation_interval": "1m",
                "trigger_price": "trade",
                "fill_model": "same_bar_trigger",
                "gap_policy": "worse_executable",
                "intrabar_conflict": "worst_case",
                "symbol_exits": {
                    "stop_loss": {"enabled": False},
                    "take_profit": {"enabled": False},
                    "trailing_stop": {"enabled": False},
                },
                "portfolio_exits": {},
                "cooldown_bars": 0,
                "reentry_policy": "next_scheduled_rebalance",
            },
            "output": {"root": "/tmp/a30-runs"},
            "performance": {
                "mode": "in_memory",
                "chunk_interval": "1d",
                "max_input_rows_per_chunk": 1000,
                "max_incremental_rss_mib": 128,
                "collect_diagnostics": True,
                "max_rank_lag": 0,
                "max_rank_state_rows": 20,
                "max_position_state_rows": 20,
                "max_pending_instructions": 20,
                "max_risk_state_rows": 20,
                "max_pending_risk_intents": 20,
            },
        }
    )


def _bars() -> pl.LazyFrame:
    return pl.DataFrame(
        [
            {
                "open_time": START + timedelta(minutes=minute),
                "close_time": START + timedelta(minutes=minute + 1),
                "symbol": "BTCUSDT",
                "interval": "1m",
                "open": 100.0 + minute,
                "high": 101.0 + minute,
                "low": 99.0 + minute,
                "close": 100.5 + minute,
                "is_complete": True,
                "dataset_version": BARS_VERSION,
            }
            for minute in range(6)
        ]
    ).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    ).lazy()


def test_strategy_flat_signal_closes_only_the_existing_long() -> None:
    config = _config()
    assert hasattr(config.portfolio, "selection")
    scores = _scores(
        [(0, "BTCUSDT", -0.01), (1, "BTCUSDT", 0.01),
         (2, "BTCUSDT", 0.02), (3, "BTCUSDT", -0.01)]
    )
    tracker = _tracker()
    selections = tracker.select(
        scores,
        decision_times=scores.select("timestamp"),
        selection=_selection(),
    )
    targets, portfolio_version = finalize_v2_selections(
        selections,
        config.portfolio,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )
    result = run_v2_backtest(
        selections,
        targets,
        pl.DataFrame(schema=RANKING_SCHEMA).lazy(),
        _bars(),
        None,
        None,
        config=config,
        base_interval="1m",
        portfolio_version=portfolio_version,
        bars_dataset_version=BARS_VERSION,
        mark_dataset_version=None,
        funding_dataset_version=None,
    )
    assert result.linked_trades["side"].to_list() == ["BUY", "SELL"]
    assert result.linked_trades["fill_time"].to_list() == [
        START + timedelta(minutes=2),
        START + timedelta(minutes=4),
    ]
    assert result.checkpoint.position.positions.is_empty()


def test_report_explains_crossover_without_claiming_rank_selection() -> None:
    from bfbt.reports.renderer import _v2_audit_html

    config = _config().model_dump(mode="json")
    html = _v2_audit_html(
        {
            "factor": {
                "factors": [
                    {
                        "name": "intrabar_ema_ratio",
                        "version": "v1",
                        "compute_interval": "1m",
                        "parameters": {
                            "source_interval": "15m",
                            "fast_span": 7,
                            "slow_span": 25,
                        },
                    }
                ]
            },
            "backtest": config,
        }
    )
    assert "逐合约因子穿越" in html
    assert "首次有效值不触发" in html
    assert "固定保证金 / Fixed Margin</th><td>10" in html
    assert "做多 Rank" not in html
