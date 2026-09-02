"""A31 backend, compatibility, and TargetSchedule contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bfbt.config.backtest import BacktestConfig
from bfbt.engine.fast_matrix.capabilities import MatrixCapabilityError, plan_backend
from bfbt.engine.fast_matrix.target_schedule import TargetScheduleError, build_target_schedule

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _payload(*, engine: dict[str, object] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "config_version": "v2",
        "schedule": {"factor_interval": "1m", "rebalance_interval": "1m", "signal_delay_bars": 1},
        "portfolio": {
            "selection": {"long": {"ranks": [1]}},
            "sizing": {"mode": "target_weight", "weighting": "equal", "target_gross_exposure": 1.0, "target_net_exposure": 0.0},
            "constraints": {},
            "holding": {"mode": "independent", "existing_signal": "add"},
        },
        "execution": {"fee": {"model": "zero"}, "slippage": {"model": "zero"}, "funding": {"enabled": False}},
        "valuation": {"price": "trade_close"},
        "risk": {
            "leverage": 2.0, "evaluation_interval": "1m", "trigger_price": "trade",
            "fill_model": "next_bar_open", "intrabar_conflict": "worst_case",
            "reentry_policy": "next_scheduled_rebalance",
        },
        "capital": {"initial_equity": 1000.0},
        "performance": {"max_rank_lag": 24},
    }
    if engine is not None:
        payload["engine"] = engine
    return payload


def test_old_config_has_no_serialized_engine_and_keeps_event_dispatch() -> None:
    config = BacktestConfig.model_validate(_payload())
    assert "engine" not in config.model_dump(mode="json")
    assert plan_backend(config).selected_backend == "event"


def test_planner_supports_matrix_but_formal_or_dynamic_sizing_fails_closed() -> None:
    matrix = BacktestConfig.model_validate(
        _payload(engine={"backend": "fast_matrix", "purpose": "research"})
    )
    assert plan_backend(matrix).selected_backend == "fast_matrix"

    formal_payload = _payload(engine={"backend": "fast_matrix", "purpose": "formal"})
    with pytest.raises(MatrixCapabilityError, match="FORMAL_REQUIRES_EVENT"):
        plan_backend(BacktestConfig.model_validate(formal_payload))

    dynamic = _payload(engine={"backend": "fast_matrix", "purpose": "research"})
    dynamic["portfolio"]["sizing"] = {  # type: ignore[index]
        "mode": "fixed_notional", "notional_amount": 100.0, "reverse_policy": "net_delta"
    }
    with pytest.raises(MatrixCapabilityError, match="UNSUPPORTED_STATE_DEPENDENT_SIZING"):
        plan_backend(BacktestConfig.model_validate(dynamic))


def _schedule_rows() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "signal_time": [START],
            "fill_time": [START + timedelta(minutes=1)],
            "symbol": ["BTCUSDT"],
            "target_weight": [1.0],
            "source_signal_id": ["signal-a31"],
            "factor_version": ["factor-a31"],
            "universe_version": ["universe-a31"],
            "portfolio_version": ["portfolio-a31"],
        }
    )


def test_target_schedule_identity_is_stable_and_rebalances_are_explicit() -> None:
    kwargs = {
        "rebalance_times": (START + timedelta(minutes=1), START + timedelta(minutes=2)),
        "parent_manifest_sha256": "a" * 64,
    }
    first = build_target_schedule(_schedule_rows(), **kwargs)
    second = build_target_schedule(_schedule_rows().lazy(), **kwargs)
    assert first.schedule_id == second.schedule_id
    assert len(first.rebalance_times) == 2  # second timestamp is an explicit all-flat snapshot

    duplicated = pl.concat([_schedule_rows(), _schedule_rows()])
    with pytest.raises(TargetScheduleError, match="duplicate"):
        build_target_schedule(duplicated, **kwargs)
    with pytest.raises(TargetScheduleError, match="explicit full rebalance"):
        build_target_schedule(
            _schedule_rows(),
            rebalance_times=(START + timedelta(minutes=2),),
            parent_manifest_sha256="a" * 64,
        )
