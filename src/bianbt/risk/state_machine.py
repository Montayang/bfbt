"""Bounded V2 stop-loss, take-profit, trailing, and portfolio risk state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping

import polars as pl

from bianbt.config.backtest import RiskV2Config, SymbolExitRuleConfig
from bianbt.config.durations import duration_seconds
from bianbt.data.hashing import content_sha256
from bianbt.data.v2_contracts import EventPriority, V2ReasonCode

RISK_ENGINE_VERSION = "a16-risk-state-v1"
EPSILON = 1e-10
UTC_MS = pl.Datetime("ms", "UTC")

_RISK_STATE_SCHEMA = {
    "symbol": pl.String,
    "direction": pl.String,
    "average_entry_price": pl.Float64,
    "favorable_extreme": pl.Float64,
    "trigger_count": pl.Int32,
}
_COOLDOWN_SCHEMA = {
    "symbol": pl.String,
    "trigger_index": pl.Int64,
    "release_index": pl.Int64,
}
_PENDING_SCHEMA = {
    "event_id": pl.String,
    "fill_time": UTC_MS,
    "symbol": pl.String,
    "event_type": pl.String,
    "direction": pl.String,
    "action": pl.String,
    "reduce_fraction": pl.Float64,
    "reference_price": pl.Float64,
    "reason_code": pl.String,
    "priority": pl.Int16,
}
RISK_EVENT_SCHEMA = {
    "event_id": pl.String,
    "evaluation_time": UTC_MS,
    "trigger_time": UTC_MS,
    "symbol": pl.String,
    "event_type": pl.String,
    "direction": pl.String,
    "entry_price": pl.Float64,
    "trigger_level": pl.Float64,
    "observed_price": pl.Float64,
    "conflict_policy": pl.String,
    "action": pl.String,
    "fill_time": UTC_MS,
    "reason_code": pl.String,
    "run_id": pl.String,
}
_FILL_SCHEMA = {
    "instruction_id": pl.String,
    "source_event_id": pl.String,
    "decision_time": UTC_MS,
    "fill_time": UTC_MS,
    "symbol": pl.String,
    "side": pl.String,
    "instruction_mode": pl.String,
    "requested_delta_notional": pl.Float64,
    "constrained_delta_notional": pl.Float64,
    "reference_price": pl.Float64,
    "reason_code": pl.String,
    "priority": pl.Int16,
    "run_id": pl.String,
}
_RISK_SNAPSHOT_SCHEMA = {
    "timestamp": UTC_MS,
    "symbol": pl.String,
    "direction": pl.String,
    "average_entry_price": pl.Float64,
    "favorable_extreme": pl.Float64,
    "stop_loss_level": pl.Float64,
    "take_profit_level": pl.Float64,
    "trailing_stop_level": pl.Float64,
    "trigger_count": pl.Int32,
    "cooldown_release_index": pl.Int64,
    "run_id": pl.String,
}


class RiskEvaluationError(ValueError):
    """Risk inputs cannot produce an unambiguous no-lookahead decision."""


class RiskStateBudgetExceeded(RiskEvaluationError):
    """Risk or pending state exceeded a configured hard limit."""


@dataclass
class _RiskPosition:
    direction: str
    average_entry_price: float
    favorable_extreme: float
    trigger_count: int = 0


@dataclass(frozen=True)
class _Cooldown:
    trigger_index: int
    release_index: int


@dataclass(frozen=True)
class _Pending:
    event_id: str
    fill_time: datetime
    symbol: str
    event_type: str
    direction: str
    action: str
    reduce_fraction: float
    reference_price: float | None
    reason_code: str
    priority: int


@dataclass(frozen=True)
class RiskCheckpoint:
    evaluation_count: int
    sequence: int
    last_open_time: datetime | None
    last_close_time: datetime | None
    portfolio_peak_equity: float
    risk_positions: pl.DataFrame
    cooldowns: pl.DataFrame
    pending_intents: pl.DataFrame

    @property
    def risk_state_rows(self) -> int:
        return len(set(self.risk_positions["symbol"])) + len(
            set(self.cooldowns["symbol"])
            - set(self.risk_positions["symbol"])
        )

    @property
    def pending_intent_rows(self) -> int:
        return self.pending_intents.height


@dataclass(frozen=True)
class RiskEvaluation:
    events: pl.DataFrame
    state: pl.DataFrame
    pending_intents: pl.DataFrame
    checkpoint: RiskCheckpoint


@dataclass(frozen=True)
class RiskFillBatch:
    instructions: pl.DataFrame
    checkpoint: RiskCheckpoint


@dataclass(frozen=True)
class ReentryDecision:
    symbol: str
    allowed: bool
    reason_code: str
    release_index: int | None


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _frame(
    rows: list[dict[str, object]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return _empty(schema)
    return pl.DataFrame(rows).select(list(schema)).cast(schema)


class RiskStateMachine:
    """Evaluate ordered complete bars while retaining only live risk state."""

    def __init__(
        self,
        *,
        config: RiskV2Config,
        initial_equity: float,
        run_id: str,
        max_risk_state_rows: int = 20_000,
        max_pending_risk_intents: int = 20_000,
        checkpoint: RiskCheckpoint | None = None,
    ) -> None:
        if not run_id or run_id.lower() == "latest":
            raise RiskEvaluationError("run_id must be explicit")
        if not isfinite(initial_equity) or initial_equity <= 0:
            raise RiskEvaluationError("initial_equity must be positive and finite")
        if max_risk_state_rows < 1 or max_pending_risk_intents < 1:
            raise RiskEvaluationError("risk state budgets must be positive")
        self.config = config
        self.initial_equity = initial_equity
        self.run_id = run_id
        self.max_risk_state_rows = max_risk_state_rows
        self.max_pending_risk_intents = max_pending_risk_intents
        self._interval_seconds = duration_seconds(config.evaluation_interval)
        self._positions: dict[str, _RiskPosition] = {}
        self._cooldowns: dict[str, _Cooldown] = {}
        self._pending: dict[str, _Pending] = {}
        self._evaluation_count = 0
        self._sequence = 0
        self._last_open_time: datetime | None = None
        self._last_close_time: datetime | None = None
        self._portfolio_peak_equity = initial_equity
        if checkpoint is not None:
            self._restore(checkpoint)
        identity = {
            "engine": RISK_ENGINE_VERSION,
            "config": config.model_dump(mode="json"),
            "initial_equity": initial_equity,
            "max_risk_state_rows": max_risk_state_rows,
            "max_pending_risk_intents": max_pending_risk_intents,
            "run_id": run_id,
        }
        self.risk_version = f"a16-{content_sha256(identity)[:24]}"

    def _restore(self, checkpoint: RiskCheckpoint) -> None:
        if checkpoint.risk_state_rows > self.max_risk_state_rows:
            raise RiskStateBudgetExceeded(
                f"risk state rows {checkpoint.risk_state_rows} exceed "
                f"max_risk_state_rows={self.max_risk_state_rows}"
            )
        if checkpoint.pending_intent_rows > self.max_pending_risk_intents:
            raise RiskStateBudgetExceeded(
                f"pending risk intents {checkpoint.pending_intent_rows} exceed "
                f"max_pending_risk_intents={self.max_pending_risk_intents}"
            )
        self._positions = {
            str(row["symbol"]): _RiskPosition(
                direction=str(row["direction"]),
                average_entry_price=float(row["average_entry_price"]),
                favorable_extreme=float(row["favorable_extreme"]),
                trigger_count=int(row["trigger_count"]),
            )
            for row in checkpoint.risk_positions.to_dicts()
        }
        self._cooldowns = {
            str(row["symbol"]): _Cooldown(
                trigger_index=int(row["trigger_index"]),
                release_index=int(row["release_index"]),
            )
            for row in checkpoint.cooldowns.to_dicts()
        }
        self._pending = {
            str(row["symbol"]): _Pending(
                event_id=str(row["event_id"]),
                fill_time=row["fill_time"],
                symbol=str(row["symbol"]),
                event_type=str(row["event_type"]),
                direction=str(row["direction"]),
                action=str(row["action"]),
                reduce_fraction=float(row["reduce_fraction"]),
                reason_code=str(row["reason_code"]),
                reference_price=(
                    None
                    if row["reference_price"] is None
                    else float(row["reference_price"])
                ),
                priority=int(row["priority"]),
            )
            for row in checkpoint.pending_intents.to_dicts()
        }
        self._evaluation_count = checkpoint.evaluation_count
        self._sequence = checkpoint.sequence
        self._last_open_time = checkpoint.last_open_time
        self._last_close_time = checkpoint.last_close_time
        self._portfolio_peak_equity = checkpoint.portfolio_peak_equity

    def checkpoint(self) -> RiskCheckpoint:
        positions = _frame(
            [
                {
                    "symbol": symbol,
                    "direction": state.direction,
                    "average_entry_price": state.average_entry_price,
                    "favorable_extreme": state.favorable_extreme,
                    "trigger_count": state.trigger_count,
                }
                for symbol, state in sorted(self._positions.items())
            ],
            _RISK_STATE_SCHEMA,
        )
        cooldowns = _frame(
            [
                {
                    "symbol": symbol,
                    "trigger_index": value.trigger_index,
                    "release_index": value.release_index,
                }
                for symbol, value in sorted(self._cooldowns.items())
            ],
            _COOLDOWN_SCHEMA,
        )
        pending = _frame(
            [
                {
                    "event_id": item.event_id,
                    "fill_time": item.fill_time,
                    "symbol": item.symbol,
                    "reference_price": item.reference_price,
                    "event_type": item.event_type,
                    "direction": item.direction,
                    "action": item.action,
                    "reduce_fraction": item.reduce_fraction,
                    "reason_code": item.reason_code,
                    "priority": item.priority,
                }
                for item in sorted(self._pending.values(), key=lambda value: value.symbol)
            ],
            _PENDING_SCHEMA,
        )
        return RiskCheckpoint(
            evaluation_count=self._evaluation_count,
            sequence=self._sequence,
            last_open_time=self._last_open_time,
            last_close_time=self._last_close_time,
            portfolio_peak_equity=self._portfolio_peak_equity,
            risk_positions=positions,
            cooldowns=cooldowns,
            pending_intents=pending,
        )

    def evaluate(
        self,
        bars: pl.DataFrame | pl.LazyFrame,
        positions: pl.DataFrame | pl.LazyFrame,
        *,
        equity: float,
        price_source: str,
    ) -> RiskEvaluation:
        """Evaluate one complete risk-clock snapshot after its intrabar range is known."""

        if price_source != self.config.trigger_price:
            raise RiskEvaluationError(
                f"price_source={price_source} does not match trigger_price="
                f"{self.config.trigger_price}"
            )
        if not isfinite(equity) or equity <= 0:
            raise RiskEvaluationError("equity must be positive and finite")
        bar_rows, opened_at, closed_at = self._bars(bars)
        if any(item.fill_time <= opened_at for item in self._pending.values()):
            raise RiskEvaluationError(
                "due risk intents must be drained before evaluating their fill bar"
            )
        position_rows = self._position_rows(positions)
        if self.config.reentry_policy == "after_cooldown":
            expired = [
                symbol
                for symbol, cooldown in self._cooldowns.items()
                if cooldown.release_index <= self._evaluation_count
                and symbol not in position_rows
            ]
            for symbol in expired:
                self._cooldowns.pop(symbol, None)
        missing_position_bars = set(position_rows) - set(bar_rows)
        if missing_position_bars:
            raise RiskEvaluationError(
                f"risk bars are missing held symbols: {sorted(missing_position_bars)}"
            )
        potential_state_rows = len(set(position_rows) | set(self._cooldowns))
        if potential_state_rows > self.max_risk_state_rows:
            raise RiskStateBudgetExceeded(
                f"risk state rows {potential_state_rows} exceed "
                f"max_risk_state_rows={self.max_risk_state_rows}"
            )
        self._sync_positions(position_rows)
        events: list[dict[str, object]] = []
        portfolio_trigger = self._portfolio_trigger(equity)
        if portfolio_trigger is not None and position_rows:
            projected_pending = len(set(self._pending) | set(position_rows))
            if projected_pending > self.max_pending_risk_intents:
                raise RiskStateBudgetExceeded(
                    f"pending risk intents {projected_pending} exceed "
                    f"max_pending_risk_intents={self.max_pending_risk_intents}"
                )
            event_type, reason, level = portfolio_trigger
            event_id = self._event_id(closed_at, None, event_type)
            events.append(
                self._event_row(
                    event_id=event_id,
                    evaluation_time=closed_at,
                    symbol=None,
                    event_type=event_type,
                    direction=None,
                    entry_price=None,
                    trigger_level=level,
                    observed_price=equity,
                    action="close",
                    fill_time=closed_at,
                    reason=reason,
                )
            )
            for symbol, row in sorted(position_rows.items()):
                self._queue(
                    event_id=event_id,
                    fill_time=closed_at,
                    symbol=symbol,
                    event_type=event_type,
                    direction=row["direction"],
                    action="close",
                    reduce_fraction=1.0,
                    reason=reason,
                    priority=EventPriority.PORTFOLIO_RISK,
                )
                self._activate_cooldown(symbol)
        else:
            for symbol, row in sorted(position_rows.items()):
                if symbol in self._pending:
                    continue
                state = self._positions[symbol]
                maximum = self.config.max_triggers_per_symbol
                if maximum is not None and state.trigger_count >= maximum:
                    continue
                trigger = self._symbol_trigger(state, bar_rows[symbol])
                if trigger is None:
                    continue
                event_type, reason, level, observed, rule = trigger
                reference_price = None
                if self.config.fill_model == "same_bar_trigger":
                    bar_open = float(bar_rows[symbol]["open"])
                    if event_type in {"stop_loss", "trailing_stop"}:
                        reference_price = (
                            min(bar_open, level)
                            if state.direction == "LONG"
                            else max(bar_open, level)
                        )
                    else:
                        reference_price = level
                event_id = self._event_id(closed_at, symbol, event_type)
                events.append(
                    self._event_row(
                        event_id=event_id,
                        evaluation_time=closed_at,
                        symbol=symbol,
                        event_type=event_type,
                        direction=state.direction,
                        entry_price=state.average_entry_price,
                        trigger_level=level,
                        observed_price=observed,
                        action=rule.action,
                        fill_time=closed_at,
                        reason=reason,
                    )
                )
                fraction = (
                    1.0 if rule.action == "close" else float(rule.reduce_fraction)
                )
                self._queue(
                    event_id=event_id,
                    fill_time=closed_at,
                    symbol=symbol,
                    event_type=event_type,
                    direction=state.direction,
                    action=rule.action,
                    reduce_fraction=fraction,
                    reason=reason,
                    priority=EventPriority.SYMBOL_RISK,
                    reference_price=reference_price,
                )
                state.trigger_count += 1
                self._activate_cooldown(symbol)
        self._check_pending_budget()
        self._update_extremes(bar_rows)
        self._portfolio_peak_equity = max(self._portfolio_peak_equity, equity)
        self._last_open_time = opened_at
        self._last_close_time = closed_at
        self._evaluation_count += 1
        checkpoint = self.checkpoint()
        return RiskEvaluation(
            events=_frame(events, RISK_EVENT_SCHEMA),
            state=self._state_snapshot(closed_at),
            pending_intents=checkpoint.pending_intents,
            checkpoint=checkpoint,
        )

    def drain_due(
        self,
        *,
        open_time: datetime,
        opening_prices: Mapping[str, float],
        positions: pl.DataFrame | pl.LazyFrame,
    ) -> RiskFillBatch:
        """Create risk instructions at the actual next open, including gap prices."""

        if open_time.tzinfo is None:
            raise RiskEvaluationError("open_time must be timezone-aware")
        overdue = [
            item for item in self._pending.values() if item.fill_time < open_time
        ]
        if overdue:
            raise RiskEvaluationError("pending risk intent passed its fill_time")
        due = sorted(
            (item for item in self._pending.values() if item.fill_time == open_time),
            key=lambda item: (item.priority, item.symbol),
        )
        position_rows = self._position_rows(positions)
        rows: list[dict[str, object]] = []
        for item in due:
            price = float(
                item.reference_price
                if item.reference_price is not None
                else opening_prices.get(item.symbol, float("nan"))
            )
            if not isfinite(price) or price <= 0:
                raise RiskEvaluationError(
                    f"missing positive opening price for {item.symbol}"
                )
            position = position_rows.get(item.symbol)
            if position is None:
                requested = 0.0
                constrained = 0.0
                reason = V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value
                side = "FLAT"
            else:
                signed_notional = float(position["quantity"]) * price
                requested = -signed_notional * item.reduce_fraction
                constrained = requested
                reason = item.reason_code
                side = "FLAT"
            self._sequence += 1
            identity = {
                "version": self.risk_version,
                "event_id": item.event_id,
                "sequence": self._sequence,
                "fill_time": open_time.isoformat(),
            }
            rows.append(
                {
                    "instruction_id": (
                        f"risk-instruction-{content_sha256(identity)[:24]}"
                    ),
                    "source_event_id": item.event_id,
                    "decision_time": item.fill_time,
                    "fill_time": open_time,
                    "symbol": item.symbol,
                    "side": side,
                    "instruction_mode": "risk_reduce",
                    "requested_delta_notional": requested,
                    "constrained_delta_notional": constrained,
                    "reference_price": price,
                    "reason_code": reason,
                    "priority": item.priority,
                    "run_id": self.run_id,
                }
            )
            self._pending.pop(item.symbol, None)
        return RiskFillBatch(
            instructions=_frame(rows, _FILL_SCHEMA), checkpoint=self.checkpoint()
        )

    def reentry_decision(
        self, symbol: str, *, scheduled_rebalance: bool
    ) -> ReentryDecision:
        cooldown = self._cooldowns.get(symbol)
        if cooldown is None:
            return ReentryDecision(symbol, True, V2ReasonCode.ACCEPTED.value, None)
        if self.config.reentry_policy == "next_scheduled_rebalance":
            allowed = scheduled_rebalance and self._evaluation_count > cooldown.trigger_index
        else:
            allowed = self._evaluation_count >= cooldown.release_index
        if allowed:
            self._cooldowns.pop(symbol, None)
            return ReentryDecision(
                symbol, True, V2ReasonCode.ACCEPTED.value, cooldown.release_index
            )
        return ReentryDecision(
            symbol,
            False,
            V2ReasonCode.COOLDOWN_ACTIVE.value,
            cooldown.release_index,
        )

    def finish(self) -> pl.DataFrame:
        """Return explicit end-of-data diagnostics for unfilled next-open intents."""

        return self.checkpoint().pending_intents.with_columns(
            pl.lit(V2ReasonCode.END_OF_DATA_UNFILLED.value).alias("reason_code")
        )

    def _bars(
        self, bars: pl.DataFrame | pl.LazyFrame
    ) -> tuple[dict[str, dict[str, object]], datetime, datetime]:
        frame = bars.collect(engine="streaming") if isinstance(bars, pl.LazyFrame) else bars
        required = {
            "open_time",
            "close_time",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "is_complete",
        }
        missing = required - set(frame.columns)
        if missing:
            raise RiskEvaluationError(f"risk bars are missing columns: {sorted(missing)}")
        normalized = frame.with_columns(
            pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS)
        ).sort("symbol")
        opens = normalized["open_time"].unique().to_list()
        closes = normalized["close_time"].unique().to_list()
        if len(opens) != 1 or len(closes) != 1:
            raise RiskEvaluationError("one risk snapshot must share open_time/close_time")
        opened_at = opens[0]
        closed_at = closes[0]
        if (closed_at - opened_at).total_seconds() != self._interval_seconds:
            raise RiskEvaluationError("risk bar duration does not match evaluation_interval")
        if self._last_close_time is not None and opened_at != self._last_close_time:
            raise RiskEvaluationError("risk bars must be contiguous across chunks")
        rows: dict[str, dict[str, object]] = {}
        for row in normalized.to_dicts():
            symbol = str(row["symbol"])
            if symbol in rows:
                raise RiskEvaluationError("risk bars contain duplicate symbols")
            if not bool(row["is_complete"]):
                raise RiskEvaluationError("risk bars must be complete")
            prices = [float(row[name]) for name in ("open", "high", "low", "close")]
            if any(not isfinite(value) or value <= 0 for value in prices):
                raise RiskEvaluationError("risk bars contain invalid prices")
            if prices[1] < max(prices[0], prices[3]) or prices[2] > min(prices[0], prices[3]):
                raise RiskEvaluationError("risk bars contain inconsistent OHLC")
            rows[symbol] = row
        return rows, opened_at, closed_at

    def _position_rows(
        self, positions: pl.DataFrame | pl.LazyFrame
    ) -> dict[str, dict[str, object]]:
        frame = (
            positions.collect(engine="streaming")
            if isinstance(positions, pl.LazyFrame)
            else positions
        )
        required = {"symbol", "quantity", "average_entry_price"}
        missing = required - set(frame.columns)
        if missing:
            raise RiskEvaluationError(f"positions are missing columns: {sorted(missing)}")
        rows: dict[str, dict[str, object]] = {}
        for row in frame.select(*required).to_dicts():
            symbol = str(row["symbol"])
            quantity = float(row["quantity"])
            average = float(row["average_entry_price"])
            if symbol in rows:
                raise RiskEvaluationError("positions contain duplicate symbols")
            if abs(quantity) <= EPSILON:
                continue
            if not isfinite(quantity) or not isfinite(average) or average <= 0:
                raise RiskEvaluationError("positions contain invalid state")
            rows[symbol] = {
                "symbol": symbol,
                "quantity": quantity,
                "average_entry_price": average,
                "direction": "LONG" if quantity > 0 else "SHORT",
            }
        return rows

    def _sync_positions(self, rows: dict[str, dict[str, object]]) -> None:
        for symbol in set(self._positions) - set(rows):
            self._positions.pop(symbol, None)
        for symbol, row in rows.items():
            direction = str(row["direction"])
            average = float(row["average_entry_price"])
            current = self._positions.get(symbol)
            if current is None or current.direction != direction:
                self._positions[symbol] = _RiskPosition(direction, average, average)
            else:
                current.average_entry_price = average

    def _symbol_trigger(
        self, state: _RiskPosition, bar: dict[str, object]
    ) -> tuple[str, V2ReasonCode, float, float, SymbolExitRuleConfig] | None:
        hits: list[
            tuple[str, V2ReasonCode, float, float, SymbolExitRuleConfig]
        ] = []
        rules = self.config.symbol_exits
        if rules.stop_loss.enabled:
            distance = self._distance(rules.stop_loss, state.direction)
            level = state.average_entry_price * (
                1.0 - distance if state.direction == "LONG" else 1.0 + distance
            )
            observed = float(bar["low"] if state.direction == "LONG" else bar["high"])
            if (state.direction == "LONG" and observed <= level) or (
                state.direction == "SHORT" and observed >= level
            ):
                hits.append(("stop_loss", V2ReasonCode.STOP_LOSS_TRIGGERED, level, observed, rules.stop_loss))
        if rules.trailing_stop.enabled:
            if self._trailing_active(rules.trailing_stop, state):
                distance = self._distance(rules.trailing_stop, state.direction)
                level = state.favorable_extreme * (
                    1.0 - distance if state.direction == "LONG" else 1.0 + distance
                )
                observed = float(bar["low"] if state.direction == "LONG" else bar["high"])
                if (state.direction == "LONG" and observed <= level) or (
                    state.direction == "SHORT" and observed >= level
                ):
                    hits.append(
                        (
                            "trailing_stop",
                            V2ReasonCode.TRAILING_STOP_TRIGGERED,
                            level,
                            observed,
                            rules.trailing_stop,
                        )
                    )
        if rules.take_profit.enabled:
            distance = self._distance(rules.take_profit, state.direction)
            level = state.average_entry_price * (
                1.0 + distance if state.direction == "LONG" else 1.0 - distance
            )
            observed = float(bar["high"] if state.direction == "LONG" else bar["low"])
            if (state.direction == "LONG" and observed >= level) or (
                state.direction == "SHORT" and observed <= level
            ):
                hits.append(("take_profit", V2ReasonCode.TAKE_PROFIT_TRIGGERED, level, observed, rules.take_profit))
        if len(hits) > 1 and self.config.intrabar_conflict == "error":
            raise RiskEvaluationError("multiple risk levels triggered in one OHLC bar")
        if not hits:
            return None
        order = {"stop_loss": 0, "trailing_stop": 1, "take_profit": 2}
        return sorted(hits, key=lambda item: order[item[0]])[0]

    def _portfolio_trigger(
        self, equity: float
    ) -> tuple[str, V2ReasonCode, float] | None:
        exits = self.config.portfolio_exits
        candidates: list[tuple[str, V2ReasonCode, float]] = []
        if exits.stop_loss is not None:
            level = self.initial_equity * (1.0 - exits.stop_loss)
            if equity <= level:
                candidates.append(("portfolio_stop_loss", V2ReasonCode.PORTFOLIO_STOP_LOSS_TRIGGERED, level))
        if exits.max_drawdown is not None:
            level = self._portfolio_peak_equity * (1.0 - exits.max_drawdown)
            if equity <= level:
                candidates.append(("portfolio_max_drawdown", V2ReasonCode.PORTFOLIO_MAX_DRAWDOWN_TRIGGERED, level))
        if exits.take_profit is not None:
            level = self.initial_equity * (1.0 + exits.take_profit)
            if equity >= level:
                candidates.append(("portfolio_take_profit", V2ReasonCode.PORTFOLIO_TAKE_PROFIT_TRIGGERED, level))
        return candidates[0] if candidates else None

    def _queue(
        self,
        *,
        event_id: str,
        fill_time: datetime,
        symbol: str,
        event_type: str,
        direction: str,
        action: str,
        reduce_fraction: float,
        reason: V2ReasonCode,
        priority: EventPriority,
        reference_price: float | None = None,
    ) -> None:
        existing = self._pending.get(symbol)
        if existing is not None and existing.priority <= int(priority):
            return
        self._pending[symbol] = _Pending(
            event_id=event_id,
            fill_time=fill_time,
            symbol=symbol,
            event_type=event_type,
            direction=direction,
            action=action,
            reduce_fraction=reduce_fraction,
            reason_code=reason.value,
            reference_price=reference_price,
            priority=int(priority),
        )

    def _activate_cooldown(self, symbol: str) -> None:
        trigger_index = self._evaluation_count + 1
        self._cooldowns[symbol] = _Cooldown(
            trigger_index=trigger_index,
            release_index=trigger_index + max(1, self.config.cooldown_bars),
        )

    def _update_extremes(self, bars: dict[str, dict[str, object]]) -> None:
        for symbol, state in self._positions.items():
            bar = bars.get(symbol)
            if bar is None:
                raise RiskEvaluationError(f"missing risk bar for held symbol {symbol}")
            if state.direction == "LONG":
                state.favorable_extreme = max(state.favorable_extreme, float(bar["high"]))
            else:
                state.favorable_extreme = min(state.favorable_extreme, float(bar["low"]))

    def _state_snapshot(self, timestamp: datetime) -> pl.DataFrame:
        rows = []
        for symbol, state in sorted(self._positions.items()):
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "direction": state.direction,
                    "average_entry_price": state.average_entry_price,
                    "favorable_extreme": state.favorable_extreme,
                    "stop_loss_level": self._level(self.config.symbol_exits.stop_loss, state, "stop"),
                    "take_profit_level": self._level(self.config.symbol_exits.take_profit, state, "take"),
                    "trailing_stop_level": self._level(self.config.symbol_exits.trailing_stop, state, "trailing"),
                    "trigger_count": state.trigger_count,
                    "cooldown_release_index": (
                        self._cooldowns[symbol].release_index if symbol in self._cooldowns else None
                    ),
                    "run_id": self.run_id,
                }
            )
        return _frame(rows, _RISK_SNAPSHOT_SCHEMA)

    def _level(
        self, rule: SymbolExitRuleConfig, state: _RiskPosition, kind: str
    ) -> float | None:
        if not rule.enabled:
            return None
        distance = self._distance(rule, state.direction)
        if kind == "stop":
            return state.average_entry_price * (1.0 - distance if state.direction == "LONG" else 1.0 + distance)
        if kind == "take":
            return state.average_entry_price * (1.0 + distance if state.direction == "LONG" else 1.0 - distance)
        if not self._trailing_active(rule, state):
            return None
        return state.favorable_extreme * (1.0 - distance if state.direction == "LONG" else 1.0 + distance)

    @staticmethod
    def _trailing_active(rule: SymbolExitRuleConfig, state: _RiskPosition) -> bool:
        activation = rule.activation_distance
        if activation is None:
            return True
        threshold = state.average_entry_price * (
            1.0 + activation if state.direction == "LONG" else 1.0 - activation
        )
        return (
            state.favorable_extreme >= threshold
            if state.direction == "LONG"
            else state.favorable_extreme <= threshold
        )

    @staticmethod
    def _distance(rule: SymbolExitRuleConfig, direction: str) -> float:
        value = rule.distance if rule.distance is not None else (
            rule.long_distance if direction == "LONG" else rule.short_distance
        )
        assert value is not None
        return float(value)

    def _event_id(self, time: datetime, symbol: str | None, event_type: str) -> str:
        self._sequence += 1
        identity = {
            "version": self.risk_version,
            "sequence": self._sequence,
            "time": time.isoformat(),
            "symbol": symbol,
            "event_type": event_type,
        }
        return f"risk-event-{content_sha256(identity)[:24]}"

    def _event_row(
        self,
        *,
        event_id: str,
        evaluation_time: datetime,
        symbol: str | None,
        event_type: str,
        direction: str | None,
        entry_price: float | None,
        trigger_level: float,
        observed_price: float,
        action: str,
        fill_time: datetime,
        reason: V2ReasonCode,
    ) -> dict[str, object]:
        return {
            "event_id": event_id,
            "evaluation_time": evaluation_time,
            "trigger_time": evaluation_time,
            "symbol": symbol,
            "event_type": event_type,
            "direction": direction,
            "entry_price": entry_price,
            "trigger_level": trigger_level,
            "observed_price": observed_price,
            "conflict_policy": self.config.intrabar_conflict,
            "action": action,
            "fill_time": fill_time,
            "reason_code": reason.value,
            "run_id": self.run_id,
        }

    def _check_state_budget(self) -> None:
        rows = len(set(self._positions) | set(self._cooldowns))
        if rows > self.max_risk_state_rows:
            raise RiskStateBudgetExceeded(
                f"risk state rows {rows} exceed max_risk_state_rows="
                f"{self.max_risk_state_rows}"
            )

    def _check_pending_budget(self) -> None:
        if len(self._pending) > self.max_pending_risk_intents:
            raise RiskStateBudgetExceeded(
                f"pending risk intents {len(self._pending)} exceed "
                f"max_pending_risk_intents={self.max_pending_risk_intents}"
            )
