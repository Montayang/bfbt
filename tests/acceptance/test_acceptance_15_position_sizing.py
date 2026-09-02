"""User-run acceptance suite for A15 incremental sizing and capital state."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from bfbt.config.backtest import (
    CapitalConfig,
    FeeConfig,
    PortfolioConstraintsV2Config,
    PositionSizingConfig,
    SlippageConfig,
)
from bfbt.portfolio.instructions import (
    IncrementalPositionEngine,
    PositionInstructionError,
    PositionStateBudgetExceeded,
)

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    BACKTEST_ROOT
    / "tests"
    / "fixtures"
    / "portfolio"
    / "acceptance_15"
    / "position_events.csv"
)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _selection(symbol: str, side: str) -> pl.DataFrame:
    return pl.DataFrame({"symbol": [symbol], "side": [side]})


def _empty_selection() -> pl.DataFrame:
    return pl.DataFrame(schema={"symbol": pl.String, "side": pl.String})


def _sizing(mode: str, **updates: object) -> PositionSizingConfig:
    payload: dict[str, object] = {
        "mode": mode,
        "reverse_policy": "net_delta",
    }
    payload.update(updates)
    return PositionSizingConfig.model_validate(payload)


def _engine(
    sizing: PositionSizingConfig,
    *,
    constraints: PortfolioConstraintsV2Config | None = None,
    initial_equity: float = 1_000.0,
    leverage: float = 2.0,
    reserved_cost_buffer: float = 0.0,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    quantity_steps: dict[str, float] | None = None,
    max_position_state_rows: int = 20_000,
    max_pending_instructions: int = 20_000,
    checkpoint=None,
) -> IncrementalPositionEngine:
    return IncrementalPositionEngine(
        sizing=sizing,
        constraints=constraints or PortfolioConstraintsV2Config(),
        capital=CapitalConfig(
            initial_equity=initial_equity,
            reserved_cost_buffer=reserved_cost_buffer,
        ),
        leverage=leverage,
        fee=FeeConfig(model="fixed_bps", taker_bps=fee_bps),
        slippage=SlippageConfig(model="fixed_bps", bps=slippage_bps),
        run_id="acceptance-a15",
        quantity_steps=quantity_steps,
        max_position_state_rows=max_position_state_rows,
        max_pending_instructions=max_pending_instructions,
        checkpoint=checkpoint,
    )


@pytest.mark.parametrize(
    ("sizing", "expected"),
    [
        (_sizing("fixed_margin", margin_amount=100.0), 200.0),
        (_sizing("fixed_notional", notional_amount=300.0), 300.0),
        (_sizing("equity_fraction", fraction=0.1), 100.0),
        (
            _sizing(
                "position_fraction",
                fraction=0.5,
                zero_position_policy="bootstrap_fixed_notional",
                bootstrap_notional_amount=250.0,
            ),
            250.0,
        ),
    ],
)
def test_four_incremental_sizing_bases_are_independent(
    sizing: PositionSizingConfig, expected: float
) -> None:
    batch = _engine(sizing).process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )

    row = batch.instructions.row(0, named=True)
    assert row["sizing_base_notional"] == pytest.approx(expected)
    assert row["requested_delta_notional"] == pytest.approx(expected)
    assert row["posttrade_symbol_notional"] == pytest.approx(expected)
    assert row["requested_target_weight"] is None


def _rolling() -> PositionSizingConfig:
    return _sizing(
        "rolling_margin",
        rolling_initial_margin=200.0,
        rolling_reset_margin=200.0,
        rolling_min_margin=80.0,
        rolling_max_margin=1_000.0,
    )


def test_rolling_margin_compounds_net_costs_and_funding_across_checkpoint() -> None:
    engine = _engine(
        _rolling(), leverage=5.0, initial_equity=2_000.0, fee_bps=4.0, slippage_bps=1.0
    )
    opened = engine.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    assert opened.instructions["sizing_base_notional"].item() == pytest.approx(1_000.0)
    engine.apply_cashflow(5.0, symbol="AUSDT")
    closed = engine.apply_external_deltas(
        pl.DataFrame({"symbol": ["AUSDT"], "constrained_delta_notional": [-1_100.0]}),
        timestamp=T0 + timedelta(minutes=1),
        marks={"AUSDT": 110.0},
    )
    assert closed.checkpoint.rolling_margin == pytest.approx(303.95)
    restored = _engine(
        _rolling(),
        leverage=5.0,
        initial_equity=2_000.0,
        fee_bps=4.0,
        slippage_bps=1.0,
        checkpoint=closed.checkpoint,
    )
    next_open = restored.process(
        _selection("BUSDT", "LONG"),
        decision_time=T0 + timedelta(minutes=2),
        marks={"BUSDT": 100.0},
    )
    assert next_open.instructions["sizing_base_notional"].item() == pytest.approx(1_519.75)


@pytest.mark.parametrize(
    ("exit_price", "expected", "resets"),
    [(88.0, 80.0, 0), (87.9, 200.0, 1), (180.0, 1_000.0, 0), (180.1, 200.0, 1)],
)
def test_rolling_margin_reset_bounds_are_strict(
    exit_price: float, expected: float, resets: int
) -> None:
    engine = _engine(_rolling(), leverage=5.0, initial_equity=2_000.0)
    engine.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    checkpoint = engine.apply_external_deltas(
        pl.DataFrame(
            {
                "symbol": ["AUSDT"],
                "constrained_delta_notional": [-10.0 * exit_price],
            }
        ),
        timestamp=T0 + timedelta(minutes=1),
        marks={"AUSDT": exit_price},
    ).checkpoint
    assert checkpoint.rolling_margin == pytest.approx(expected)
    assert checkpoint.rolling_reset_count == resets


def test_rolling_margin_uses_actual_margin_after_available_capital_scaling() -> None:
    engine = _engine(_rolling(), leverage=5.0, initial_equity=150.0)
    opened = engine.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    actual_margin = abs(opened.checkpoint.positions["quantity"].item() * 100.0) / 5.0
    assert actual_margin == pytest.approx(150.0)
    closed = engine.apply_external_deltas(
        pl.DataFrame(
            {
                "symbol": ["AUSDT"],
                "constrained_delta_notional": [
                    -opened.checkpoint.positions["quantity"].item() * 100.0
                ],
            }
        ),
        timestamp=T0 + timedelta(minutes=1),
        marks={"AUSDT": 100.0},
    )
    assert closed.checkpoint.rolling_margin == pytest.approx(150.0)


def test_repeated_same_side_signals_add_and_position_fraction_uses_old_position() -> None:
    engine = _engine(_sizing("fixed_margin", margin_amount=100.0))
    first = engine.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    second = engine.process(
        _selection("AUSDT", "LONG"),
        decision_time=T0 + timedelta(hours=1),
        marks={"AUSDT": 100.0},
    )

    assert first.positions["signed_notional"].item() == pytest.approx(200.0)
    assert second.positions["signed_notional"].item() == pytest.approx(400.0)
    assert second.positions["consecutive_adds"].item() == 2

    fraction = _engine(
        _sizing(
            "position_fraction",
            fraction=0.5,
            zero_position_policy="bootstrap_fixed_notional",
            bootstrap_notional_amount=200.0,
        )
    )
    fraction.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    added = fraction.process(
        _selection("AUSDT", "LONG"),
        decision_time=T0 + timedelta(hours=1),
        marks={"AUSDT": 100.0},
    )
    assert added.instructions["sizing_base_notional"].item() == pytest.approx(100.0)
    assert added.positions["signed_notional"].item() == pytest.approx(300.0)


@pytest.mark.parametrize(
    ("policy", "expected_delta", "expected_post"),
    [
        ("flatten_only", -200.0, 0.0),
        ("flatten_then_open", -300.0, -100.0),
        ("net_delta", -100.0, 100.0),
    ],
)
def test_reverse_policies_cross_zero_in_a_fixed_order(
    policy: str, expected_delta: float, expected_post: float
) -> None:
    engine = _engine(
        _sizing(
            "fixed_notional", notional_amount=100.0, reverse_policy=policy
        )
    )
    engine.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    engine.process(
        _selection("AUSDT", "LONG"),
        decision_time=T0 + timedelta(hours=1),
        marks={"AUSDT": 100.0},
    )
    reversed_batch = engine.process(
        _selection("AUSDT", "SHORT"),
        decision_time=T0 + timedelta(hours=2),
        marks={"AUSDT": 100.0},
    )

    row = reversed_batch.instructions.row(0, named=True)
    assert row["requested_delta_notional"] == pytest.approx(expected_delta)
    assert row["posttrade_symbol_notional"] == pytest.approx(expected_post)


@pytest.mark.parametrize(
    ("constraints", "reason", "expected"),
    [
        (
            PortfolioConstraintsV2Config(max_gross_exposure=0.3),
            "SCALED_MAX_GROSS_EXPOSURE",
            300.0,
        ),
        (
            PortfolioConstraintsV2Config(max_net_exposure=0.1),
            "SCALED_MAX_NET_EXPOSURE",
            100.0,
        ),
        (
            PortfolioConstraintsV2Config(max_symbol_weight=0.2),
            "SCALED_MAX_SYMBOL_WEIGHT",
            200.0,
        ),
        (
            PortfolioConstraintsV2Config(max_symbol_notional=250.0),
            "SCALED_MAX_SYMBOL_NOTIONAL",
            250.0,
        ),
        (
            PortfolioConstraintsV2Config(max_turnover=0.15),
            "SCALED_MAX_TURNOVER",
            150.0,
        ),
    ],
)
def test_each_explicit_exposure_constraint_scales_the_requested_opening(
    constraints: PortfolioConstraintsV2Config,
    reason: str,
    expected: float,
) -> None:
    batch = _engine(
        _sizing("fixed_notional", notional_amount=500.0),
        constraints=constraints,
    ).process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )

    row = batch.instructions.row(0, named=True)
    assert row["requested_delta_notional"] == pytest.approx(500.0)
    assert row["constrained_delta_notional"] == pytest.approx(expected, abs=1e-6)
    assert row["reason_code"] == reason


def test_available_margin_and_consecutive_adds_reject_excess_risk() -> None:
    margin = _engine(
        _sizing("fixed_notional", notional_amount=2_000.0),
        initial_equity=1_000.0,
        leverage=1.0,
        reserved_cost_buffer=100.0,
    ).process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    row = margin.instructions.row(0, named=True)
    assert row["constrained_delta_notional"] == pytest.approx(900.0, abs=1e-6)
    assert row["reason_code"] == "REJECTED_INSUFFICIENT_MARGIN"
    assert margin.account.available_margin == pytest.approx(0.0, abs=1e-6)

    capped = _engine(
        _sizing("fixed_notional", notional_amount=100.0),
        constraints=PortfolioConstraintsV2Config(max_consecutive_adds=2),
    )
    capped.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    capped.process(
        _selection("AUSDT", "LONG"),
        decision_time=T0 + timedelta(hours=1),
        marks={"AUSDT": 100.0},
    )
    rejected = capped.process(
        _selection("AUSDT", "LONG"),
        decision_time=T0 + timedelta(hours=2),
        marks={"AUSDT": 100.0},
    )
    assert rejected.instructions["constrained_delta_notional"].item() == 0.0
    assert rejected.instructions["reason_code"].item() == (
        "REJECTED_MAX_CONSECUTIVE_ADDS"
    )
    assert rejected.positions["signed_notional"].item() == pytest.approx(200.0)


def test_position_fraction_zero_policies_are_explicit() -> None:
    skipped = _engine(
        _sizing(
            "position_fraction",
            fraction=0.5,
            zero_position_policy="skip",
        )
    ).process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 100.0}
    )
    assert skipped.instructions["reason_code"].item() == "ZERO_POSITION_SKIPPED"
    assert skipped.positions.is_empty()

    failing = _engine(
        _sizing(
            "position_fraction",
            fraction=0.5,
            zero_position_policy="error",
        )
    )
    with pytest.raises(PositionInstructionError, match="zero position"):
        failing.process(
            _selection("AUSDT", "LONG"),
            decision_time=T0,
            marks={"AUSDT": 100.0},
        )


def test_external_full_close_removes_low_price_large_quantity_dust() -> None:
    engine = _engine(
        _sizing("fixed_notional", notional_amount=44_653.9511570206),
        constraints=PortfolioConstraintsV2Config(
            max_gross_exposure=5.0,
            max_net_exposure=5.0,
            max_symbol_weight=5.0,
            max_turnover=10.0,
        ),
        initial_equity=10_000.0,
        leverage=5.0,
    )
    opened = engine.process(
        _selection("USUSDT", "LONG"),
        decision_time=T0,
        marks={"USUSDT": 0.011319},
    )
    quantity = opened.checkpoint.positions.item(0, "quantity")
    exit_price = 0.01109262
    closed = engine.apply_external_deltas(
        pl.DataFrame(
            {
                "symbol": ["USUSDT"],
                "constrained_delta_notional": [-quantity * exit_price],
            }
        ),
        timestamp=T0 + timedelta(minutes=1),
        marks={"USUSDT": exit_price},
    )
    assert closed.positions.is_empty()
    assert closed.checkpoint.positions.is_empty()


def test_quantity_step_rounding_and_simple_cross_accounting_are_auditable() -> None:
    engine = _engine(
        _sizing("fixed_notional", notional_amount=500.0),
        fee_bps=10.0,
        slippage_bps=10.0,
        quantity_steps={"AUSDT": 0.1},
    )
    first = engine.process(
        _selection("AUSDT", "LONG"), decision_time=T0, marks={"AUSDT": 30.0}
    )
    row = first.instructions.row(0, named=True)
    assert row["constrained_delta_notional"] == pytest.approx(498.0)
    assert first.account.cash_balance == pytest.approx(999.004)
    assert first.account.equity == pytest.approx(
        first.account.cash_balance + first.account.unrealized_pnl
    )
    assert first.account.used_margin == pytest.approx(
        first.account.gross_notional / 2.0
    )
    assert first.account.available_margin == pytest.approx(
        first.account.equity - first.account.used_margin
    )

    marked = engine.process(
        _empty_selection(),
        decision_time=T0 + timedelta(hours=1),
        marks={"AUSDT": 33.0},
    )
    assert marked.account.unrealized_pnl == pytest.approx(49.8)
    assert marked.account.equity == pytest.approx(1_048.804)


def test_fixture_replay_checkpoint_restore_and_state_budgets_are_deterministic() -> None:
    events = pl.read_csv(FIXTURE, try_parse_dates=True).with_columns(
        pl.col("decision_time").cast(pl.Datetime("ms", "UTC"))
    )
    sizing = _sizing("fixed_notional", notional_amount=100.0)
    continuous = _engine(sizing)
    continuous_batches = []
    for event in events.head(2).iter_rows(named=True):
        continuous_batches.append(
            continuous.process(
                _selection(str(event["symbol"]), str(event["side"])),
                decision_time=event["decision_time"],
                marks={"AUSDT": float(event["price"])},
            )
        )

    split = _engine(sizing)
    first_event = events.row(0, named=True)
    first = split.process(
        _selection(str(first_event["symbol"]), str(first_event["side"])),
        decision_time=first_event["decision_time"],
        marks={"AUSDT": float(first_event["price"])},
    )
    restored = _engine(sizing, checkpoint=first.checkpoint)
    second_event = events.row(1, named=True)
    second = restored.process(
        _selection(str(second_event["symbol"]), str(second_event["side"])),
        decision_time=second_event["decision_time"],
        marks={"AUSDT": float(second_event["price"])},
    )
    assert second.instructions.equals(continuous_batches[-1].instructions)
    assert second.positions.equals(continuous_batches[-1].positions)
    assert second.account == continuous_batches[-1].account
    assert second.checkpoint.position_state_rows == 1
    assert second.account.pending_instruction_count == 0

    over_positions = _engine(sizing, max_position_state_rows=1)
    two_symbols = pl.DataFrame(
        {"symbol": ["AUSDT", "BUSDT"], "side": ["LONG", "SHORT"]}
    )
    with pytest.raises(PositionStateBudgetExceeded, match="potential position"):
        over_positions.process(
            two_symbols,
            decision_time=T0,
            marks={"AUSDT": 100.0, "BUSDT": 50.0},
        )
    assert over_positions.checkpoint().position_state_rows == 0

    over_pending = _engine(sizing, max_pending_instructions=1)
    with pytest.raises(PositionStateBudgetExceeded, match="pending instructions"):
        over_pending.process(
            two_symbols,
            decision_time=T0,
            marks={"AUSDT": 100.0, "BUSDT": 50.0},
        )
