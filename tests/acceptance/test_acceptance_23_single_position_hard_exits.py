"""Automated A23 acceptance for single-position replacement and hard exits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bianbt.config.backtest import (
    CapitalConfig,
    FeeConfig,
    HoldingPolicyConfig,
    PortfolioConstraintsV2Config,
    PositionSizingConfig,
    RiskV2Config,
    SlippageConfig,
)
from bianbt.portfolio.instructions import (
    IncrementalPositionEngine,
    PositionInstructionError,
)
from bianbt.risk import RiskStateMachine

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _selection(symbol: str) -> pl.DataFrame:
    return pl.DataFrame({"symbol": [symbol], "side": ["LONG"]})


def _position_engine(
    *,
    sizing: PositionSizingConfig,
    holding: HoldingPolicyConfig | None = None,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
) -> IncrementalPositionEngine:
    return IncrementalPositionEngine(
        sizing=sizing,
        constraints=PortfolioConstraintsV2Config(),
        holding=holding,
        capital=CapitalConfig(initial_equity=1_000.0),
        leverage=5.0,
        fee=FeeConfig(model="fixed_bps", taker_bps=fee_bps),
        slippage=SlippageConfig(model="fixed_bps", bps=slippage_bps),
        run_id="acceptance-a23",
    )


def _risk(*, stop: float = 0.02, take: float | None = None) -> RiskV2Config:
    exits: dict[str, object] = {
        "stop_loss": {
            "enabled": True,
            "distance": stop,
            "action": "close",
        }
    }
    if take is not None:
        exits["take_profit"] = {
            "enabled": True,
            "distance": take,
            "action": "close",
        }
    return RiskV2Config.model_validate(
        {
            "leverage": 5.0,
            "enforce_liquidation": False,
            "evaluation_interval": "1m",
            "trigger_price": "trade",
            "fill_model": "same_bar_trigger",
            "gap_policy": "worse_executable",
            "intrabar_conflict": "worst_case",
            "symbol_exits": exits,
            "portfolio_exits": {},
            "cooldown_bars": 0,
            "reentry_policy": "next_scheduled_rebalance",
            "max_triggers_per_symbol": None,
        }
    )


def _positions() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["AUSDT"],
            "quantity": [2.0],
            "average_entry_price": [100.0],
        }
    )


def _bar(
    *,
    opened: float,
    high: float,
    low: float,
    close: float,
) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "open_time": [T0],
            "close_time": [T0 + timedelta(minutes=1)],
            "symbol": ["AUSDT"],
            "open": [opened],
            "high": [high],
            "low": [low],
            "close": [close],
            "is_complete": [True],
        }
    )


def test_equity_margin_fraction_means_margin_share_times_leverage() -> None:
    engine = _position_engine(
        sizing=PositionSizingConfig(
            mode="equity_margin_fraction",
            fraction=1.0,
            reverse_policy="net_delta",
        )
    )
    result = engine.process(
        _selection("AUSDT"),
        decision_time=T0,
        marks={"AUSDT": 100.0},
    )
    assert result.instructions["sizing_base_notional"].item() == pytest.approx(
        5_000.0
    )
    assert result.positions["used_margin"].item() == pytest.approx(1_000.0)


def test_full_margin_is_conservatively_scaled_for_fees_and_slippage() -> None:
    engine = _position_engine(
        sizing=PositionSizingConfig(
            mode="equity_margin_fraction",
            fraction=1.0,
            reverse_policy="net_delta",
        ),
        fee_bps=4.0,
        slippage_bps=1.0,
    )
    result = engine.process(
        _selection("AUSDT"),
        decision_time=T0,
        marks={"AUSDT": 100.0},
    )
    notional = result.positions["signed_notional"].item()
    assert 4_900.0 < notional < 5_000.0
    assert result.account.used_margin + result.account.fee_cost + result.account.slippage_cost <= 1_000.0 + 1e-8


def test_single_position_ignores_same_symbol_and_replaces_another() -> None:
    engine = _position_engine(
        sizing=PositionSizingConfig(
            mode="fixed_notional",
            notional_amount=500.0,
            reverse_policy="net_delta",
        ),
        holding=HoldingPolicyConfig(
            mode="single_position_replace",
            existing_signal="ignore",
        ),
    )
    engine.process(_selection("AUSDT"), decision_time=T0, marks={"AUSDT": 100.0})
    ignored = engine.process(
        _selection("AUSDT"),
        decision_time=T0 + timedelta(minutes=1),
        marks={"AUSDT": 101.0},
    )
    assert ignored.instructions["reason_code"].item() == "ALREADY_HELD"
    assert ignored.instructions["constrained_delta_notional"].item() == 0.0

    replaced = engine.process(
        _selection("BUSDT"),
        decision_time=T0 + timedelta(minutes=2),
        marks={"AUSDT": 102.0, "BUSDT": 50.0},
    )
    rows = replaced.instructions.sort("symbol").to_dicts()
    assert [row["symbol"] for row in rows] == ["AUSDT", "BUSDT"]
    assert rows[0]["side"] == "FLAT"
    assert rows[0]["reason_code"] == "REPLACED_BY_SIGNAL"
    assert replaced.positions["symbol"].to_list() == ["BUSDT"]


def test_single_position_rejects_ambiguous_multi_signal_snapshot() -> None:
    engine = _position_engine(
        sizing=PositionSizingConfig(
            mode="fixed_notional",
            notional_amount=100.0,
            reverse_policy="net_delta",
        ),
        holding=HoldingPolicyConfig(mode="single_position_replace"),
    )
    with pytest.raises(PositionInstructionError, match="one signal"):
        engine.process(
            pl.DataFrame(
                {"symbol": ["AUSDT", "BUSDT"], "side": ["LONG", "LONG"]}
            ),
            decision_time=T0,
            marks={"AUSDT": 100.0, "BUSDT": 50.0},
        )


@pytest.mark.parametrize(
    ("bar", "expected_price", "expected_reason"),
    [
        (
            _bar(opened=100.0, high=101.0, low=97.0, close=99.0),
            98.0,
            "STOP_LOSS_TRIGGERED",
        ),
        (
            _bar(opened=95.0, high=96.0, low=94.0, close=95.0),
            95.0,
            "STOP_LOSS_TRIGGERED",
        ),
        (
            _bar(opened=100.0, high=105.0, low=97.0, close=102.0),
            98.0,
            "STOP_LOSS_TRIGGERED",
        ),
    ],
)
def test_same_bar_hard_exit_uses_conservative_price_and_worst_case(
    bar: pl.DataFrame,
    expected_price: float,
    expected_reason: str,
) -> None:
    engine = RiskStateMachine(
        config=_risk(take=0.036),
        initial_equity=1_000.0,
        run_id="acceptance-a23",
    )
    evaluated = engine.evaluate(
        bar,
        _positions(),
        equity=1_000.0,
        price_source="trade",
    )
    assert evaluated.events["reason_code"].item() == expected_reason
    filled = engine.drain_due(
        open_time=T0 + timedelta(minutes=1),
        opening_prices={"AUSDT": 999.0},
        positions=_positions(),
    )
    assert filled.instructions["reference_price"].item() == pytest.approx(
        expected_price
    )
    assert filled.instructions["requested_delta_notional"].item() == pytest.approx(
        -2.0 * expected_price
    )


def test_same_bar_reference_price_survives_checkpoint_restore() -> None:
    first = RiskStateMachine(
        config=_risk(),
        initial_equity=1_000.0,
        run_id="acceptance-a23",
    )
    evaluated = first.evaluate(
        _bar(opened=100.0, high=101.0, low=97.0, close=99.0),
        _positions(),
        equity=1_000.0,
        price_source="trade",
    )
    restored = RiskStateMachine(
        config=_risk(),
        initial_equity=1_000.0,
        run_id="acceptance-a23",
        checkpoint=evaluated.checkpoint,
    )
    filled = restored.drain_due(
        open_time=T0 + timedelta(minutes=1),
        opening_prices={"AUSDT": 999.0},
        positions=_positions(),
    )
    assert filled.instructions["reference_price"].item() == pytest.approx(98.0)


def test_v2_event_loop_executes_hard_stop_in_trigger_bar() -> None:
    from test_acceptance_18_v2_e2e import (
        PORTFOLIO_VERSION,
        START as E2E_START,
        VERSION,
        _bars,
        _config,
        _rankings,
        _strategy,
    )

    from bianbt.engine.v2 import run_v2_backtest

    strategy = _strategy().filter(
        (pl.col("symbol") == "BTCUSDT")
        & (pl.col("signal_time") == E2E_START)
    )
    targets = strategy.with_columns(
        pl.lit(0.0).alias("unconstrained_weight"),
        pl.lit(0.0).alias("target_weight"),
        pl.lit("INCREMENTAL_SIZING").alias("constraint_flags"),
        pl.lit(PORTFOLIO_VERSION).alias("portfolio_version"),
    )
    payload = _config().model_dump(mode="python")
    payload["risk"]["fill_model"] = "same_bar_trigger"
    config = type(_config()).model_validate(payload)
    result = run_v2_backtest(
        strategy,
        targets,
        _rankings(),
        _bars(),
        None,
        None,
        config=config,
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=VERSION,
        mark_dataset_version=None,
        funding_dataset_version=None,
    )

    risk_trade = result.linked_trades.filter(
        pl.col("source_event_id").is_not_null()
    )
    assert risk_trade.height == 1
    assert risk_trade["symbol"].item() == "BTCUSDT"
    assert risk_trade["fill_time"].item() == E2E_START + timedelta(minutes=2)
    assert risk_trade["reference_price"].item() == pytest.approx(99.0)
    later_positions = result.result.positions.collect().filter(
        (pl.col("symbol") == "BTCUSDT")
        & (pl.col("timestamp") >= E2E_START + timedelta(minutes=2))
    )
    assert later_positions.is_empty()
    assert result.checkpoint.risk.pending_intents.is_empty()
