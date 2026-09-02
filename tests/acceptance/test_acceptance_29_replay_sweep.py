"""A29 acceptance for bounded shared-input replay parameter sweeps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bfbt.config.backtest import BacktestConfig
from bfbt.portfolio.ranking import RANKING_SCHEMA
from bfbt.research.replay_sweep import (
    ReplaySweepCandidate,
    ReplaySweepError,
    run_chunked_replay_sweep,
    run_replay_sweep,
)

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")


def _config(
    *, margin: float, entry_threshold: float = 0.0, mode: str = "in_memory"
) -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "config_version": "v2",
            "run": {
                "name": f"sweep-{margin}",
                "start": START,
                "end": START + timedelta(minutes=5),
                "dataset_version": "snapshot-a29",
            },
            "schedule": {
                "factor_interval": "1m",
                "rebalance_interval": "1m",
                "signal_delay_bars": 1,
            },
            "capital": {
                "initial_equity": 1000.0,
                "margin_model": "simple_cross",
                "reserved_cost_buffer": 0.0,
            },
            "portfolio": {
                "selection": {
                    "mode": "factor_crossover",
                    "clock": "factor",
                    "lag": 0,
                    "long": {"ranks": [], "ranges": []},
                    "short": {"ranks": [], "ranges": []},
                    "crossover": {
                        "entry_threshold": entry_threshold,
                        "exit_threshold": 0.0,
                    },
                },
                "sizing": {
                    "mode": "fixed_margin",
                    "margin_amount": margin,
                    "reverse_policy": "net_delta",
                },
                "constraints": {
                    "max_gross_exposure": 5.0,
                    "max_net_exposure": 5.0,
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
                "leverage": 2.0,
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
            "performance": {
                "mode": mode,
                "chunk_interval": "2m" if mode == "chunked" else "1d",
                "max_input_rows_per_chunk": 1000,
                "max_incremental_rss_mib": 128,
                "collect_diagnostics": True,
                "max_rank_lag": 0,
                "max_rank_state_rows": 20,
                "max_position_state_rows": 20,
                "max_pending_instructions": 20,
                "max_risk_state_rows": 20,
                "max_pending_risk_intents": 20,
                **(
                    {"max_process_rss_mib": 5632, "resume_policy": "resume"}
                    if mode == "chunked" else {}
                ),
            },
        }
    )


def _selections() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "signal_time": START,
                "rank_source_time": None,
                "symbol": "BTCUSDT",
                "score": 0.01,
                "side": "LONG",
                "factor_version": "factor-a29",
                "universe_version": "universe-a29",
            },
            {
                "signal_time": START + timedelta(minutes=3),
                "rank_source_time": None,
                "symbol": "BTCUSDT",
                "score": -0.01,
                "side": "FLAT",
                "factor_version": "factor-a29",
                "universe_version": "universe-a29",
            },
        ]
    ).with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.col("rank_source_time").cast(UTC_MS),
    )


def _bars() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "open_time": START + timedelta(minutes=minute),
                "close_time": START + timedelta(minutes=minute + 1),
                "symbol": "BTCUSDT",
                "interval": "1m",
                "open": 100.0 + 5 * minute,
                "high": 101.0 + 5 * minute,
                "low": 99.0 + 5 * minute,
                "close": 100.0 + 5 * minute,
                "is_complete": True,
                "dataset_version": "bars-a29",
            }
            for minute in range(5)
        ]
    ).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    )


def _run(candidates: tuple[ReplaySweepCandidate, ...]):
    return run_replay_sweep(
        candidates=candidates,
        selections=_selections(),
        rankings=pl.DataFrame(schema=RANKING_SCHEMA),
        trade_bars=_bars(),
        mark_bars=None,
        funding=None,
        base_interval="1m",
        factor_version="factor-a29",
        universe_version="universe-a29",
        bars_dataset_version="bars-a29",
        mark_dataset_version=None,
        funding_dataset_version=None,
    )


def test_sweep_reuses_inputs_but_keeps_economic_state_independent() -> None:
    results = _run(
        (
            ReplaySweepCandidate("margin-10", _config(margin=10.0)),
            ReplaySweepCandidate("margin-20", _config(margin=20.0)),
        )
    )
    assert [item.name for item in results] == ["margin-10", "margin-20"]
    assert [item.trade_count for item in results] == [2, 2]
    assert results[1].ending_equity - 1000.0 == pytest.approx(
        2.0 * (results[0].ending_equity - 1000.0)
    )
    assert results[0].result_hash != results[1].result_hash


def test_sweep_rejects_selection_changes_and_unbounded_candidate_count() -> None:
    with pytest.raises(ReplaySweepError, match="must share"):
        _run(
            (
                ReplaySweepCandidate("base", _config(margin=10.0)),
                ReplaySweepCandidate(
                    "different-signal",
                    _config(margin=10.0, entry_threshold=0.01),
                ),
            )
        )


def test_chunked_sweep_loads_each_slice_once_and_matches_single_load_economics() -> None:
    expected = _run(
        (
            ReplaySweepCandidate("margin-10", _config(margin=10.0)),
            ReplaySweepCandidate("margin-20", _config(margin=20.0)),
        )
    )
    actual = run_chunked_replay_sweep(
        candidates=(
            ReplaySweepCandidate(
                "margin-10", _config(margin=10.0, mode="chunked")
            ),
            ReplaySweepCandidate(
                "margin-20", _config(margin=20.0, mode="chunked")
            ),
        ),
        selections=_selections().lazy(),
        rankings=pl.DataFrame(schema=RANKING_SCHEMA).lazy(),
        trade_bars=_bars().lazy(),
        mark_bars=None,
        funding=None,
        execution_start=START,
        execution_end=START + timedelta(minutes=5),
        base_interval="1m",
        factor_version="factor-a29",
        universe_version="universe-a29",
        bars_dataset_version="bars-a29",
        mark_dataset_version=None,
        funding_dataset_version=None,
    )
    for reference, chunked in zip(expected, actual, strict=True):
        assert chunked.name == reference.name
        assert chunked.ending_equity == pytest.approx(reference.ending_equity)
        assert chunked.total_return == pytest.approx(reference.total_return)
        assert chunked.max_drawdown == pytest.approx(reference.max_drawdown)
        assert chunked.trade_count == reference.trade_count
    with pytest.raises(ReplaySweepError, match="exceeds"):
        run_replay_sweep(
            candidates=(ReplaySweepCandidate("only", _config(margin=10.0)),),
            selections=_selections(),
            rankings=pl.DataFrame(schema=RANKING_SCHEMA),
            trade_bars=_bars(),
            mark_bars=None,
            funding=None,
            base_interval="1m",
            factor_version="factor-a29",
            universe_version="universe-a29",
            bars_dataset_version="bars-a29",
            mark_dataset_version=None,
            funding_dataset_version=None,
            max_candidates=0,
        )
