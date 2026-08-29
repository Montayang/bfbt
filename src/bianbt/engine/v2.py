"""A18 correctness-first V2 event loop over a bounded DatasetSnapshot slice."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping

import polars as pl

from bianbt.config.backtest import (
    BacktestConfig,
    BacktestPerformanceV2Config,
    PortfolioV2Config,
    RiskV2Config,
)
from bianbt.config.durations import duration_seconds
from bianbt.data.hashing import content_sha256
from bianbt.data.resample import resample_bars
from bianbt.data.v2_contracts import EventPriority, V2ReasonCode
from bianbt.engine.costs import fee_rate, slippage_rate
from bianbt.engine.events import (
    ARBITRATION_TRACE_SCHEMA,
    INSTRUCTION_ARTIFACT_SCHEMA,
    INTENT_SCHEMA,
    EventArbitrator,
    link_risk_event_fills,
)
from bianbt.engine.execution import adverse_fill_price, fill_time
from bianbt.engine.funding import funding_cashflow
from bianbt.engine.vectorized import (
    COST_SCHEMA,
    POSITION_SCHEMA,
    RETURN_SCHEMA,
    TARGET_SCHEMA,
    TRADE_SCHEMA,
    BacktestError,
    BacktestResult,
)
from bianbt.portfolio.instructions import (
    IncrementalPositionEngine,
    PositionCheckpoint,
)
from bianbt.risk.state_machine import (
    RISK_EVENT_SCHEMA,
    RiskCheckpoint,
    RiskStateMachine,
)

V2_ENGINE_CODE_VERSION = "a30-engine-v2-carry-forward-valuation"
UTC_MS = pl.Datetime("ms", "UTC")
EPSILON = 1e-10

POSITION_V2_SCHEMA = {
    **POSITION_SCHEMA,
    "average_entry_price": pl.Float64,
    "used_margin": pl.Float64,
    "available_margin": pl.Float64,
    "consecutive_adds": pl.Int32,
}
LINKED_TRADE_SCHEMA = {
    **TRADE_SCHEMA,
    "instruction_id": pl.String,
    "source_event_id": pl.String,
    "priority": pl.Int16,
    "instruction_reason_code": pl.String,
}


@dataclass(frozen=True)
class V2BacktestResult:
    """Economic result plus the V2 audit facts published atomically."""

    result: BacktestResult
    position_instructions: pl.DataFrame | pl.LazyFrame
    risk_events: pl.DataFrame | pl.LazyFrame
    linked_trades: pl.DataFrame | pl.LazyFrame
    arbitration_trace: pl.DataFrame | pl.LazyFrame
    audit_result_hash: str
    checkpoint: "V2ExecutionCheckpoint"


@dataclass(frozen=True)
class V2ExecutionCheckpoint:
    """Minimum economic state required to continue at the next time chunk."""

    run_id: str
    position: PositionCheckpoint
    risk: RiskCheckpoint
    sequence: int
    previous_equity: float
    peak_equity: float
    warnings: tuple[str, ...]
    max_position_state_rows_observed: int
    max_risk_state_rows_observed: int
    max_pending_risk_intents_observed: int
    input_trade_bar_rows: int
    input_risk_bar_rows: int
    last_close_marks: dict[str, float]


def _frame(
    rows: list[dict[str, object]], schema: Mapping[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=dict(schema))
    return pl.DataFrame(rows).select(list(schema)).cast(dict(schema))


def _concat(
    frames: list[pl.DataFrame], schema: Mapping[str, pl.DataType]
) -> pl.DataFrame:
    useful = [frame for frame in frames if frame.height]
    if not useful:
        return pl.DataFrame(schema=dict(schema))
    return pl.concat(useful, how="vertical").select(list(schema)).cast(dict(schema))


def _collect_bars(
    frame: pl.LazyFrame,
    *,
    dataset_version: str,
    interval: str,
    label: str,
    max_rows: int,
) -> pl.DataFrame:
    required = {
        "open_time",
        "close_time",
        "symbol",
        "interval",
        "open",
        "high",
        "low",
        "close",
        "is_complete",
        "dataset_version",
    }
    missing = required - set(frame.collect_schema().names())
    if missing:
        raise BacktestError(f"{label} is missing columns: {sorted(missing)}")
    query = frame.filter(
        (pl.col("dataset_version") == dataset_version)
        & (pl.col("interval") == interval)
    ).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    )
    row_count = int(query.select(pl.len()).collect(engine="streaming").item())
    if row_count > max_rows:
        raise BacktestError(
            f"{label} rows {row_count} exceed max_input_rows={max_rows}"
        )
    result = query.sort(["open_time", "symbol"]).collect(engine="streaming")
    if not result.height:
        raise BacktestError(f"{label} has no rows for requested version/interval")
    duplicate = (
        result.group_by(["open_time", "symbol"])
        .len()
        .filter(pl.col("len") > 1)
    )
    if duplicate.height:
        raise BacktestError(f"{label} contains duplicate open_time/symbol rows")
    if result.filter(~pl.col("is_complete")).height:
        raise BacktestError(f"{label} contains incomplete bars")
    return result


def _group_bars(
    frame: pl.DataFrame, time_column: str
) -> dict[datetime, dict[str, dict[str, object]]]:
    output: dict[datetime, dict[str, dict[str, object]]] = {}
    for row in frame.to_dicts():
        timestamp = row[time_column]
        assert isinstance(timestamp, datetime)
        output.setdefault(timestamp, {})[str(row["symbol"])] = row
    return output


def _risk_input(
    source: pl.LazyFrame,
    *,
    dataset_name: str,
    source_interval: str,
    risk_interval: str,
    dataset_version: str,
    max_rows: int,
) -> pl.DataFrame:
    if duration_seconds(risk_interval) == duration_seconds(source_interval):
        return _collect_bars(
            source,
            dataset_version=dataset_version,
            interval=source_interval,
            label=f"{dataset_name} risk bars",
            max_rows=max_rows,
        )
    result = resample_bars(
        source,
        dataset_name=dataset_name,  # type: ignore[arg-type]
        source_interval=source_interval,
        target_interval=risk_interval,
        source_dataset_version=dataset_version,
    )
    return _collect_bars(
        result.frame,
        dataset_version=result.dataset_version,
        interval=risk_interval,
        label=f"resampled {dataset_name} risk bars",
        max_rows=max_rows,
    )


def _intent_frame(
    instructions: pl.DataFrame,
    *,
    fill_at: datetime,
) -> pl.DataFrame:
    frame = instructions
    for name, expression in (
        ("fill_time", pl.lit(fill_at, dtype=UTC_MS)),
        ("rank_source_time", pl.lit(None, dtype=UTC_MS)),
        ("requested_target_weight", pl.lit(None, dtype=pl.Float64)),
        ("source_event_id", pl.lit(None, dtype=pl.String)),
    ):
        if name not in frame.columns:
            frame = frame.with_columns(expression.alias(name))
    return frame.select(list(INTENT_SCHEMA)).cast(INTENT_SCHEMA)


def _blocked_intent(
    row: dict[str, object],
    *,
    run_id: str,
    fill_at: datetime,
    sizing_mode: str,
    requested_delta: float,
    reason: str,
) -> dict[str, object]:
    decision_time = row["signal_time"]
    assert isinstance(decision_time, datetime)
    symbol = str(row["symbol"])
    identity = {
        "engine": V2_ENGINE_CODE_VERSION,
        "run_id": run_id,
        "decision_time": decision_time.isoformat(),
        "fill_time": fill_at.isoformat(),
        "symbol": symbol,
        "reason": reason,
    }
    return {
        "instruction_id": f"instruction-{content_sha256(identity)[:24]}",
        "decision_time": decision_time,
        "fill_time": fill_at,
        "rank_source_time": row.get("rank_source_time"),
        "symbol": symbol,
        "side": str(row["side"]),
        "instruction_mode": sizing_mode,
        "requested_delta_notional": requested_delta,
        "constrained_delta_notional": requested_delta,
        "requested_target_weight": row.get("target_weight"),
        "source_event_id": None,
        "reason_code": reason,
        "priority": int(EventPriority.SCHEDULED_STRATEGY),
        "run_id": run_id,
    }


def _requested_delta(
    row: dict[str, object],
    *,
    config: BacktestConfig,
    equity: float,
    old_notional: float,
    rolling_margin: float | None = None,
) -> float:
    portfolio = config.portfolio
    risk = config.risk
    assert isinstance(portfolio, PortfolioV2Config)
    assert isinstance(risk, RiskV2Config)
    sizing = portfolio.sizing
    side = str(row["side"])
    if side == "FLAT":
        return -old_notional
    sign = 1.0 if side == "LONG" else -1.0
    if sizing.mode == "target_weight":
        return float(row["target_weight"]) * equity - old_notional
    if sizing.mode == "fixed_margin":
        assert sizing.margin_amount is not None
        return sign * sizing.margin_amount * risk.leverage
    if sizing.mode == "rolling_margin":
        if rolling_margin is None:
            raise BacktestError("rolling sizing state is unavailable")
        return sign * rolling_margin * risk.leverage
    if sizing.mode == "fixed_notional":
        assert sizing.notional_amount is not None
        return sign * sizing.notional_amount
    if sizing.mode == "equity_margin_fraction":
        assert sizing.fraction is not None
        return sign * sizing.fraction * equity * risk.leverage
    if sizing.mode == "equity_fraction":
        assert sizing.fraction is not None
        return sign * sizing.fraction * equity
    assert sizing.fraction is not None
    if abs(old_notional) > EPSILON:
        return sign * sizing.fraction * abs(old_notional)
    return sign * float(sizing.bootstrap_notional_amount or 0.0)


def _json_rows(frame: pl.DataFrame) -> list[dict[str, object]]:
    return [
        {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in row.items()
        }
        for row in frame.to_dicts()
    ]


def v2_engine_run_id(
    *,
    config: BacktestConfig,
    portfolio_version: str,
    bars_dataset_version: str,
    mark_dataset_version: str | None,
    funding_dataset_version: str | None,
    base_interval: str,
) -> str:
    identity = {
        "engine": V2_ENGINE_CODE_VERSION,
        "config": config.model_dump(mode="json"),
        "portfolio_version": portfolio_version,
        "bars_dataset_version": bars_dataset_version,
        "mark_dataset_version": mark_dataset_version,
        "funding_dataset_version": funding_dataset_version,
        "base_interval": base_interval,
    }
    return f"a20-{content_sha256(identity)[:24]}"


def _run_v2_slice(
    strategy: pl.LazyFrame,
    targets: pl.LazyFrame,
    rankings: pl.LazyFrame,
    trade_bars: pl.LazyFrame,
    mark_bars: pl.LazyFrame | None,
    funding: pl.LazyFrame | None,
    *,
    config: BacktestConfig,
    base_interval: str,
    portfolio_version: str,
    bars_dataset_version: str,
    mark_dataset_version: str | None,
    funding_dataset_version: str | None,
    checkpoint: V2ExecutionCheckpoint | None,
    finalize: bool,
    execution_mode: str,
) -> V2BacktestResult:
    """Execute one ordered V2 market slice from an optional checkpoint."""

    if config.config_version != "v2":
        raise BacktestError("run_v2_backtest requires config_version=v2")
    portfolio = config.portfolio
    risk_config = config.risk
    performance = config.performance
    capital = config.capital
    assert isinstance(portfolio, PortfolioV2Config)
    assert isinstance(risk_config, RiskV2Config)
    assert isinstance(performance, BacktestPerformanceV2Config)
    assert capital is not None
    trade = _collect_bars(
        trade_bars,
        dataset_version=bars_dataset_version,
        interval=base_interval,
        label="trade bars",
        max_rows=performance.max_input_rows_per_chunk,
    )
    if config.valuation.price == "mark_close":
        if mark_bars is None or not mark_dataset_version:
            raise BacktestError("mark_close valuation requires versioned mark_bars")
        valuation = _collect_bars(
            mark_bars,
            dataset_version=mark_dataset_version,
            interval=base_interval,
            label="mark bars",
            max_rows=performance.max_input_rows_per_chunk,
        )
    else:
        valuation = trade
    risk_source = mark_bars if risk_config.trigger_price == "mark" else trade_bars
    risk_version = (
        mark_dataset_version
        if risk_config.trigger_price == "mark"
        else bars_dataset_version
    )
    if risk_source is None or not risk_version:
        raise BacktestError("risk trigger source is unavailable")
    risk_bars = _risk_input(
        risk_source,
        dataset_name="mark_bars" if risk_config.trigger_price == "mark" else "bars",
        source_interval=base_interval,
        risk_interval=risk_config.evaluation_interval,
        dataset_version=risk_version,
        max_rows=performance.max_input_rows_per_chunk,
    )
    strategy_frame = (
        strategy.with_columns(pl.col("signal_time").cast(UTC_MS))
        .sort(["signal_time", "symbol"])
        .collect(engine="streaming")
    )
    if not strategy_frame.height and execution_mode == "in_memory_v2":
        raise BacktestError("V2 strategy has no selected rows")
    target_frame = (
        targets.with_columns(
            pl.col("signal_time").cast(UTC_MS),
            pl.lit("").alias("run_id"),
        )
        .select(list(TARGET_SCHEMA))
        .collect(engine="streaming")
    )
    run_id = v2_engine_run_id(
        config=config,
        portfolio_version=portfolio_version,
        bars_dataset_version=bars_dataset_version,
        mark_dataset_version=mark_dataset_version,
        funding_dataset_version=funding_dataset_version,
        base_interval=base_interval,
    )
    if checkpoint is not None and checkpoint.run_id != run_id:
        raise BacktestError("V2 checkpoint run_id does not match this execution")
    target_frame = target_frame.with_columns(pl.lit(run_id).alias("run_id"))

    trade_by_open = _group_bars(trade, "open_time")
    valuation_by_open = _group_bars(valuation, "open_time")
    risk_by_close = _group_bars(risk_bars, "close_time")
    strategy_by_fill: dict[datetime, pl.DataFrame] = {}
    for group in strategy_frame.partition_by("signal_time", maintain_order=True):
        signal_at = group.item(0, "signal_time")
        filled_at = fill_time(
            signal_at,
            signal_delay_bars=config.schedule.signal_delay_bars,
            base_interval=base_interval,
        )
        strategy_by_fill[filled_at] = group
    unknown_strategy_fills = set(strategy_by_fill) - set(trade_by_open)
    if unknown_strategy_fills:
        raise BacktestError(
            "V2 strategy fill times have no trade bar: "
            f"{sorted(unknown_strategy_fills)!r}"
        )

    funding_by_time: dict[datetime, dict[str, dict[str, object]]] = {}
    if config.execution.funding.enabled:
        if funding is None or not funding_dataset_version:
            if config.execution.funding.missing_policy == "error":
                raise BacktestError("enabled funding requires a versioned dataset")
        else:
            funding_frame = (
                funding.filter(pl.col("dataset_version") == funding_dataset_version)
                .with_columns(pl.col("funding_time").cast(UTC_MS))
                .sort(["funding_time", "symbol"])
                .collect(engine="streaming")
            )
            for row in funding_frame.to_dicts():
                funding_by_time.setdefault(row["funding_time"], {})[
                    str(row["symbol"])
                ] = row

    position_engine = IncrementalPositionEngine(
        sizing=portfolio.sizing,
        constraints=portfolio.constraints,
        holding=portfolio.holding,
        capital=capital,
        leverage=risk_config.leverage,
        fee=config.execution.fee,
        slippage=config.execution.slippage,
        run_id=run_id,
        max_position_state_rows=performance.max_position_state_rows,
        max_pending_instructions=performance.max_pending_instructions,
        checkpoint=checkpoint.position if checkpoint is not None else None,
    )
    risk_engine = RiskStateMachine(
        config=risk_config,
        initial_equity=capital.initial_equity,
        run_id=run_id,
        max_risk_state_rows=performance.max_risk_state_rows,
        max_pending_risk_intents=performance.max_pending_risk_intents,
        checkpoint=checkpoint.risk if checkpoint is not None else None,
    )
    arbitrator = EventArbitrator(
        run_id=run_id,
        max_pending_intents=performance.max_pending_instructions
        + performance.max_pending_risk_intents,
    )

    trade_rows: list[dict[str, object]] = []
    position_rows: list[dict[str, object]] = []
    cost_rows: list[dict[str, object]] = []
    return_rows: list[dict[str, object]] = []
    instruction_batches: list[pl.DataFrame] = []
    accepted_batches: list[pl.DataFrame] = []
    trace_batches: list[pl.DataFrame] = []
    risk_event_batches: list[pl.DataFrame] = []
    same_bar_accounted_ids: set[str] = set()
    warnings = set(checkpoint.warnings) if checkpoint is not None else set()
    sequence = checkpoint.sequence if checkpoint is not None else 0
    previous_equity = (
        checkpoint.previous_equity
        if checkpoint is not None
        else capital.initial_equity
    )
    peak = (
        checkpoint.peak_equity
        if checkpoint is not None
        else capital.initial_equity
    )
    max_position_rows = (
        checkpoint.max_position_state_rows_observed
        if checkpoint is not None
        else 0
    )
    max_risk_rows = (
        checkpoint.max_risk_state_rows_observed
        if checkpoint is not None
        else 0
    )
    max_pending = (
        checkpoint.max_pending_risk_intents_observed
        if checkpoint is not None
        else 0
    )
    last_close_marks = (
        dict(checkpoint.last_close_marks) if checkpoint is not None else {}
    )

    for opened_at in sorted(trade_by_open):
        market = trade_by_open[opened_at]
        values = valuation_by_open.get(opened_at, {})
        close_times = {row["close_time"] for row in market.values()}
        if len(close_times) != 1:
            raise BacktestError("one trade snapshot must share close_time")
        closed_at = next(iter(close_times))
        actual_symbols = set(market)
        open_marks = {symbol: float(row["open"]) for symbol, row in market.items()}
        close_marks = {
            symbol: float(row["close"]) for symbol, row in values.items()
        }
        held_symbols = set(position_engine.checkpoint().positions["symbol"])
        missing_open = held_symbols - set(open_marks)
        missing_close = held_symbols - set(close_marks)
        missing_marks = missing_open | missing_close
        unavailable = missing_marks - set(last_close_marks)
        if unavailable:
            raise BacktestError(
                "held symbols have neither a current bar nor a prior close: "
                f"{sorted(unavailable)}"
            )
        pending_risk_symbols = set(risk_engine.checkpoint().pending_intents["symbol"])
        missing_pending_risk = pending_risk_symbols - actual_symbols
        if missing_pending_risk:
            raise BacktestError(
                "due risk exits have no real opening bar: "
                f"{sorted(missing_pending_risk)}"
            )
        for symbol in sorted(missing_open):
            open_marks[symbol] = last_close_marks[symbol]
        for symbol in sorted(missing_close):
            close_marks[symbol] = last_close_marks[symbol]
        for symbol in sorted(missing_marks):
            warnings.add(
                f"valuation_carry_forward:{opened_at.isoformat()}:{symbol}"
            )

        positions_at_open = position_engine.position_snapshot(opened_at, open_marks)
        risk_fill = risk_engine.drain_due(
            open_time=opened_at,
            opening_prices=open_marks,
            positions=positions_at_open,
        )
        risk_intents = _intent_frame(risk_fill.instructions, fill_at=opened_at)
        risk_symbols = set(risk_intents["symbol"])
        if risk_intents.height:
            position_engine.apply_external_deltas(
                risk_intents,
                timestamp=opened_at,
                marks=open_marks,
            )

        scheduled = strategy_by_fill.get(opened_at)
        blocked_rows: list[dict[str, object]] = []
        strategy_intents = pl.DataFrame(schema=INTENT_SCHEMA)
        if scheduled is not None:
            candidate = scheduled
            if portfolio.sizing.mode == "target_weight":
                held = set(position_engine.checkpoint().positions["symbol"])
                chosen = set(candidate["symbol"])
                missing_targets = sorted(held - chosen)
                if missing_targets:
                    synthetic = pl.DataFrame(
                        {
                            "signal_time": [candidate.item(0, "signal_time")]
                            * len(missing_targets),
                            "rank_source_time": [None] * len(missing_targets),
                            "symbol": missing_targets,
                            "side": ["LONG"] * len(missing_targets),
                            "target_weight": [0.0] * len(missing_targets),
                        }
                    ).with_columns(
                        pl.col("signal_time").cast(UTC_MS),
                        pl.col("rank_source_time").cast(UTC_MS),
                    )
                    candidate = pl.concat(
                        [candidate, synthetic], how="diagonal_relaxed"
                    ).sort("symbol")
            allowed_rows: list[dict[str, object]] = []
            account_at_open = position_engine.account_snapshot(opened_at, open_marks)
            old_notionals = {
                str(row["symbol"]): float(row["quantity"])
                * open_marks[str(row["symbol"])]
                for row in position_engine.checkpoint().positions.to_dicts()
            }
            for row in candidate.to_dicts():
                symbol = str(row["symbol"])
                closing_target = (
                    portfolio.sizing.mode == "target_weight"
                    and abs(float(row.get("target_weight") or 0.0)) <= EPSILON
                )
                decision = risk_engine.reentry_decision(
                    symbol, scheduled_rebalance=True
                )
                missing_execution_bar = symbol not in actual_symbols
                blocked = missing_execution_bar or symbol in risk_symbols or (
                    not decision.allowed and not closing_target
                )
                if blocked:
                    reason = (
                        V2ReasonCode.END_OF_DATA_UNFILLED.value
                        if missing_execution_bar
                        else (
                            V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value
                            if symbol in risk_symbols
                            else V2ReasonCode.COOLDOWN_ACTIVE.value
                        )
                    )
                    blocked_row = _blocked_intent(
                        row,
                        run_id=run_id,
                        fill_at=opened_at,
                        sizing_mode=portfolio.sizing.mode,
                        requested_delta=_requested_delta(
                            row,
                            config=config,
                            equity=account_at_open.equity,
                            old_notional=old_notionals.get(symbol, 0.0),
                            rolling_margin=position_engine.checkpoint().rolling_margin,
                        ),
                        reason=reason,
                    )
                    if missing_execution_bar:
                        blocked_row["constrained_delta_notional"] = 0.0
                    blocked_rows.append(blocked_row)
                else:
                    allowed_rows.append(row)
            if allowed_rows:
                allowed = pl.DataFrame(allowed_rows)
                decision_at = allowed.item(0, "signal_time")
                batch = position_engine.process(
                    allowed,
                    decision_time=decision_at,
                    marks=open_marks,
                )
                strategy_intents = _intent_frame(
                    batch.instructions, fill_at=opened_at
                )
        blocked_intents = _frame(blocked_rows, INTENT_SCHEMA)
        intents = _concat(
            [risk_intents, strategy_intents, blocked_intents], INTENT_SCHEMA
        )
        if intents.height:
            arbitration = arbitrator.arbitrate(
                intents,
                cooldown_symbols={
                    str(row["symbol"])
                    for row in blocked_rows
                    if row["reason_code"] == V2ReasonCode.COOLDOWN_ACTIVE.value
                },
            )
            instruction_batches.append(arbitration.instructions)
            accepted_batches.append(arbitration.accepted_intents)
            trace_batches.append(arbitration.trace)
            end_positions = position_engine.position_snapshot(opened_at, open_marks)
            post_notional = {
                str(row["symbol"]): float(row["signed_notional"])
                for row in end_positions.to_dicts()
            }
            for intent in arbitration.accepted_intents.to_dicts():
                delta = float(intent["constrained_delta_notional"] or 0.0)
                if abs(delta) <= EPSILON:
                    continue
                symbol = str(intent["symbol"])
                start_equity = max(previous_equity, EPSILON)
                post = post_notional.get(symbol, 0.0)
                sequence += 1
                trade_rows.append(
                    {
                        "signal_time": intent["decision_time"],
                        "fill_time": opened_at,
                        "symbol": symbol,
                        "sequence": sequence,
                        "side": "BUY" if delta > 0 else "SELL",
                        "old_weight": (post - delta) / start_equity,
                        "target_weight": post / start_equity,
                        "filled_weight": post / start_equity,
                        "turnover": abs(delta) / start_equity,
                        "reference_price": open_marks[symbol],
                        "fill_price": adverse_fill_price(
                            open_marks[symbol],
                            delta / open_marks[symbol],
                            slippage_rate(config.execution.slippage),
                        ),
                        "notional": abs(delta),
                        "status": "FILLED",
                        "constraint_flags": str(intent["reason_code"]),
                        "run_id": run_id,
                        "instruction_id": intent["instruction_id"],
                        "source_event_id": intent["source_event_id"],
                        "priority": intent["priority"],
                        "instruction_reason_code": intent["reason_code"],
                    }
                )

        event_costs: dict[str, dict[str, float]] = {}
        for row in trade_rows:
            if row["fill_time"] != opened_at:
                continue
            symbol = str(row["symbol"])
            notional = float(row["notional"])
            event_costs[symbol] = {
                "fee": notional * fee_rate(config.execution.fee),
                "slippage": notional * slippage_rate(config.execution.slippage),
                "funding": 0.0,
            }
        funding_total = 0.0
        for event_at in sorted(
            value for value in funding_by_time if opened_at < value <= closed_at
        ):
            records = funding_by_time[event_at]
            for position in position_engine.checkpoint().positions.to_dicts():
                symbol = str(position["symbol"])
                record = records.get(symbol)
                if record is None:
                    policy = config.execution.funding.missing_policy
                    if policy == "error":
                        raise BacktestError(
                            f"missing funding for {symbol} at {event_at.isoformat()}"
                        )
                    warnings.add(f"funding_{policy}:{event_at.isoformat()}:{symbol}")
                    continue
                price_value = record.get("mark_price")
                price = (
                    float(price_value)
                    if price_value is not None and isfinite(float(price_value))
                    else close_marks[symbol]
                )
                cashflow = funding_cashflow(
                    float(position["quantity"]),
                    price,
                    float(record["funding_rate"]),
                )
                position_engine.apply_cashflow(cashflow, symbol=symbol)
                funding_total += cashflow
                event_costs.setdefault(
                    symbol, {"fee": 0.0, "slippage": 0.0, "funding": 0.0}
                )["funding"] += cashflow

        account = position_engine.account_snapshot(closed_at, close_marks)
        if not isfinite(account.equity) or account.equity <= 0:
            raise BacktestError("V2 equity became non-positive or non-finite")
        positions = position_engine.position_snapshot(closed_at, close_marks)
        risk_snapshot = risk_by_close.get(closed_at)
        if risk_snapshot is not None:
            risk_rows = list(risk_snapshot.values())
            risk_symbols_available = set(risk_snapshot)
            missing_risk = set(positions["symbol"]) - risk_symbols_available
            if missing_risk:
                template = dict(risk_rows[0])
                for symbol in sorted(missing_risk):
                    stale = close_marks[symbol]
                    synthetic = dict(template)
                    synthetic.update(
                        {
                            "symbol": symbol,
                            "open": stale,
                            "high": stale,
                            "low": stale,
                            "close": stale,
                            "is_complete": True,
                        }
                    )
                    risk_rows.append(synthetic)
            risk_frame = pl.DataFrame(risk_rows)
            risk_evaluation = risk_engine.evaluate(
                risk_frame,
                positions,
                equity=account.equity,
                price_source=risk_config.trigger_price,
            )
            risk_event_batches.append(risk_evaluation.events)
            max_risk_rows = max(
                max_risk_rows, risk_evaluation.checkpoint.risk_state_rows
            )
            max_pending = max(
                max_pending, risk_evaluation.checkpoint.pending_intent_rows
            )
            if risk_config.fill_model == "same_bar_trigger":
                same_bar_fill = risk_engine.drain_due(
                    open_time=closed_at,
                    opening_prices=close_marks,
                    positions=positions,
                )
                same_bar_intents = _intent_frame(
                    same_bar_fill.instructions, fill_at=closed_at
                )
                if same_bar_intents.height:
                    reference_prices = {
                        str(row["symbol"]): float(row["reference_price"])
                        for row in same_bar_fill.instructions.to_dicts()
                    }
                    arbitration = arbitrator.arbitrate(same_bar_intents)
                    execution_marks = dict(close_marks)
                    execution_marks.update(reference_prices)
                    position_engine.apply_external_deltas(
                        arbitration.accepted_intents,
                        timestamp=closed_at,
                        marks=execution_marks,
                    )
                    instruction_batches.append(arbitration.instructions)
                    accepted_batches.append(arbitration.accepted_intents)
                    trace_batches.append(arbitration.trace)
                    end_positions = position_engine.position_snapshot(
                        closed_at, close_marks
                    )
                    post_notional = {
                        str(row["symbol"]): float(row["signed_notional"])
                        for row in end_positions.to_dicts()
                    }
                    for intent in arbitration.accepted_intents.to_dicts():
                        delta = float(
                            intent["constrained_delta_notional"] or 0.0
                        )
                        if abs(delta) <= EPSILON:
                            continue
                        symbol = str(intent["symbol"])
                        reference_price = reference_prices[symbol]
                        post = post_notional.get(symbol, 0.0)
                        sequence += 1
                        trade_rows.append(
                            {
                                "signal_time": intent["decision_time"],
                                "fill_time": closed_at,
                                "symbol": symbol,
                                "sequence": sequence,
                                "side": "BUY" if delta > 0 else "SELL",
                                "old_weight": (
                                    post - delta
                                ) / max(previous_equity, EPSILON),
                                "target_weight": post
                                / max(previous_equity, EPSILON),
                                "filled_weight": post
                                / max(previous_equity, EPSILON),
                                "turnover": abs(delta)
                                / max(previous_equity, EPSILON),
                                "reference_price": reference_price,
                                "fill_price": adverse_fill_price(
                                    reference_price,
                                    delta / reference_price,
                                    slippage_rate(config.execution.slippage),
                                ),
                                "notional": abs(delta),
                                "status": "FILLED",
                                "constraint_flags": str(
                                    intent["reason_code"]
                                ),
                                "run_id": run_id,
                                "instruction_id": intent["instruction_id"],
                                "source_event_id": intent["source_event_id"],
                                "priority": intent["priority"],
                                "instruction_reason_code": intent[
                                    "reason_code"
                                ],
                            }
                        )
                        same_bar_accounted_ids.add(str(intent["instruction_id"]))
                        costs = event_costs.setdefault(
                            symbol,
                            {
                                "fee": 0.0,
                                "slippage": 0.0,
                                "funding": 0.0,
                            },
                        )
                        costs["fee"] += abs(delta) * fee_rate(
                            config.execution.fee
                        )
                        costs["slippage"] += abs(delta) * slippage_rate(
                            config.execution.slippage
                        )
                    account = position_engine.account_snapshot(
                        closed_at, close_marks
                    )
                    positions = position_engine.position_snapshot(
                        closed_at, close_marks
                    )
        peak = max(peak, account.equity)
        fee_total = sum(value["fee"] for value in event_costs.values())
        slip_total = sum(value["slippage"] for value in event_costs.values())
        net_return = account.equity / previous_equity - 1.0
        gross_return = (
            net_return
            + fee_total / previous_equity
            + slip_total / previous_equity
            - funding_total / previous_equity
        )
        for row in positions.to_dicts():
            symbol = str(row["symbol"])
            signed_notional = float(row["signed_notional"])
            position_rows.append(
                {
                    "timestamp": closed_at,
                    "symbol": symbol,
                    "quantity": row["quantity"],
                    "signed_notional": signed_notional,
                    "target_weight": 0.0,
                    "actual_weight": signed_notional / account.equity,
                    "mark_price": close_marks[symbol],
                    "unrealized_pnl": row["unrealized_pnl"],
                    "run_id": run_id,
                    "average_entry_price": row["average_entry_price"],
                    "used_margin": row["used_margin"],
                    "available_margin": account.available_margin,
                    "consecutive_adds": row["consecutive_adds"],
                }
            )
        for symbol, values_for_symbol in sorted(event_costs.items()):
            cost_rows.append(
                {
                    "timestamp": closed_at,
                    "symbol": symbol,
                    "fee_cost": values_for_symbol["fee"] / previous_equity,
                    "slippage_cost": values_for_symbol["slippage"] / previous_equity,
                    "funding_cashflow": values_for_symbol["funding"] / previous_equity,
                    "total_cost": (
                        values_for_symbol["fee"]
                        + values_for_symbol["slippage"]
                        - values_for_symbol["funding"]
                    )
                    / previous_equity,
                    "run_id": run_id,
                }
            )
        return_rows.append(
            {
                "timestamp": closed_at,
                "gross_price_return": gross_return,
                "fee_cost": fee_total / previous_equity,
                "slippage_cost": slip_total / previous_equity,
                "funding_return": funding_total / previous_equity,
                "net_return": net_return,
                "equity": account.equity,
                "drawdown": account.equity / peak - 1.0,
                "gross_exposure": account.gross_notional / account.equity,
                "net_exposure": account.net_notional / account.equity,
                "turnover": sum(
                    float(row["notional"])
                    for row in trade_rows
                    if (
                        row["fill_time"] == opened_at
                        and row["instruction_id"] not in same_bar_accounted_ids
                    )
                    or (
                        row["fill_time"] == closed_at
                        and row["instruction_id"] in same_bar_accounted_ids
                    )
                ) / previous_equity,
                "run_id": run_id,
            }
        )
        max_position_rows = max(
            max_position_rows, position_engine.checkpoint().position_state_rows
        )
        previous_equity = account.equity
        last_close_marks.update(
            {
                symbol: float(row["close"])
                for symbol, row in values.items()
            }
        )
        current_symbols = set(position_engine.checkpoint().positions["symbol"])
        last_close_marks = {
            symbol: last_close_marks[symbol]
            for symbol in sorted(current_symbols)
            if symbol in last_close_marks
        }

    instructions = _concat(
        instruction_batches, INSTRUCTION_ARTIFACT_SCHEMA
    )
    accepted = _concat(accepted_batches, INTENT_SCHEMA)
    traces = _concat(trace_batches, ARBITRATION_TRACE_SCHEMA)
    trades = _frame(trade_rows, TRADE_SCHEMA)
    linked = arbitrator.link_trades(trades, accepted).trades
    risk_checkpoint = risk_engine.checkpoint()
    if finalize and risk_checkpoint.evaluation_count == 0:
        raise BacktestError("no complete V2 risk-clock snapshot was evaluated")
    risk_events = _concat(risk_event_batches, RISK_EVENT_SCHEMA)
    terminal_risk = (
        risk_engine.finish()
        if finalize
        else pl.DataFrame(schema=RISK_EVENT_SCHEMA)
    )
    if terminal_risk.height:
        terminal_ids = terminal_risk["event_id"].unique().to_list()
        risk_events = risk_events.with_columns(
            pl.when(pl.col("event_id").is_in(terminal_ids))
            .then(pl.lit(None, dtype=UTC_MS))
            .otherwise(pl.col("fill_time"))
            .alias("fill_time"),
            pl.when(pl.col("event_id").is_in(terminal_ids))
            .then(pl.lit(V2ReasonCode.END_OF_DATA_UNFILLED.value))
            .otherwise(pl.col("reason_code"))
            .alias("reason_code"),
        )
        warnings.add(f"risk_end_of_data_unfilled:{terminal_risk.height}")
    if finalize and risk_events.height:
        risk_events = link_risk_event_fills(
            risk_events,
            linked,
            run_id=run_id,
            position_instructions=instructions,
        )

    positions_result = _frame(position_rows, POSITION_V2_SCHEMA)
    costs_result = _frame(cost_rows, COST_SCHEMA)
    returns_result = _frame(return_rows, RETURN_SCHEMA)
    result_payload = {
        "run_id": run_id,
        "targets": _json_rows(target_frame),
        "trades": _json_rows(linked),
        "positions": _json_rows(positions_result),
        "costs": _json_rows(costs_result),
        "returns": _json_rows(returns_result),
        "warnings": sorted(warnings),
    }
    result_hash = content_sha256(result_payload)
    audit_payload = {
        "instructions": _json_rows(instructions),
        "risk_events": _json_rows(risk_events),
        "trades": _json_rows(linked),
        "trace": _json_rows(traces),
    }
    cumulative_trade_rows = (
        (checkpoint.input_trade_bar_rows if checkpoint is not None else 0)
        + trade.height
    )
    cumulative_risk_rows = (
        (checkpoint.input_risk_bar_rows if checkpoint is not None else 0)
        + risk_bars.height
    )
    diagnostics = {
        "max_position_state_rows_observed": max_position_rows,
        "max_risk_state_rows_observed": max_risk_rows,
        "max_pending_risk_intents_observed": max_pending,
        "input_trade_bar_rows": cumulative_trade_rows,
        "input_risk_bar_rows": cumulative_risk_rows,
    }
    execution_checkpoint = V2ExecutionCheckpoint(
        run_id=run_id,
        position=position_engine.checkpoint(),
        risk=risk_checkpoint,
        sequence=sequence,
        previous_equity=previous_equity,
        peak_equity=peak,
        warnings=tuple(sorted(warnings)),
        max_position_state_rows_observed=max_position_rows,
        max_risk_state_rows_observed=max_risk_rows,
        max_pending_risk_intents_observed=max_pending,
        input_trade_bar_rows=cumulative_trade_rows,
        input_risk_bar_rows=cumulative_risk_rows,
        last_close_marks=last_close_marks,
    )
    result = BacktestResult(
        run_id=run_id,
        result_hash=result_hash,
        targets=target_frame.lazy(),
        trades=linked.lazy(),
        positions=positions_result.lazy(),
        costs=costs_result.lazy(),
        returns=returns_result.lazy(),
        warnings=tuple(sorted(warnings)),
        diagnostics=diagnostics,
        execution_mode=execution_mode,
    )
    return V2BacktestResult(
        result=result,
        position_instructions=instructions,
        risk_events=risk_events,
        linked_trades=linked,
        arbitration_trace=traces,
        audit_result_hash=content_sha256(audit_payload),
        checkpoint=execution_checkpoint,
    )


def run_v2_backtest(
    strategy: pl.LazyFrame,
    targets: pl.LazyFrame,
    rankings: pl.LazyFrame,
    trade_bars: pl.LazyFrame,
    mark_bars: pl.LazyFrame | None,
    funding: pl.LazyFrame | None,
    *,
    config: BacktestConfig,
    base_interval: str,
    portfolio_version: str,
    bars_dataset_version: str,
    mark_dataset_version: str | None,
    funding_dataset_version: str | None,
) -> V2BacktestResult:
    """Execute one complete in-memory V2 run with A18-compatible semantics."""

    if config.performance.mode != "in_memory":
        raise BacktestError(
            "run_v2_backtest requires performance.mode=in_memory; "
            "use run_v2_backtest_chunk for chunked slices"
        )
    return _run_v2_slice(
        strategy,
        targets,
        rankings,
        trade_bars,
        mark_bars,
        funding,
        config=config,
        base_interval=base_interval,
        portfolio_version=portfolio_version,
        bars_dataset_version=bars_dataset_version,
        mark_dataset_version=mark_dataset_version,
        funding_dataset_version=funding_dataset_version,
        checkpoint=None,
        finalize=True,
        execution_mode="in_memory_v2",
    )


def run_v2_backtest_chunk(
    strategy: pl.LazyFrame,
    targets: pl.LazyFrame,
    rankings: pl.LazyFrame,
    trade_bars: pl.LazyFrame,
    mark_bars: pl.LazyFrame | None,
    funding: pl.LazyFrame | None,
    *,
    config: BacktestConfig,
    base_interval: str,
    portfolio_version: str,
    bars_dataset_version: str,
    mark_dataset_version: str | None,
    funding_dataset_version: str | None,
    checkpoint: V2ExecutionCheckpoint | None = None,
    finalize: bool = False,
) -> V2BacktestResult:
    """Execute one bounded V2 slice and return state for the next slice."""

    if config.performance.mode != "chunked":
        raise BacktestError(
            "run_v2_backtest_chunk requires performance.mode=chunked"
        )
    if config.performance.max_process_rss_mib is None:
        raise BacktestError(
            "V2 chunked execution requires max_process_rss_mib"
        )
    return _run_v2_slice(
        strategy,
        targets,
        rankings,
        trade_bars,
        mark_bars,
        funding,
        config=config,
        base_interval=base_interval,
        portfolio_version=portfolio_version,
        bars_dataset_version=bars_dataset_version,
        mark_dataset_version=mark_dataset_version,
        funding_dataset_version=funding_dataset_version,
        checkpoint=checkpoint,
        finalize=finalize,
        execution_mode="chunked_v2",
    )
