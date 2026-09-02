"""User-run acceptance suite for A16 bounded risk state and next-open fills."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from bfbt.config.backtest import RiskV2Config
from bfbt.risk import (
    RiskEvaluationError,
    RiskStateBudgetExceeded,
    RiskStateMachine,
)

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    BACKTEST_ROOT
    / "tests"
    / "fixtures"
    / "risk"
    / "acceptance_16"
    / "risk_bars.csv"
)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _risk(**updates: object) -> RiskV2Config:
    payload: dict[str, object] = {
        "leverage": 2.0,
        "enforce_liquidation": False,
        "evaluation_interval": "1m",
        "trigger_price": "trade",
        "fill_model": "next_bar_open",
        "intrabar_conflict": "worst_case",
        "symbol_exits": {},
        "portfolio_exits": {},
        "cooldown_bars": 0,
        "reentry_policy": "after_cooldown",
        "max_triggers_per_symbol": None,
    }
    payload.update(updates)
    return RiskV2Config.model_validate(payload)


def _engine(config: RiskV2Config, **updates: object) -> RiskStateMachine:
    payload: dict[str, object] = {
        "config": config,
        "initial_equity": 1_000.0,
        "run_id": "acceptance-a16",
    }
    payload.update(updates)
    return RiskStateMachine(**payload)


def _positions(*rows: tuple[str, float, float]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "quantity": pl.Float64,
                "average_entry_price": pl.Float64,
            }
        )
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "quantity": [row[1] for row in rows],
            "average_entry_price": [row[2] for row in rows],
        }
    )


def _bars(
    minute: int,
    *rows: tuple[str, float, float, float, float],
) -> pl.DataFrame:
    opened = T0 + timedelta(minutes=minute)
    closed = opened + timedelta(minutes=1)
    return pl.DataFrame(
        {
            "open_time": [opened for _ in rows],
            "close_time": [closed for _ in rows],
            "symbol": [row[0] for row in rows],
            "open": [row[1] for row in rows],
            "high": [row[2] for row in rows],
            "low": [row[3] for row in rows],
            "close": [row[4] for row in rows],
            "is_complete": [True for _ in rows],
        }
    )


def _rule(
    kind: str,
    distance: float,
    *,
    action: str = "close",
    reduce_fraction: float | None = None,
) -> dict[str, object]:
    return {
        kind: {
            "enabled": True,
            "distance": distance,
            "action": action,
            "reduce_fraction": reduce_fraction,
        }
    }


def test_long_stop_is_detected_on_low_and_filled_at_actual_gap_open() -> None:
    fixture = pl.read_csv(FIXTURE, try_parse_dates=True).with_columns(
        pl.col("open_time").cast(pl.Datetime("ms", "UTC")),
        pl.col("close_time").cast(pl.Datetime("ms", "UTC")),
    )
    first_bar = fixture.filter(pl.col("open_time") == T0)
    engine = _engine(_risk(symbol_exits=_rule("stop_loss", 0.05)))
    evaluated = engine.evaluate(
        first_bar,
        _positions(("AUSDT", 2.0, 100.0)),
        equity=1_000.0,
        price_source="trade",
    )

    event = evaluated.events.row(0, named=True)
    assert event["reason_code"] == "STOP_LOSS_TRIGGERED"
    assert event["trigger_level"] == pytest.approx(95.0)
    assert event["observed_price"] == pytest.approx(94.0)
    assert event["fill_time"] == T0 + timedelta(minutes=1)

    filled = engine.drain_due(
        open_time=T0 + timedelta(minutes=1),
        opening_prices={"AUSDT": 90.0},
        positions=_positions(("AUSDT", 2.0, 100.0)),
    )
    instruction = filled.instructions.row(0, named=True)
    assert instruction["reference_price"] == 90.0
    assert instruction["requested_delta_notional"] == pytest.approx(-180.0)
    assert instruction["source_event_id"] == event["event_id"]


@pytest.mark.parametrize(
    ("bar", "rule", "reason", "level"),
    [
        (("AUSDT", 100.0, 106.0, 99.0, 103.0), "stop_loss", "STOP_LOSS_TRIGGERED", 105.0),
        (("AUSDT", 100.0, 101.0, 89.0, 95.0), "take_profit", "TAKE_PROFIT_TRIGGERED", 90.0),
    ],
)
def test_short_stop_and_take_profit_use_opposite_ohlc_extremes(
    bar: tuple[str, float, float, float, float],
    rule: str,
    reason: str,
    level: float,
) -> None:
    engine = _engine(_risk(symbol_exits=_rule(rule, 0.05 if rule == "stop_loss" else 0.1)))
    result = engine.evaluate(
        _bars(0, bar),
        _positions(("AUSDT", -1.0, 100.0)),
        equity=1_000.0,
        price_source="trade",
    )
    assert result.events["reason_code"].item() == reason
    assert result.events["trigger_level"].item() == pytest.approx(level)


def test_intrabar_double_trigger_uses_worst_case_stop() -> None:
    engine = _engine(
        _risk(
            symbol_exits={
                **_rule("stop_loss", 0.05),
                **_rule("take_profit", 0.1),
            }
        )
    )
    result = engine.evaluate(
        _bars(0, ("AUSDT", 100.0, 111.0, 94.0, 101.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=1_000.0,
        price_source="trade",
    )
    assert result.events["event_type"].item() == "stop_loss"
    assert result.events["conflict_policy"].item() == "worst_case"


def test_intrabar_double_trigger_error_does_not_queue_an_intent() -> None:
    engine = _engine(
        _risk(
            intrabar_conflict="error",
            symbol_exits={
                **_rule("stop_loss", 0.05),
                **_rule("take_profit", 0.1),
            },
        )
    )
    with pytest.raises(RiskEvaluationError, match="multiple risk levels"):
        engine.evaluate(
            _bars(0, ("AUSDT", 100.0, 111.0, 94.0, 101.0)),
            _positions(("AUSDT", 1.0, 100.0)),
            equity=1_000.0,
            price_source="trade",
        )
    assert engine.checkpoint().pending_intent_rows == 0


def test_trailing_stop_uses_only_previous_bar_extreme_and_new_average_entry() -> None:
    engine = _engine(_risk(symbol_exits=_rule("trailing_stop", 0.05)))
    first = engine.evaluate(
        _bars(0, ("AUSDT", 100.0, 110.0, 99.0, 108.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=1_008.0,
        price_source="trade",
    )
    assert first.events.is_empty()
    assert first.state["favorable_extreme"].item() == 110.0
    assert first.state["trailing_stop_level"].item() == pytest.approx(104.5)

    second = engine.evaluate(
        _bars(1, ("AUSDT", 108.0, 109.0, 104.0, 105.0)),
        _positions(("AUSDT", 2.0, 102.0)),
        equity=1_006.0,
        price_source="trade",
    )
    assert second.events["reason_code"].item() == "TRAILING_STOP_TRIGGERED"
    assert second.events["entry_price"].item() == 102.0
    assert second.events["trigger_level"].item() == pytest.approx(104.5)


def test_activated_trailing_uses_confirmed_prior_bar_extreme() -> None:
    rules = _rule("trailing_stop", 0.028)
    rules["trailing_stop"]["activation_distance"] = 0.058
    engine = _engine(_risk(symbol_exits=rules))

    activation_bar = engine.evaluate(
        _bars(0, ("AUSDT", 100.0, 106.0, 100.0, 105.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=1_005.0,
        price_source="trade",
    )
    assert activation_bar.events.is_empty()
    assert activation_bar.state["trailing_stop_level"].item() == pytest.approx(103.032)

    retrace_bar = engine.evaluate(
        _bars(1, ("AUSDT", 105.0, 105.5, 102.9, 103.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=1_003.0,
        price_source="trade",
    )
    assert retrace_bar.events["reason_code"].item() == "TRAILING_STOP_TRIGGERED"
    assert retrace_bar.events["trigger_level"].item() == pytest.approx(103.032)


def test_reduce_fraction_creates_a_partial_next_open_instruction() -> None:
    engine = _engine(
        _risk(
            symbol_exits=_rule(
                "take_profit", 0.1, action="reduce_fraction", reduce_fraction=0.25
            )
        )
    )
    engine.evaluate(
        _bars(0, ("AUSDT", 100.0, 111.0, 99.0, 110.0)),
        _positions(("AUSDT", 4.0, 100.0)),
        equity=1_040.0,
        price_source="trade",
    )
    fill = engine.drain_due(
        open_time=T0 + timedelta(minutes=1),
        opening_prices={"AUSDT": 120.0},
        positions=_positions(("AUSDT", 4.0, 100.0)),
    )
    assert fill.instructions["requested_delta_notional"].item() == -120.0


def test_portfolio_stop_take_and_drawdown_have_higher_priority() -> None:
    scenarios = [
        ({"stop_loss": 0.1}, 899.0, "PORTFOLIO_STOP_LOSS_TRIGGERED"),
        ({"take_profit": 0.1}, 1_101.0, "PORTFOLIO_TAKE_PROFIT_TRIGGERED"),
    ]
    for exits, equity, reason in scenarios:
        engine = _engine(_risk(portfolio_exits=exits))
        result = engine.evaluate(
            _bars(0, ("AUSDT", 100.0, 101.0, 99.0, 100.0)),
            _positions(("AUSDT", 1.0, 100.0)),
            equity=equity,
            price_source="trade",
        )
        assert result.events["reason_code"].item() == reason
        assert result.pending_intents["priority"].item() == 100

    drawdown = _engine(_risk(portfolio_exits={"max_drawdown": 0.2}))
    drawdown.evaluate(
        _bars(0, ("AUSDT", 100.0, 101.0, 99.0, 100.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=1_200.0,
        price_source="trade",
    )
    result = drawdown.evaluate(
        _bars(1, ("AUSDT", 100.0, 101.0, 99.0, 100.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=950.0,
        price_source="trade",
    )
    assert result.events["reason_code"].item() == "PORTFOLIO_MAX_DRAWDOWN_TRIGGERED"
    assert result.events["trigger_level"].item() == 960.0


def test_cooldown_and_next_scheduled_rebalance_block_same_time_reentry() -> None:
    after_cooldown = _engine(
        _risk(
            symbol_exits=_rule("stop_loss", 0.05),
            cooldown_bars=2,
            reentry_policy="after_cooldown",
        )
    )
    after_cooldown.evaluate(
        _bars(0, ("AUSDT", 100.0, 101.0, 94.0, 95.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=995.0,
        price_source="trade",
    )
    assert not after_cooldown.reentry_decision(
        "AUSDT", scheduled_rebalance=True
    ).allowed
    after_cooldown.drain_due(
        open_time=T0 + timedelta(minutes=1),
        opening_prices={"AUSDT": 95.0},
        positions=_positions(("AUSDT", 1.0, 100.0)),
    )
    after_cooldown.evaluate(
        _bars(1, ("AUSDT", 95.0, 96.0, 94.0, 95.0)),
        _positions(),
        equity=995.0,
        price_source="trade",
    )
    assert not after_cooldown.reentry_decision(
        "AUSDT", scheduled_rebalance=False
    ).allowed
    after_cooldown.evaluate(
        _bars(2, ("AUSDT", 95.0, 96.0, 94.0, 95.0)),
        _positions(),
        equity=995.0,
        price_source="trade",
    )
    assert after_cooldown.reentry_decision(
        "AUSDT", scheduled_rebalance=False
    ).allowed

    scheduled = _engine(
        _risk(
            symbol_exits=_rule("stop_loss", 0.05),
            reentry_policy="next_scheduled_rebalance",
        )
    )
    scheduled.evaluate(
        _bars(0, ("AUSDT", 100.0, 101.0, 94.0, 95.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=995.0,
        price_source="trade",
    )
    assert not scheduled.reentry_decision("AUSDT", scheduled_rebalance=True).allowed


def test_checkpoint_restore_matches_continuous_trailing_state() -> None:
    config = _risk(symbol_exits=_rule("trailing_stop", 0.05))
    first_bar = _bars(0, ("AUSDT", 100.0, 110.0, 99.0, 108.0))
    second_bar = _bars(1, ("AUSDT", 108.0, 109.0, 104.0, 105.0))
    positions = _positions(("AUSDT", 1.0, 100.0))

    continuous = _engine(config)
    continuous.evaluate(first_bar, positions, equity=1_008.0, price_source="trade")
    expected = continuous.evaluate(
        second_bar, positions, equity=1_005.0, price_source="trade"
    )

    split = _engine(config)
    checkpoint = split.evaluate(
        first_bar, positions, equity=1_008.0, price_source="trade"
    ).checkpoint
    restored = _engine(config, checkpoint=checkpoint)
    actual = restored.evaluate(
        second_bar, positions, equity=1_005.0, price_source="trade"
    )
    assert actual.events.equals(expected.events)
    assert actual.state.equals(expected.state)
    assert actual.pending_intents.equals(expected.pending_intents)


def test_risk_and_pending_state_budgets_fail_before_partial_queueing() -> None:
    positions = _positions(("AUSDT", 1.0, 100.0), ("BUSDT", -1.0, 50.0))
    bars = _bars(
        0,
        ("AUSDT", 100.0, 101.0, 99.0, 100.0),
        ("BUSDT", 50.0, 51.0, 49.0, 50.0),
    )
    state_limited = _engine(_risk(), max_risk_state_rows=1)
    with pytest.raises(RiskStateBudgetExceeded, match="risk state rows"):
        state_limited.evaluate(
            bars, positions, equity=1_000.0, price_source="trade"
        )
    assert state_limited.checkpoint().risk_state_rows == 0

    pending_limited = _engine(
        _risk(portfolio_exits={"stop_loss": 0.01}),
        max_pending_risk_intents=1,
    )
    with pytest.raises(RiskStateBudgetExceeded, match="pending risk intents"):
        pending_limited.evaluate(
            bars, positions, equity=900.0, price_source="trade"
        )
    assert pending_limited.checkpoint().pending_intent_rows == 0


def test_risk_clock_source_contiguity_and_end_of_data_are_explicit() -> None:
    engine = _engine(_risk(symbol_exits=_rule("stop_loss", 0.05)))
    with pytest.raises(RiskEvaluationError, match="price_source"):
        engine.evaluate(
            _bars(0, ("AUSDT", 100.0, 101.0, 99.0, 100.0)),
            _positions(("AUSDT", 1.0, 100.0)),
            equity=1_000.0,
            price_source="mark",
        )
    engine.evaluate(
        _bars(0, ("AUSDT", 100.0, 101.0, 94.0, 95.0)),
        _positions(("AUSDT", 1.0, 100.0)),
        equity=995.0,
        price_source="trade",
    )
    assert engine.finish()["reason_code"].item() == "END_OF_DATA_UNFILLED"
    with pytest.raises(RiskEvaluationError, match="drained"):
        engine.evaluate(
            _bars(1, ("AUSDT", 95.0, 96.0, 94.0, 95.0)),
            _positions(("AUSDT", 1.0, 100.0)),
            equity=995.0,
            price_source="trade",
        )
