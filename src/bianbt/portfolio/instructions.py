"""Incremental V2 position instructions and simple-cross capital state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import floor, isclose, isfinite
from typing import Mapping

import polars as pl

from bianbt.config.backtest import (
    CapitalConfig,
    FeeConfig,
    HoldingPolicyConfig,
    PortfolioConstraintsV2Config,
    PositionSizingConfig,
    SlippageConfig,
)
from bianbt.data.hashing import content_sha256
from bianbt.data.v2_contracts import EventPriority, V2ReasonCode
from bianbt.engine.costs import fee_rate, slippage_rate
from bianbt.engine.execution import updated_average_entry

INSTRUCTION_ENGINE_VERSION = "a15-instructions-v1"
EPSILON = 1e-10
UTC_MS = pl.Datetime("ms", "UTC")

_POSITION_STATE_SCHEMA = {
    "symbol": pl.String,
    "quantity": pl.Float64,
    "average_entry_price": pl.Float64,
    "consecutive_adds": pl.Int32,
}
_INSTRUCTION_SCHEMA = {
    "instruction_id": pl.String,
    "decision_time": UTC_MS,
    "rank_source_time": UTC_MS,
    "symbol": pl.String,
    "side": pl.String,
    "instruction_mode": pl.String,
    "sizing_base_notional": pl.Float64,
    "pretrade_symbol_notional": pl.Float64,
    "requested_delta_notional": pl.Float64,
    "constrained_delta_notional": pl.Float64,
    "posttrade_symbol_notional": pl.Float64,
    "requested_target_weight": pl.Float64,
    "source_event_id": pl.String,
    "reason_code": pl.String,
    "constraint_flags": pl.String,
    "priority": pl.Int16,
    "fee_cost": pl.Float64,
    "slippage_cost": pl.Float64,
    "used_margin": pl.Float64,
    "available_margin": pl.Float64,
    "run_id": pl.String,
}
_POSITION_SNAPSHOT_SCHEMA = {
    "timestamp": UTC_MS,
    "symbol": pl.String,
    "quantity": pl.Float64,
    "signed_notional": pl.Float64,
    "average_entry_price": pl.Float64,
    "unrealized_pnl": pl.Float64,
    "used_margin": pl.Float64,
    "consecutive_adds": pl.Int32,
    "run_id": pl.String,
}
_EXTERNAL_EXECUTION_SCHEMA = {
    "symbol": pl.String,
    "delta_notional": pl.Float64,
    "fee_cost": pl.Float64,
    "slippage_cost": pl.Float64,
}


class PositionInstructionError(ValueError):
    """A V2 sizing decision cannot be represented safely."""


class PositionStateBudgetExceeded(PositionInstructionError):
    """The bounded current-position state exceeded its configured hard limit."""


@dataclass
class PositionState:
    quantity: float
    average_entry_price: float
    consecutive_adds: int


@dataclass(frozen=True)
class PositionCheckpoint:
    """Serializable minimum state carried across execution chunks."""

    cash_balance: float
    sequence: int
    last_decision_time: datetime | None
    positions: pl.DataFrame
    rolling_margin: float | None = None
    rolling_active_margin: float | None = None
    rolling_round_net_pnl: float = 0.0
    rolling_reset_count: int = 0
    rolling_last_reset_reason: str | None = None

    @property
    def position_state_rows(self) -> int:
        return self.positions.height


@dataclass(frozen=True)
class AccountSnapshot:
    timestamp: datetime
    cash_balance: float
    unrealized_pnl: float
    equity: float
    used_margin: float
    available_margin: float
    gross_notional: float
    net_notional: float
    turnover_notional: float
    fee_cost: float
    slippage_cost: float
    position_state_rows: int
    pending_instruction_count: int = 0


@dataclass(frozen=True)
class InstructionBatch:
    instructions: pl.DataFrame
    positions: pl.DataFrame
    account: AccountSnapshot
    checkpoint: PositionCheckpoint


@dataclass(frozen=True)
class ExternalExecutionBatch:
    executions: pl.DataFrame
    positions: pl.DataFrame
    account: AccountSnapshot
    checkpoint: PositionCheckpoint


@dataclass
class _MutableState:
    cash_balance: float
    positions: dict[str, PositionState] = field(default_factory=dict)
    sequence: int = 0
    last_decision_time: datetime | None = None
    rolling_margin: float | None = None
    rolling_active_margin: float | None = None
    rolling_round_net_pnl: float = 0.0
    rolling_reset_count: int = 0
    rolling_last_reset_reason: str | None = None


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _frame(
    rows: list[dict[str, object]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return _empty(schema)
    return pl.DataFrame(rows).select(list(schema)).cast(schema)


class IncrementalPositionEngine:
    """Turn independent LONG/SHORT selections into bounded incremental state."""

    _CONSTRAINT_ORDER = (
        V2ReasonCode.SCALED_MAX_GROSS_EXPOSURE,
        V2ReasonCode.SCALED_MAX_NET_EXPOSURE,
        V2ReasonCode.SCALED_MAX_SYMBOL_WEIGHT,
        V2ReasonCode.SCALED_MAX_SYMBOL_NOTIONAL,
        V2ReasonCode.SCALED_MAX_TURNOVER,
        V2ReasonCode.REJECTED_INSUFFICIENT_MARGIN,
    )

    def __init__(
        self,
        *,
        sizing: PositionSizingConfig,
        constraints: PortfolioConstraintsV2Config,
        holding: HoldingPolicyConfig | None = None,
        capital: CapitalConfig,
        leverage: float,
        fee: FeeConfig,
        slippage: SlippageConfig,
        run_id: str,
        quantity_steps: Mapping[str, float] | None = None,
        max_position_state_rows: int = 20_000,
        max_pending_instructions: int = 20_000,
        checkpoint: PositionCheckpoint | None = None,
    ) -> None:
        if not run_id or run_id.lower() == "latest":
            raise PositionInstructionError("run_id must be explicit")
        if not isfinite(leverage) or leverage <= 0:
            raise PositionInstructionError("leverage must be positive and finite")
        if max_position_state_rows < 1:
            raise PositionInstructionError(
                "max_position_state_rows must be positive"
            )
        if max_pending_instructions < 1:
            raise PositionInstructionError(
                "max_pending_instructions must be positive"
            )
        self.sizing = sizing
        self.constraints = constraints
        self.holding = holding or HoldingPolicyConfig()
        self.capital = capital
        self.leverage = leverage
        self.run_id = run_id
        self.max_position_state_rows = max_position_state_rows
        self.max_pending_instructions = max_pending_instructions
        self._fee_rate = fee_rate(fee)
        self._slippage_rate = slippage_rate(slippage)
        self._cost_rate = self._fee_rate + self._slippage_rate
        self._quantity_steps = dict(quantity_steps or {})
        for symbol, step in self._quantity_steps.items():
            if not symbol or not isfinite(step) or step <= 0:
                raise PositionInstructionError(
                    "quantity_steps must map symbols to positive finite values"
                )
        self._state = self._restore(checkpoint)
        identity = {
            "engine": INSTRUCTION_ENGINE_VERSION,
            "sizing": sizing.model_dump(mode="json"),
            "constraints": constraints.model_dump(mode="json"),
            "holding": self.holding.model_dump(mode="json"),
            "capital": capital.model_dump(mode="json"),
            "leverage": leverage,
            "fee_rate": self._fee_rate,
            "slippage_rate": self._slippage_rate,
            "quantity_steps": self._quantity_steps,
            "max_position_state_rows": self.max_position_state_rows,
            "max_pending_instructions": self.max_pending_instructions,
        }
        self.instruction_version = f"a15-{content_sha256(identity)[:24]}"

    def _restore(self, checkpoint: PositionCheckpoint | None) -> _MutableState:
        if checkpoint is None:
            return _MutableState(
                cash_balance=self.capital.initial_equity,
                rolling_margin=(
                    self.sizing.rolling_initial_margin
                    if self.sizing.mode == "rolling_margin"
                    else None
                ),
            )
        if checkpoint.position_state_rows > self.max_position_state_rows:
            raise PositionStateBudgetExceeded(
                f"position state rows {checkpoint.position_state_rows} exceed "
                f"max_position_state_rows={self.max_position_state_rows}"
            )
        names = set(checkpoint.positions.columns)
        missing = set(_POSITION_STATE_SCHEMA) - names
        if missing:
            raise PositionInstructionError(
                f"checkpoint is missing columns: {sorted(missing)}"
            )
        positions: dict[str, PositionState] = {}
        for row in checkpoint.positions.select(list(_POSITION_STATE_SCHEMA)).to_dicts():
            symbol = str(row["symbol"])
            if symbol in positions:
                raise PositionInstructionError("checkpoint contains duplicate symbols")
            quantity = float(row["quantity"])
            average = float(row["average_entry_price"])
            adds = int(row["consecutive_adds"])
            if (
                not isfinite(quantity)
                or abs(quantity) <= EPSILON
                or not isfinite(average)
                or average <= 0
                or adds < 1
            ):
                raise PositionInstructionError("checkpoint contains invalid position state")
            positions[symbol] = PositionState(quantity, average, adds)
        if self.sizing.mode == "rolling_margin":
            if checkpoint.rolling_margin is None or checkpoint.rolling_margin <= 0:
                raise PositionInstructionError("rolling checkpoint is missing current margin")
            values = (
                checkpoint.rolling_margin,
                checkpoint.rolling_round_net_pnl,
                checkpoint.rolling_active_margin,
            )
            if any(value is not None and not isfinite(value) for value in values):
                raise PositionInstructionError("rolling checkpoint contains non-finite state")
            if checkpoint.rolling_reset_count < 0:
                raise PositionInstructionError("rolling reset count cannot be negative")
            if bool(positions) != (checkpoint.rolling_active_margin is not None):
                raise PositionInstructionError(
                    "rolling active margin must match held-position state"
                )
            if not positions and abs(checkpoint.rolling_round_net_pnl) > EPSILON:
                raise PositionInstructionError("flat rolling checkpoint contains unsettled PnL")
        elif checkpoint.rolling_margin is not None:
            raise PositionInstructionError("non-rolling checkpoint contains rolling state")
        return _MutableState(
            cash_balance=checkpoint.cash_balance,
            positions=positions,
            sequence=checkpoint.sequence,
            last_decision_time=checkpoint.last_decision_time,
            rolling_margin=checkpoint.rolling_margin,
            rolling_active_margin=checkpoint.rolling_active_margin,
            rolling_round_net_pnl=checkpoint.rolling_round_net_pnl,
            rolling_reset_count=checkpoint.rolling_reset_count,
            rolling_last_reset_reason=checkpoint.rolling_last_reset_reason,
        )

    def checkpoint(self) -> PositionCheckpoint:
        rows = [
            {
                "symbol": symbol,
                "quantity": state.quantity,
                "average_entry_price": state.average_entry_price,
                "consecutive_adds": state.consecutive_adds,
            }
            for symbol, state in sorted(self._state.positions.items())
        ]
        return PositionCheckpoint(
            cash_balance=self._state.cash_balance,
            sequence=self._state.sequence,
            last_decision_time=self._state.last_decision_time,
            positions=_frame(rows, _POSITION_STATE_SCHEMA),
            rolling_margin=self._state.rolling_margin,
            rolling_active_margin=self._state.rolling_active_margin,
            rolling_round_net_pnl=self._state.rolling_round_net_pnl,
            rolling_reset_count=self._state.rolling_reset_count,
            rolling_last_reset_reason=self._state.rolling_last_reset_reason,
        )

    def account_snapshot(
        self, timestamp: datetime, marks: Mapping[str, float]
    ) -> AccountSnapshot:
        """Value current state without changing positions or cash."""

        normalized_marks = self._validate_marks(marks)
        return self._account_snapshot(
            timestamp,
            normalized_marks,
            turnover=0.0,
            fee_cost=0.0,
            slippage_cost=0.0,
        )

    def position_snapshot(
        self, timestamp: datetime, marks: Mapping[str, float]
    ) -> pl.DataFrame:
        """Return current positions valued at the supplied marks."""

        normalized_marks = self._validate_marks(marks)
        return self._position_snapshot(timestamp, normalized_marks)

    def apply_cashflow(self, amount: float, *, symbol: str | None = None) -> None:
        """Apply an externally modeled cashflow such as perpetual funding."""

        value = float(amount)
        if not isfinite(value):
            raise PositionInstructionError("cashflow must be finite")
        if (
            self.sizing.mode == "rolling_margin"
            and self._state.rolling_active_margin is not None
        ):
            if symbol is None or symbol not in self._state.positions:
                raise PositionInstructionError("rolling cashflow requires the held symbol")
        self._state.cash_balance += value
        if (
            self.sizing.mode == "rolling_margin"
            and self._state.rolling_active_margin is not None
        ):
            self._state.rolling_round_net_pnl += value

    def apply_external_deltas(
        self,
        deltas: pl.DataFrame | pl.LazyFrame,
        *,
        timestamp: datetime,
        marks: Mapping[str, float],
    ) -> ExternalExecutionBatch:
        """Apply already-arbitrated risk or forced-exit deltas."""

        normalized_marks = self._validate_marks(marks)
        frame = (
            deltas.collect(engine="streaming")
            if isinstance(deltas, pl.LazyFrame)
            else deltas
        )
        missing = {"symbol", "constrained_delta_notional"} - set(frame.columns)
        if missing:
            raise PositionInstructionError(
                f"external deltas are missing columns: {sorted(missing)}"
            )
        if frame.height > self.max_pending_instructions:
            raise PositionStateBudgetExceeded(
                f"external deltas {frame.height} exceed "
                f"max_pending_instructions={self.max_pending_instructions}"
            )
        rows: list[dict[str, object]] = []
        turnover = 0.0
        total_fee = 0.0
        total_slippage = 0.0
        for row in frame.sort("symbol").to_dicts():
            symbol = str(row["symbol"])
            if symbol not in normalized_marks:
                raise PositionInstructionError(
                    f"missing mark for external delta symbol {symbol}"
                )
            delta = float(row["constrained_delta_notional"] or 0.0)
            fee_value, slip_value = self._execute(
                symbol=symbol,
                delta_notional=delta,
                price=normalized_marks[symbol],
            )
            turnover += abs(delta)
            total_fee += fee_value
            total_slippage += slip_value
            rows.append(
                {
                    "symbol": symbol,
                    "delta_notional": delta,
                    "fee_cost": fee_value,
                    "slippage_cost": slip_value,
                }
            )
        account = self._account_snapshot(
            timestamp,
            normalized_marks,
            turnover=turnover,
            fee_cost=total_fee,
            slippage_cost=total_slippage,
        )
        return ExternalExecutionBatch(
            executions=_frame(rows, _EXTERNAL_EXECUTION_SCHEMA),
            positions=self._position_snapshot(timestamp, normalized_marks),
            account=account,
            checkpoint=self.checkpoint(),
        )

    def process(
        self,
        selections: pl.DataFrame | pl.LazyFrame,
        *,
        decision_time: datetime,
        marks: Mapping[str, float],
    ) -> InstructionBatch:
        """Process one complete decision snapshot in deterministic symbol order."""

        if decision_time.tzinfo is None:
            raise PositionInstructionError("decision_time must be timezone-aware")
        if (
            self._state.last_decision_time is not None
            and decision_time <= self._state.last_decision_time
        ):
            raise PositionInstructionError(
                "decision_time must advance strictly across batches"
            )
        normalized_marks = self._validate_marks(marks)
        selection_rows = self._selection_rows(selections, decision_time)
        if (
            self.holding.mode == "single_position_replace"
            and len(selection_rows) > 1
        ):
            raise PositionInstructionError("single_position_replace accepts one signal")
        if len(selection_rows) > self.max_pending_instructions:
            raise PositionStateBudgetExceeded(
                f"pending instructions {len(selection_rows)} exceed "
                f"max_pending_instructions={self.max_pending_instructions}"
            )
        missing_selected_marks = {
            str(row["symbol"]) for row in selection_rows
        } - set(normalized_marks)
        if missing_selected_marks:
            raise PositionInstructionError(
                f"marks are missing selected symbols: {sorted(missing_selected_marks)}"
            )
        possible_symbols = set(self._state.positions) | {
            str(row["symbol"]) for row in selection_rows
        }
        if self.holding.mode == "single_position_replace" and selection_rows:
            possible_symbols = {str(selection_rows[0]["symbol"])}
        if len(possible_symbols) > self.max_position_state_rows:
            raise PositionStateBudgetExceeded(
                f"potential position state rows {len(possible_symbols)} exceed "
                f"max_position_state_rows={self.max_position_state_rows}"
            )
        if (
            self.sizing.mode == "position_fraction"
            and self.sizing.zero_position_policy == "error"
        ):
            zero_symbols = sorted(
                str(row["symbol"])
                for row in selection_rows
                if str(row["symbol"]) not in self._state.positions
            )
            if zero_symbols:
                raise PositionInstructionError(
                    "position_fraction cannot size zero positions: "
                    f"{zero_symbols}"
                )
        pretrade = self._account_snapshot(
            decision_time,
            normalized_marks,
            turnover=0.0,
            fee_cost=0.0,
            slippage_cost=0.0,
        )
        batch_turnover = 0.0
        batch_fee = 0.0
        batch_slippage = 0.0
        instruction_rows: list[dict[str, object]] = []
        if self.holding.mode == "single_position_replace" and selection_rows:
            selected = selection_rows[0]
            selected_symbol = str(selected["symbol"])
            held_symbols = set(self._state.positions)
            if (
                selected_symbol in held_symbols
                and self.holding.existing_signal == "ignore"
            ):
                old_state = self._state.positions[selected_symbol]
                old_notional = (
                    old_state.quantity * normalized_marks[selected_symbol]
                )
                instruction_rows.append(
                    self._instruction_row(
                        decision_time=decision_time,
                        selection=selected,
                        symbol=selected_symbol,
                        side=str(selected["side"]),
                        base_notional=0.0,
                        old_notional=old_notional,
                        requested_delta=0.0,
                        constrained_delta=0.0,
                        reason=V2ReasonCode.ALREADY_HELD,
                        flags=(),
                        fee_cost=0.0,
                        slippage_cost=0.0,
                        marks=normalized_marks,
                    )
                )
                selection_rows = []
            else:
                for old_symbol in sorted(held_symbols - {selected_symbol}):
                    price = normalized_marks[old_symbol]
                    old_state = self._state.positions[old_symbol]
                    old_notional = old_state.quantity * price
                    close_delta = -old_notional
                    fee_value, slip_value = self._execute(
                        symbol=old_symbol,
                        delta_notional=close_delta,
                        price=price,
                    )
                    batch_turnover += abs(close_delta)
                    batch_fee += fee_value
                    batch_slippage += slip_value
                    instruction_rows.append(
                        self._instruction_row(
                            decision_time=decision_time,
                            selection=selected,
                            symbol=old_symbol,
                            side="FLAT",
                            base_notional=abs(old_notional),
                            old_notional=old_notional,
                            requested_delta=close_delta,
                            constrained_delta=close_delta,
                            reason=V2ReasonCode.REPLACED_BY_SIGNAL,
                            flags=(),
                            fee_cost=fee_value,
                            slippage_cost=slip_value,
                            marks=normalized_marks,
                        )
                    )
                pretrade = self._account_snapshot(
                    decision_time,
                    normalized_marks,
                    turnover=batch_turnover,
                    fee_cost=batch_fee,
                    slippage_cost=batch_slippage,
                )

        for row in selection_rows:
            result = self._process_one(
                row,
                decision_time=decision_time,
                marks=normalized_marks,
                sizing_equity=pretrade.equity,
                batch_turnover=batch_turnover,
            )
            batch_turnover += abs(float(result["constrained_delta_notional"]))
            batch_fee += float(result["fee_cost"])
            batch_slippage += float(result["slippage_cost"])
            instruction_rows.append(result)
        self._state.last_decision_time = decision_time
        account = self._account_snapshot(
            decision_time,
            normalized_marks,
            turnover=batch_turnover,
            fee_cost=batch_fee,
            slippage_cost=batch_slippage,
        )
        positions = self._position_snapshot(decision_time, normalized_marks)
        return InstructionBatch(
            instructions=_frame(instruction_rows, _INSTRUCTION_SCHEMA),
            positions=positions,
            account=account,
            checkpoint=self.checkpoint(),
        )

    def _selection_rows(
        self,
        selections: pl.DataFrame | pl.LazyFrame,
        decision_time: datetime,
    ) -> list[dict[str, object]]:
        frame = (
            selections.collect(engine="streaming")
            if isinstance(selections, pl.LazyFrame)
            else selections
        )
        missing = {"symbol", "side"} - set(frame.columns)
        if missing:
            raise PositionInstructionError(
                f"selection input is missing columns: {sorted(missing)}"
            )
        if "signal_time" in frame.columns:
            frame = frame.filter(
                pl.col("signal_time").cast(UTC_MS) == decision_time
            )
        optional = [
            name
            for name in ("rank_source_time", "target_weight")
            if name in frame.columns
        ]
        rows = frame.select("symbol", "side", *optional).sort("symbol").to_dicts()
        if self.sizing.mode == "target_weight" and "target_weight" not in frame.columns:
            raise PositionInstructionError(
                "target_weight sizing requires target_weight"
            )
        symbols: set[str] = set()
        for row in rows:
            symbol = str(row["symbol"])
            side = str(row["side"])
            if not symbol or side not in {"LONG", "SHORT", "FLAT"}:
                raise PositionInstructionError(
                    "selection symbols must be non-empty and sides LONG, SHORT, or FLAT"
                )
            if symbol in symbols:
                raise PositionInstructionError(
                    "selection contains duplicate symbols at one decision"
                )
            symbols.add(symbol)
        return rows

    def _validate_marks(self, marks: Mapping[str, float]) -> dict[str, float]:
        normalized = {symbol: float(value) for symbol, value in marks.items()}
        required = set(self._state.positions)
        missing = required - set(normalized)
        if missing:
            raise PositionInstructionError(
                f"marks are missing held symbols: {sorted(missing)}"
            )
        for symbol, value in normalized.items():
            if not symbol or not isfinite(value) or value <= 0:
                raise PositionInstructionError(
                    "marks must map symbols to positive finite prices"
                )
        return normalized

    def _base_notional(
        self, *, old_notional: float, sizing_equity: float
    ) -> tuple[float | None, V2ReasonCode | None]:
        if self.sizing.mode == "fixed_margin":
            assert self.sizing.margin_amount is not None
            return self.sizing.margin_amount * self.leverage, None
        if self.sizing.mode == "rolling_margin":
            assert self._state.rolling_margin is not None
            return self._state.rolling_margin * self.leverage, None
        if self.sizing.mode == "fixed_notional":
            assert self.sizing.notional_amount is not None
            return self.sizing.notional_amount, None
        if self.sizing.mode == "equity_margin_fraction":
            assert self.sizing.fraction is not None
            return self.sizing.fraction * sizing_equity * self.leverage, None
        if self.sizing.mode == "equity_fraction":
            assert self.sizing.fraction is not None
            return self.sizing.fraction * sizing_equity, None
        assert self.sizing.mode == "position_fraction"
        assert self.sizing.fraction is not None
        if abs(old_notional) > EPSILON:
            return self.sizing.fraction * abs(old_notional), None
        if self.sizing.zero_position_policy == "skip":
            return None, V2ReasonCode.ZERO_POSITION_SKIPPED
        if self.sizing.zero_position_policy == "error":
            raise PositionInstructionError(
                "position_fraction cannot size a zero position"
            )
        assert self.sizing.zero_position_policy == "bootstrap_fixed_notional"
        assert self.sizing.bootstrap_notional_amount is not None
        return self.sizing.bootstrap_notional_amount, None

    def _reverse_legs(
        self, *, old_notional: float, signed_base: float
    ) -> tuple[float, float]:
        if abs(old_notional) <= EPSILON or old_notional * signed_base > 0:
            return 0.0, signed_base
        assert self.sizing.reverse_policy is not None
        if self.sizing.reverse_policy == "flatten_only":
            return -old_notional, 0.0
        if self.sizing.reverse_policy == "flatten_then_open":
            return -old_notional, signed_base
        if abs(signed_base) <= abs(old_notional):
            return signed_base, 0.0
        return -old_notional, signed_base + old_notional

    def _process_one(
        self,
        selection: dict[str, object],
        *,
        decision_time: datetime,
        marks: dict[str, float],
        sizing_equity: float,
        batch_turnover: float,
    ) -> dict[str, object]:
        symbol = str(selection["symbol"])
        side = str(selection["side"])
        if symbol not in marks:
            raise PositionInstructionError(f"missing mark for selected symbol {symbol}")
        price = marks[symbol]
        old = self._state.positions.get(symbol)
        old_notional = 0.0 if old is None else old.quantity * price
        if side == "FLAT":
            base = abs(old_notional)
            early_reason = None
            close_leg, opening_leg = -old_notional, 0.0
        elif self.sizing.mode == "target_weight":
            target_weight = float(selection["target_weight"])
            target_notional = target_weight * sizing_equity
            base = abs(target_notional)
            early_reason = None
            if old_notional * target_notional < 0:
                close_leg, opening_leg = -old_notional, target_notional
            elif abs(target_notional) < abs(old_notional):
                close_leg, opening_leg = target_notional - old_notional, 0.0
            else:
                close_leg, opening_leg = 0.0, target_notional - old_notional
        else:
            base, early_reason = self._base_notional(
                old_notional=old_notional, sizing_equity=sizing_equity
            )
            if base is None:
                return self._instruction_row(
                    decision_time=decision_time,
                    selection=selection,
                    symbol=symbol,
                    side=side,
                    base_notional=0.0,
                    old_notional=old_notional,
                    requested_delta=0.0,
                    constrained_delta=0.0,
                    reason=early_reason or V2ReasonCode.ZERO_POSITION_SKIPPED,
                    flags=(),
                    fee_cost=0.0,
                    slippage_cost=0.0,
                    marks=marks,
                )
            signed_base = base if side == "LONG" else -base
            close_leg, opening_leg = self._reverse_legs(
                old_notional=old_notional, signed_base=signed_base
            )
        requested_delta = close_leg + opening_leg
        flags: list[V2ReasonCode] = []
        close_fill = self._limit_close_turnover(
            close_leg,
            sizing_equity=sizing_equity,
            batch_turnover=batch_turnover,
        )
        if abs(close_fill - close_leg) > EPSILON:
            flags.append(V2ReasonCode.SCALED_MAX_TURNOVER)
            opening_fill = 0.0
        else:
            opening_fill, opening_flags = self._limit_opening(
                symbol=symbol,
                old_notional=old_notional + close_fill,
                opening_leg=opening_leg,
                marks=marks,
                sizing_equity=sizing_equity,
                batch_turnover=batch_turnover + abs(close_fill),
                close_turnover=abs(close_fill),
            )
            flags.extend(opening_flags)
        constrained_delta = self._round_delta(
            symbol=symbol,
            price=price,
            close_delta=close_fill,
            opening_delta=opening_fill,
        )
        if (
            old is None
            and abs(constrained_delta) > EPSILON
            and len(self._state.positions) >= self.max_position_state_rows
        ):
            raise PositionStateBudgetExceeded(
                f"position state rows would exceed "
                f"max_position_state_rows={self.max_position_state_rows}"
            )
        fee_value, slip_value = self._execute(
            symbol=symbol,
            delta_notional=constrained_delta,
            price=price,
        )
        reason = flags[0] if flags else V2ReasonCode.ACCEPTED
        return self._instruction_row(
            decision_time=decision_time,
            selection=selection,
            symbol=symbol,
            side=side,
            base_notional=base,
            old_notional=old_notional,
            requested_delta=requested_delta,
            constrained_delta=constrained_delta,
            reason=reason,
            flags=tuple(flags),
            fee_cost=fee_value,
            slippage_cost=slip_value,
            marks=marks,
        )

    def _limit_close_turnover(
        self,
        close_leg: float,
        *,
        sizing_equity: float,
        batch_turnover: float,
    ) -> float:
        maximum = self.constraints.max_turnover
        if maximum is None or abs(close_leg) <= EPSILON:
            return close_leg
        remaining = max(0.0, maximum * sizing_equity - batch_turnover)
        return close_leg * min(1.0, remaining / abs(close_leg))

    def _limit_opening(
        self,
        *,
        symbol: str,
        old_notional: float,
        opening_leg: float,
        marks: dict[str, float],
        sizing_equity: float,
        batch_turnover: float,
        close_turnover: float,
    ) -> tuple[float, list[V2ReasonCode]]:
        if abs(opening_leg) <= EPSILON:
            return 0.0, []
        old_state = self._state.positions.get(symbol)
        same_direction_add = (
            old_state is not None
            and old_notional * opening_leg > 0
            and abs(old_notional) > EPSILON
        )
        maximum_adds = self.constraints.max_consecutive_adds
        if (
            same_direction_add
            and maximum_adds is not None
            and old_state.consecutive_adds >= maximum_adds
        ):
            return 0.0, [V2ReasonCode.REJECTED_MAX_CONSECUTIVE_ADDS]
        full_violations = self._constraint_violations(
            symbol=symbol,
            post_symbol_notional=old_notional + opening_leg,
            marks=marks,
            sizing_equity=sizing_equity,
            turnover=batch_turnover + abs(opening_leg),
            trade_cost=(close_turnover + abs(opening_leg)) * self._cost_rate,
        )
        if not full_violations:
            return opening_leg, []
        low = 0.0
        high = 1.0
        for _ in range(60):
            middle = (low + high) / 2.0
            candidate = opening_leg * middle
            violations = self._constraint_violations(
                symbol=symbol,
                post_symbol_notional=old_notional + candidate,
                marks=marks,
                sizing_equity=sizing_equity,
                turnover=batch_turnover + abs(candidate),
                trade_cost=(close_turnover + abs(candidate)) * self._cost_rate,
            )
            if violations:
                high = middle
            else:
                low = middle
        constrained = opening_leg * low
        if abs(constrained) <= EPSILON:
            constrained = 0.0
        return constrained, full_violations

    def _constraint_violations(
        self,
        *,
        symbol: str,
        post_symbol_notional: float,
        marks: dict[str, float],
        sizing_equity: float,
        turnover: float,
        trade_cost: float,
    ) -> list[V2ReasonCode]:
        notionals = self._notionals(marks)
        if abs(post_symbol_notional) <= EPSILON:
            notionals.pop(symbol, None)
        else:
            notionals[symbol] = post_symbol_notional
        gross = sum(abs(value) for value in notionals.values())
        unrealized = sum(
            state.quantity * (marks[name] - state.average_entry_price)
            for name, state in self._state.positions.items()
        )
        current_equity = self._state.cash_balance + unrealized
        net = sum(notionals.values())
        used_margin = gross / self.leverage
        violations: list[V2ReasonCode] = []
        if (
            self.constraints.max_symbol_notional is not None
            and abs(post_symbol_notional)
            > self.constraints.max_symbol_notional + EPSILON
        ):
            violations.append(V2ReasonCode.SCALED_MAX_SYMBOL_NOTIONAL)
        if (
            self.constraints.max_symbol_weight is not None
            and abs(post_symbol_notional) / sizing_equity
            > self.constraints.max_symbol_weight + EPSILON
        ):
            violations.append(V2ReasonCode.SCALED_MAX_SYMBOL_WEIGHT)
        if (
            self.constraints.max_gross_exposure is not None
            and gross / sizing_equity
            > self.constraints.max_gross_exposure + EPSILON
        ):
            violations.append(V2ReasonCode.SCALED_MAX_GROSS_EXPOSURE)
        if (
            self.constraints.max_net_exposure is not None
            and abs(net) / sizing_equity
            > self.constraints.max_net_exposure + EPSILON
        ):
            violations.append(V2ReasonCode.SCALED_MAX_NET_EXPOSURE)
        if (
            self.constraints.max_turnover is not None
            and turnover / sizing_equity
            > self.constraints.max_turnover + EPSILON
        ):
            violations.append(V2ReasonCode.SCALED_MAX_TURNOVER)
        if (
            used_margin + self.capital.reserved_cost_buffer
            > current_equity - trade_cost + EPSILON
        ):
            violations.append(V2ReasonCode.REJECTED_INSUFFICIENT_MARGIN)
        return [item for item in self._CONSTRAINT_ORDER if item in violations]

    def _round_delta(
        self,
        *,
        symbol: str,
        price: float,
        close_delta: float,
        opening_delta: float,
    ) -> float:
        step = self._quantity_steps.get(symbol)
        if step is None:
            return close_delta + opening_delta
        old = self._state.positions.get(symbol)
        close_quantity = close_delta / price
        if old is not None and abs(close_delta + old.quantity * price) <= EPSILON:
            close_quantity = -old.quantity
        elif abs(close_quantity) > EPSILON:
            close_quantity = (
                (1.0 if close_quantity > 0 else -1.0)
                * floor(abs(close_quantity) / step + EPSILON)
                * step
            )
        open_quantity = opening_delta / price
        rounded_open = (
            (1.0 if open_quantity > 0 else -1.0)
            * floor(abs(open_quantity) / step + EPSILON)
            * step
            if abs(open_quantity) > EPSILON
            else 0.0
        )
        return (close_quantity + rounded_open) * price

    def _execute(
        self, *, symbol: str, delta_notional: float, price: float
    ) -> tuple[float, float]:
        if abs(delta_notional) <= EPSILON:
            return 0.0, 0.0
        old = self._state.positions.get(symbol)
        old_quantity = 0.0 if old is None else old.quantity
        old_average = None if old is None else old.average_entry_price
        delta_quantity = delta_notional / price
        if old is not None and old_quantity * delta_quantity < 0 and isclose(
            delta_notional,
            -old_quantity * price,
            rel_tol=1e-12,
            abs_tol=EPSILON,
        ):
            delta_quantity = -old_quantity
        new_quantity = old_quantity + delta_quantity
        if (
            self.sizing.mode == "rolling_margin"
            and old is not None
            and old_quantity * new_quantity < -EPSILON
        ):
            raise PositionInstructionError("rolling_margin does not support atomic reversal")
        realized = 0.0
        if old is not None and old_quantity * delta_quantity < 0:
            closed_quantity = min(abs(old_quantity), abs(delta_quantity))
            realized = (
                closed_quantity
                * (price - old.average_entry_price)
                * (1.0 if old_quantity > 0 else -1.0)
            )
        fee_value = abs(delta_notional) * self._fee_rate
        slip_value = abs(delta_notional) * self._slippage_rate
        self._state.cash_balance += realized - fee_value - slip_value
        if self.sizing.mode == "rolling_margin":
            if old is None and abs(new_quantity) > EPSILON:
                self._state.rolling_active_margin = (
                    abs(new_quantity * price) / self.leverage
                )
                self._state.rolling_round_net_pnl = -fee_value - slip_value
                self._state.rolling_last_reset_reason = None
            elif old is not None:
                self._state.rolling_round_net_pnl += realized - fee_value - slip_value
                if abs(new_quantity) <= EPSILON:
                    self._settle_rolling_round()
        average = updated_average_entry(
            old_quantity, new_quantity, old_average, price
        )
        if average is None or abs(new_quantity) <= EPSILON:
            self._state.positions.pop(symbol, None)
            return fee_value, slip_value
        if old is None or old_quantity * new_quantity <= 0:
            adds = 1
        elif abs(new_quantity) > abs(old_quantity) + EPSILON:
            adds = old.consecutive_adds + 1
        else:
            adds = old.consecutive_adds
        self._state.positions[symbol] = PositionState(
            quantity=new_quantity,
            average_entry_price=average,
            consecutive_adds=adds,
        )
        return fee_value, slip_value

    def _settle_rolling_round(self) -> None:
        active = self._state.rolling_active_margin
        if active is None:
            raise PositionInstructionError("rolling close has no active margin")
        candidate = active + self._state.rolling_round_net_pnl
        assert self.sizing.rolling_min_margin is not None
        assert self.sizing.rolling_max_margin is not None
        assert self.sizing.rolling_reset_margin is not None
        reason = None
        if candidate < self.sizing.rolling_min_margin:
            reason = "below_min"
        elif candidate > self.sizing.rolling_max_margin:
            reason = "above_max"
        if reason is not None:
            candidate = self.sizing.rolling_reset_margin
            self._state.rolling_reset_count += 1
        if not isfinite(candidate) or candidate <= 0:
            raise PositionInstructionError("rolling settlement produced invalid margin")
        self._state.rolling_margin = candidate
        self._state.rolling_active_margin = None
        self._state.rolling_round_net_pnl = 0.0
        self._state.rolling_last_reset_reason = reason

    def _notionals(self, marks: Mapping[str, float]) -> dict[str, float]:
        return {
            symbol: state.quantity * marks[symbol]
            for symbol, state in self._state.positions.items()
        }

    def _account_snapshot(
        self,
        timestamp: datetime,
        marks: Mapping[str, float],
        *,
        turnover: float,
        fee_cost: float,
        slippage_cost: float,
    ) -> AccountSnapshot:
        notionals = self._notionals(marks)
        unrealized = sum(
            state.quantity * (marks[symbol] - state.average_entry_price)
            for symbol, state in self._state.positions.items()
        )
        equity = self._state.cash_balance + unrealized
        if not isfinite(equity) or equity <= 0:
            raise PositionInstructionError("equity became non-positive")
        gross = sum(abs(value) for value in notionals.values())
        net = sum(notionals.values())
        used = gross / self.leverage
        available = max(
            0.0, equity - used - self.capital.reserved_cost_buffer
        )
        return AccountSnapshot(
            timestamp=timestamp,
            cash_balance=self._state.cash_balance,
            unrealized_pnl=unrealized,
            equity=equity,
            used_margin=used,
            available_margin=available,
            gross_notional=gross,
            net_notional=net,
            turnover_notional=turnover,
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
            position_state_rows=len(self._state.positions),
        )

    def _position_snapshot(
        self, timestamp: datetime, marks: Mapping[str, float]
    ) -> pl.DataFrame:
        rows = []
        for symbol, state in sorted(self._state.positions.items()):
            mark = marks[symbol]
            signed_notional = state.quantity * mark
            rows.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "quantity": state.quantity,
                    "signed_notional": signed_notional,
                    "average_entry_price": state.average_entry_price,
                    "unrealized_pnl": state.quantity
                    * (mark - state.average_entry_price),
                    "used_margin": abs(signed_notional) / self.leverage,
                    "consecutive_adds": state.consecutive_adds,
                    "run_id": self.run_id,
                }
            )
        return _frame(rows, _POSITION_SNAPSHOT_SCHEMA)

    def _instruction_row(
        self,
        *,
        decision_time: datetime,
        selection: dict[str, object],
        symbol: str,
        side: str,
        base_notional: float,
        old_notional: float,
        requested_delta: float,
        constrained_delta: float,
        reason: V2ReasonCode,
        flags: tuple[V2ReasonCode, ...],
        fee_cost: float,
        slippage_cost: float,
        marks: Mapping[str, float],
    ) -> dict[str, object]:
        self._state.sequence += 1
        identity = {
            "version": self.instruction_version,
            "run_id": self.run_id,
            "sequence": self._state.sequence,
            "decision_time": decision_time.isoformat(),
            "symbol": symbol,
            "side": side,
        }
        post = self._notionals(marks).get(symbol, 0.0)
        account = self._account_snapshot(
            decision_time,
            marks,
            turnover=abs(constrained_delta),
            fee_cost=fee_cost,
            slippage_cost=slippage_cost,
        )
        rank_source = selection.get("rank_source_time")
        return {
            "instruction_id": f"instruction-{content_sha256(identity)[:24]}",
            "decision_time": decision_time,
            "rank_source_time": rank_source,
            "symbol": symbol,
            "side": side,
            "instruction_mode": self.sizing.mode,
            "sizing_base_notional": base_notional,
            "pretrade_symbol_notional": old_notional,
            "requested_delta_notional": requested_delta,
            "constrained_delta_notional": constrained_delta,
            "posttrade_symbol_notional": post,
            "requested_target_weight": selection.get("target_weight"),
            "source_event_id": None,
            "reason_code": reason.value,
            "constraint_flags": ";".join(dict.fromkeys(item.value for item in flags)),
            "priority": int(EventPriority.SCHEDULED_STRATEGY),
            "fee_cost": fee_cost,
            "slippage_cost": slippage_cost,
            "used_margin": account.used_margin,
            "available_margin": account.available_margin,
            "run_id": self.run_id,
        }
