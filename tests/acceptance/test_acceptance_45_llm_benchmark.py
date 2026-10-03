from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import exp, sin

import pytest
import polars as pl

from bfbt.experiments.llm_benchmark import (
    CHATGPT,
    KIMI,
    FundingPoint,
    HourPoint,
    MarketView,
    chatgpt_decision,
    claude_decision,
    grok_decision,
    kimi_decision,
    StrategyDecision,
)
from bfbt.experiments.llm_benchmark_event import (
    INSTRUCTION_SCHEMA,
    Account,
    PendingOrder,
    _frame,
    _minute_risk,
)


UTC = timezone.utc
NOW = datetime(2025, 9, 1, tzinfo=UTC)


def _hours(symbol_index: int, count: int = 697) -> tuple[HourPoint, ...]:
    return tuple(
        HourPoint(
            available_at=NOW - timedelta(hours=count - 1 - index),
            close=(100.0 + symbol_index) * (1.0001 ** index),
            minute_count=60,
            quote_volume=1_000_000.0 - symbol_index,
        )
        for index in range(count)
    )


def _view(count: int = 40) -> MarketView:
    symbols = tuple(f"S{index:03d}" for index in range(count))
    return MarketView(
        now=NOW,
        eligible=symbols,
        hourly={symbol: _hours(index) for index, symbol in enumerate(symbols)},
        funding={
            symbol: tuple(
                FundingPoint(NOW - timedelta(hours=8 * offset), 0.0001 * (index - 20))
                for offset in (3, 2, 1)
            )
            for index, symbol in enumerate(symbols)
        },
        quote_volume_24h={symbol: 1_000_000.0 - index for index, symbol in enumerate(symbols)},
    )


def _varied_view(count: int = 100) -> MarketView:
    symbols = tuple(f"V{index:03d}" for index in range(count))
    hourly = {}
    funding = {}
    for index, symbol in enumerate(symbols):
        points = []
        for step in range(200):
            value = 100.0 * exp((index - count / 2) * 0.000002 * step + 0.003 * sin(step / 7 + index / 3))
            points.append(HourPoint(
                available_at=NOW - timedelta(hours=199 - step),
                close=value, minute_count=60,
                quote_volume=2_000_000.0 - index,
            ))
        hourly[symbol] = tuple(points)
        funding[symbol] = tuple(
            FundingPoint(NOW - timedelta(hours=8 * offset), (index - 50) * 0.000001)
            for offset in range(24, 0, -1)
        )
    return MarketView(
        now=NOW, eligible=symbols, hourly=hourly, funding=funding,
        quote_volume_24h={symbol: 2_000_000.0 - index for index, symbol in enumerate(symbols)},
    )


def test_chatgpt_contract_produces_exact_balanced_15_by_15_targets() -> None:
    decision = chatgpt_decision(_view(), {})
    positives = [value for value in decision.target_weights.values() if value > 0]
    negatives = [value for value in decision.target_weights.values() if value < 0]
    assert len(positives) == len(negatives) == 15
    assert sum(positives) == pytest.approx(0.5)
    assert sum(negatives) == pytest.approx(-0.5)
    assert sum(abs(value) for value in decision.target_weights.values()) == pytest.approx(1.0)


def test_chatgpt_contract_fails_closed_below_40_scores() -> None:
    assert chatgpt_decision(_view(39), {}).target_weights == {}


def test_kimi_contract_uses_four_hour_components_and_drawdown_scale() -> None:
    decision = kimi_decision(_view(), {}, risk_scale=0.5)
    positives = [value for value in decision.target_weights.values() if value > 0]
    negatives = [value for value in decision.target_weights.values() if value < 0]
    assert len(positives) == len(negatives) == 10
    assert sum(positives) == pytest.approx(0.25)
    assert sum(negatives) == pytest.approx(-0.25)
    assert max(abs(value) for value in decision.target_weights.values()) <= 0.10


def test_future_funding_does_not_change_kimi_decision() -> None:
    base = _view()
    changed = MarketView(
        now=base.now,
        eligible=base.eligible,
        hourly=base.hourly,
        funding={
            **base.funding,
            "S000": base.funding["S000"] + (FundingPoint(NOW + timedelta(minutes=1), 99.0),),
        },
        quote_volume_24h=base.quote_volume_24h,
    )
    assert kimi_decision(base, {}, risk_scale=1.0) == kimi_decision(
        changed, {}, risk_scale=1.0
    )


def test_event_account_sizes_chatgpt_basket_against_post_cost_equity() -> None:
    account = Account(strategy=CHATGPT, run_id="evt-test")
    symbols = [f"L{index:02d}" for index in range(15)] + [f"S{index:02d}" for index in range(15)]
    account.marks = {symbol: 100.0 for symbol in symbols}
    decision = StrategyDecision(
        strategy_id=CHATGPT.strategy_id,
        signal_time=NOW,
        scores=(),
        target_weights={
            symbol: (1.0 / 30.0 if symbol.startswith("L") else -1.0 / 30.0)
            for symbol in symbols
        },
        metadata={},
    )
    account.queue_targets(decision, next_marks=account.marks, exact_post_cost=True)
    account.fill_due(NOW + timedelta(minutes=1), account.marks)
    expected = 100_000.0 / 1.0007
    assert account.equity() == pytest.approx(expected)
    assert sum(abs(value) for value in account.notionals().values()) == pytest.approx(expected)
    assert len(account.trades) == 30


def test_event_account_never_fills_scheduled_decision_at_same_open() -> None:
    account = Account(strategy=KIMI, run_id="evt-test")
    account.marks["BTCUSDT"] = 100.0
    account.queue(PendingOrder(
        symbol="BTCUSDT", target_quantity=10.0, target_weight=0.01,
        decision_time=NOW, eligible_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=61), reason="scheduled_rebalance",
        priority=30,
    ))
    account.fill_due(NOW, {"BTCUSDT": 100.0})
    assert account.positions == {}
    account.fill_due(NOW + timedelta(minutes=1), {"BTCUSDT": 101.0})
    assert account.positions["BTCUSDT"] == 10.0


def test_event_funding_sign_is_symmetric() -> None:
    long = Account(strategy=KIMI, run_id="long", cash=99_000.0, positions={"X": 10.0}, marks={"X": 100.0})
    short = Account(strategy=KIMI, run_id="short", cash=101_000.0, positions={"X": -10.0}, marks={"X": 100.0})
    long.apply_funding(NOW, [("X", 0.0001, NOW)])
    short.apply_funding(NOW, [("X", 0.0001, NOW)])
    assert long.minute_funding == pytest.approx(-0.1)
    assert short.minute_funding == pytest.approx(0.1)


def test_grok_benchmark_override_keeps_fixed_half_gross_per_side() -> None:
    decision = grok_decision(_varied_view(), {})
    positives = [value for value in decision.target_weights.values() if value > 0]
    negatives = [value for value in decision.target_weights.values() if value < 0]
    assert len(positives) == len(negatives) == 15
    assert sum(positives) == pytest.approx(0.5)
    assert sum(negatives) == pytest.approx(-0.5)


def test_claude_waterfill_obeys_side_budget_and_five_percent_cap() -> None:
    decision = claude_decision(_varied_view(), {}, {})
    positives = [value for value in decision.target_weights.values() if value > 0]
    negatives = [value for value in decision.target_weights.values() if value < 0]
    assert len(positives) == len(negatives) == 20
    assert sum(positives) == pytest.approx(0.5)
    assert sum(negatives) == pytest.approx(-0.5)
    assert max(abs(value) for value in decision.target_weights.values()) <= 0.05


def test_pending_risk_event_survives_flush_and_checkpoint(tmp_path) -> None:
    workspace = tmp_path / "parts"
    account = Account(
        strategy=KIMI, run_id="evt-risk-test", workspace=workspace,
        cash=99_000.0, positions={"X": 10.0}, marks={"X": 100.0},
        average_entry={"X": 100.0},
    )
    event_id = account.risk_event(
        NOW, symbol="X", event_type="drawdown", trigger_level=-0.10,
        observed=-0.11, action="flatten", reason="test_breaker",
    )
    account.queue(PendingOrder(
        symbol="X", target_quantity=0.0, target_weight=0.0,
        decision_time=NOW, eligible_at=NOW + timedelta(minutes=1),
        expires_at=None, reason="test_breaker", priority=0, event_id=event_id,
    ))
    account.flush()
    checkpoint = account.checkpoint()
    assert checkpoint["pending_risks"][0]["event_id"] == event_id
    assert not list((workspace / "risk_events").glob("*.parquet"))

    restored = Account(strategy=KIMI, run_id="evt-risk-test", workspace=workspace)
    restored.restore(checkpoint)
    restored.fill_due(NOW + timedelta(minutes=1), {"X": 101.0})
    restored.flush()
    rows = pl.read_parquet(workspace / "risk_events" / "part-0002.parquet")
    assert rows.item(0, "event_id") == event_id
    assert rows.item(0, "fill_time") == NOW + timedelta(minutes=1)


def test_kimi_drawdown_repair_retains_submitted_sixty_minute_timeout() -> None:
    account = Account(
        strategy=KIMI, run_id="evt-kimi-risk", cash=79_000.0,
        positions={"X": 10.0}, marks={"X": 100.0},
        target_weights={"X": 0.01}, peak_equity=100_000.0,
    )
    _minute_risk(account, NOW, {}, None)
    order = account.pending["X"]
    assert order.reason == "kimi_drawdown_on"
    assert order.expires_at == NOW + timedelta(minutes=60)


def test_sparse_audit_string_after_inference_window_uses_explicit_schema() -> None:
    rows = [
        {
            "instruction_id": f"ins-{index}", "decision_time": NOW,
            "rank_source_time": NOW, "symbol": "X", "side": "LONG",
            "instruction_mode": "target_quantity",
            "requested_delta_notional": 1.0,
            "constrained_delta_notional": 1.0,
            "requested_target_weight": 0.01,
            "source_event_id": None, "reason_code": "scheduled_rebalance",
            "priority": 30, "run_id": "evt-schema-test",
        }
        for index in range(100)
    ]
    rows.append({
        **rows[-1], "instruction_id": "ins-risk",
        "source_event_id": "risk-63dfe302c4a9471613dbe265",
        "reason_code": "risk_exit", "priority": 2,
    })
    frame = _frame(rows, INSTRUCTION_SCHEMA)
    assert frame.schema["source_event_id"] == pl.String
    assert frame.item(100, "source_event_id") == "risk-63dfe302c4a9471613dbe265"
