"""Chronological multi-account Event runner for the frozen LLM benchmark.

The runner scans the market once and advances four isolated accounts through
the same UTC minute clock.  It deliberately lives outside the generic strategy
API: these one-shot submissions contain bespoke state machines whose defects
must be retained rather than normalized into a simpler portfolio model.
"""

from __future__ import annotations

import json
import os
import shutil
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite, log
from pathlib import Path
from statistics import median
from typing import Iterable, Mapping

import duckdb
import polars as pl

from bfbt.data.hashing import content_sha256, sha256_file
from bfbt.experiments.llm_benchmark import (
    CHATGPT,
    CLAUDE,
    GROK,
    KIMI,
    STRATEGIES,
    FrozenStrategy,
    FundingPoint,
    HourPoint,
    MarketView,
    StrategyDecision,
    decide,
)
from bfbt.metrics.summary import compute_run_metrics


UTC = timezone.utc
CORE_START = datetime(2025, 9, 1, tzinfo=UTC)
CORE_END = datetime(2026, 9, 1, tzinfo=UTC)
RAW_START = datetime(2025, 8, 1, tzinfo=UTC)
TAIL_END = datetime(2026, 9, 2, tzinfo=UTC)
INITIAL_EQUITY = 100_000.0
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
COST_RATE = FEE_RATE + SLIPPAGE_RATE
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
ENGINE_VERSION = "llmcs-event-v1"
UTC_MS = pl.Datetime("ms", "UTC")


TARGET_SCHEMA = {
    "signal_time": UTC_MS, "symbol": pl.String, "score": pl.Float64,
    "side": pl.String, "unconstrained_weight": pl.Float64,
    "target_weight": pl.Float64, "constraint_flags": pl.String,
    "portfolio_version": pl.String, "run_id": pl.String,
}
TRADE_SCHEMA = {
    "signal_time": UTC_MS, "fill_time": UTC_MS, "symbol": pl.String,
    "sequence": pl.Int64, "side": pl.String, "old_weight": pl.Float64,
    "target_weight": pl.Float64, "filled_weight": pl.Float64,
    "turnover": pl.Float64, "reference_price": pl.Float64,
    "fill_price": pl.Float64, "notional": pl.Float64, "status": pl.String,
    "constraint_flags": pl.String, "run_id": pl.String,
}
POSITION_SCHEMA = {
    "timestamp": UTC_MS, "symbol": pl.String, "quantity": pl.Float64,
    "signed_notional": pl.Float64, "target_weight": pl.Float64,
    "actual_weight": pl.Float64, "mark_price": pl.Float64,
    "unrealized_pnl": pl.Float64, "run_id": pl.String,
}
COST_SCHEMA = {
    "timestamp": UTC_MS, "symbol": pl.String, "fee_cost": pl.Float64,
    "slippage_cost": pl.Float64, "funding_cashflow": pl.Float64,
    "total_cost": pl.Float64, "run_id": pl.String,
}
RETURN_SCHEMA = {
    "timestamp": UTC_MS, "gross_price_return": pl.Float64,
    "fee_cost": pl.Float64, "slippage_cost": pl.Float64,
    "funding_return": pl.Float64, "net_return": pl.Float64,
    "equity": pl.Float64, "drawdown": pl.Float64,
    "gross_exposure": pl.Float64, "net_exposure": pl.Float64,
    "turnover": pl.Float64, "run_id": pl.String,
}
RANKING_SCHEMA = {
    "timestamp": UTC_MS, "rank_clock": pl.String, "symbol": pl.String,
    "factor_name": pl.String, "raw_score": pl.Float64,
    "ordinal_rank": pl.Int32, "percentile_rank": pl.Float64,
    "sample_count": pl.Int32, "factor_version": pl.String,
    "universe_version": pl.String, "run_id": pl.String,
}
INSTRUCTION_SCHEMA = {
    "instruction_id": pl.String, "decision_time": UTC_MS,
    "rank_source_time": UTC_MS, "symbol": pl.String, "side": pl.String,
    "instruction_mode": pl.String, "requested_delta_notional": pl.Float64,
    "constrained_delta_notional": pl.Float64,
    "requested_target_weight": pl.Float64, "source_event_id": pl.String,
    "reason_code": pl.String, "priority": pl.Int16, "run_id": pl.String,
}
RISK_SCHEMA = {
    "event_id": pl.String, "evaluation_time": UTC_MS,
    "trigger_time": UTC_MS, "symbol": pl.String, "event_type": pl.String,
    "direction": pl.String, "entry_price": pl.Float64,
    "trigger_level": pl.Float64, "observed_price": pl.Float64,
    "conflict_policy": pl.String, "action": pl.String,
    "fill_time": UTC_MS, "reason_code": pl.String, "run_id": pl.String,
}


def _frame(rows: list[dict[str, object]], schema: Mapping[str, pl.DataType]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    # Construct with the artifact schema instead of inferring from an initial
    # row sample.  Sparse audit columns can legitimately be null for hundreds
    # of ordinary instructions before the first risk-linked string appears.
    return pl.from_dicts(rows, schema=schema)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _dt(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


@dataclass
class PendingOrder:
    symbol: str
    target_quantity: float
    target_weight: float
    decision_time: datetime
    eligible_at: datetime
    expires_at: datetime | None
    reason: str
    priority: int
    event_id: str | None = None


@dataclass
class Account:
    strategy: FrozenStrategy
    run_id: str
    cash: float = INITIAL_EQUITY
    positions: dict[str, float] = field(default_factory=dict)
    average_entry: dict[str, float] = field(default_factory=dict)
    target_weights: dict[str, float] = field(default_factory=dict)
    pending: dict[str, PendingOrder] = field(default_factory=dict)
    marks: dict[str, float] = field(default_factory=dict)
    mark_times: dict[str, datetime] = field(default_factory=dict)
    peak_equity: float = INITIAL_EQUITY
    previous_equity: float = INITIAL_EQUITY
    breaker_on: bool = False
    risk_scale: float = 1.0
    cooldown_until: dict[str, datetime] = field(default_factory=dict)
    enforcer_release: datetime | None = None
    mandatory_exits: set[str] = field(default_factory=set)
    flatten_latched: bool = False
    flatten_completed_at: datetime | None = None
    insolvent: bool = False
    sequence: int = 0
    instruction_sequence: int = 0
    risk_sequence: int = 0
    latest_decision: StrategyDecision | None = None
    claude_universe_anchor: dict[str, float] = field(default_factory=dict)
    claude_position_anchor: dict[str, float] = field(default_factory=dict)
    claude_flash_threshold: dict[str, float] = field(default_factory=dict)
    targets: list[dict[str, object]] = field(default_factory=list)
    trades: list[dict[str, object]] = field(default_factory=list)
    positions_rows: list[dict[str, object]] = field(default_factory=list)
    costs: list[dict[str, object]] = field(default_factory=list)
    returns: list[dict[str, object]] = field(default_factory=list)
    rankings: list[dict[str, object]] = field(default_factory=list)
    instructions: list[dict[str, object]] = field(default_factory=list)
    risks: list[dict[str, object]] = field(default_factory=list)
    warnings: set[str] = field(default_factory=set)
    workspace: Path | None = None
    part_sequence: int = 0
    minute_fee: float = 0.0
    minute_slippage: float = 0.0
    minute_funding: float = 0.0
    minute_turnover: float = 0.0

    def equity(self, marks: Mapping[str, float] | None = None) -> float:
        prices = marks or self.marks
        return self.cash + sum(
            quantity * prices[symbol]
            for symbol, quantity in self.positions.items()
            if symbol in prices
        )

    def notionals(self) -> dict[str, float]:
        return {
            symbol: quantity * self.marks[symbol]
            for symbol, quantity in self.positions.items()
            if symbol in self.marks
        }

    def weights(self) -> dict[str, float]:
        equity = self.equity()
        if equity <= 0:
            return {}
        return {symbol: value / equity for symbol, value in self.notionals().items()}

    def _instruction_id(self, order: PendingOrder) -> str:
        self.instruction_sequence += 1
        return f"ins-{content_sha256([self.run_id, self.instruction_sequence, order.symbol, order.decision_time.isoformat(), order.reason])[:24]}"

    def queue(self, order: PendingOrder) -> None:
        existing = self.pending.get(order.symbol)
        if existing is not None and existing.priority < order.priority:
            return
        if (
            existing is not None
            and existing.priority == order.priority
            and existing.reason != "scheduled_rebalance"
            and order.reason != "terminal_liquidation"
        ):
            return
        if existing is not None and existing.event_id and existing.event_id != order.event_id:
            self._complete_unfilled_risk(existing.event_id, "superseded")
        self.pending[order.symbol] = order

    def _complete_unfilled_risk(self, event_id: str, outcome: str) -> None:
        for row in reversed(self.risks):
            if row["event_id"] == event_id:
                if row["fill_time"] is None:
                    row["action"] = outcome
                return

    def queue_targets(
        self,
        decision: StrategyDecision,
        *,
        next_marks: Mapping[str, float],
        exact_post_cost: bool = False,
        symbols_to_order: set[str] | None = None,
    ) -> None:
        equity = self.equity()
        union = set(self.positions) | set(decision.target_weights)
        weights = dict(decision.target_weights)
        target_equity = equity
        if exact_post_cost:
            old = {
                symbol: self.positions.get(symbol, 0.0) * self.marks.get(symbol, next_marks.get(symbol, 0.0))
                for symbol in union
            }
            low, high = 0.0, max(0.0, equity)
            for _ in range(80):
                middle = (low + high) / 2.0
                lhs = middle + COST_RATE * sum(
                    abs(weights.get(symbol, 0.0) * middle - old.get(symbol, 0.0))
                    for symbol in union
                )
                if lhs <= equity:
                    low = middle
                else:
                    high = middle
            target_equity = low
        for symbol in sorted(union):
            price = self.marks.get(symbol)
            if price is None or price <= 0:
                continue
            weight = weights.get(symbol, 0.0)
            quantity = weight * target_equity / price
            if symbols_to_order is not None and symbol not in symbols_to_order:
                continue
            if (
                self.strategy is GROK
                and weight != 0.0
                and abs((quantity - self.positions.get(symbol, 0.0)) * price) < 1.0
            ):
                continue
            order = PendingOrder(
                symbol=symbol,
                target_quantity=quantity,
                target_weight=weight,
                decision_time=decision.signal_time,
                eligible_at=decision.signal_time + MINUTE,
                expires_at=decision.signal_time + timedelta(minutes=61),
                reason="scheduled_rebalance",
                priority=30,
            )
            self.queue(order)
            self.targets.append({
                "signal_time": decision.signal_time, "symbol": symbol,
                "score": next((row.score for row in decision.scores if row.symbol == symbol), 0.0),
                "side": "LONG" if weight > 0 else "SHORT" if weight < 0 else "FLAT",
                "unconstrained_weight": weight, "target_weight": weight,
                "constraint_flags": "", "portfolio_version": self.strategy.factor_version,
                "run_id": self.run_id,
            })
        self.target_weights = weights

    def queue_claude_targets(self, decision: StrategyDecision) -> None:
        equity = self.equity()
        current = self.weights()
        targets = dict(decision.target_weights)
        union = set(self.positions) | set(targets)
        traded: set[str] = set()
        for symbol in union:
            target = targets.get(symbol, 0.0)
            existing = current.get(symbol, 0.0)
            if target == 0.0 and symbol in self.positions:
                traded.add(symbol)
            elif existing == 0.0 or existing * target < 0:
                traded.add(symbol)
            elif abs(existing - target) > 0.10 * abs(target):
                traded.add(symbol)
        for positive in (True, False):
            side = {symbol for symbol, value in targets.items() if (value > 0) == positive}
            gross_after = sum(
                abs(targets[symbol]) if symbol in traded else abs(current.get(symbol, 0.0))
                for symbol in side
            )
            if side and abs(gross_after - 0.5) > 0.01:
                traded.update(side)
        self.queue_targets(
            decision,
            next_marks=self.marks,
            symbols_to_order=traded,
        )

    def queue_flatten(self, now: datetime, reason: str, *, priority: int = 0) -> None:
        self.flatten_latched = True
        for symbol in sorted(self.positions):
            self.queue(PendingOrder(
                symbol=symbol, target_quantity=0.0, target_weight=0.0,
                decision_time=now, eligible_at=now, expires_at=None,
                reason=reason, priority=priority,
            ))

    def risk_event(
        self,
        now: datetime,
        *,
        symbol: str | None,
        event_type: str,
        trigger_level: float,
        observed: float,
        action: str,
        reason: str,
    ) -> str:
        self.risk_sequence += 1
        event_id = f"risk-{content_sha256([self.run_id, self.risk_sequence, now.isoformat(), symbol, reason])[:24]}"
        quantity = self.positions.get(symbol or "", 0.0)
        self.risks.append({
            "event_id": event_id, "evaluation_time": now, "trigger_time": now,
            "symbol": symbol, "event_type": event_type,
            "direction": "LONG" if quantity > 0 else "SHORT" if quantity < 0 else None,
            "entry_price": self.average_entry.get(symbol or ""),
            "trigger_level": trigger_level, "observed_price": observed,
            "conflict_policy": "completed_minute_close",
            "action": action, "fill_time": None, "reason_code": reason,
            "run_id": self.run_id,
        })
        return event_id

    def fill_due(self, now: datetime, opens: Mapping[str, float]) -> None:
        if self.strategy is CHATGPT:
            scheduled = [
                order for order in self.pending.values()
                if order.reason == "scheduled_rebalance" and order.eligible_at == now
            ]
            if scheduled:
                required = {order.symbol for order in scheduled}
                if not required.issubset(opens):
                    for symbol in required:
                        self.pending.pop(symbol, None)
                    self.warnings.add(f"atomic_basket_missing_open:{now.isoformat()}")
                    self.queue_flatten(now, "chatgpt_missing_basket_open", priority=1)
                else:
                    open_marks = dict(self.marks)
                    open_marks.update({symbol: float(opens[symbol]) for symbol in required})
                    equity = self.equity(open_marks)
                    old = {
                        symbol: self.positions.get(symbol, 0.0) * float(opens[symbol])
                        for symbol in required
                    }
                    low, high = 0.0, max(0.0, equity)
                    for _ in range(80):
                        middle = (low + high) / 2.0
                        lhs = middle + COST_RATE * sum(
                            abs(order.target_weight * middle - old[order.symbol])
                            for order in scheduled
                        )
                        if lhs <= equity:
                            low = middle
                        else:
                            high = middle
                    for order in scheduled:
                        order.target_quantity = order.target_weight * low / float(opens[order.symbol])
        for symbol in sorted(list(self.pending)):
            order = self.pending[symbol]
            if now < order.eligible_at:
                continue
            if order.expires_at is not None and now >= order.expires_at:
                del self.pending[symbol]
                if order.event_id:
                    self._complete_unfilled_risk(order.event_id, "expired_without_fill")
                self.warnings.add(f"order_timeout:{order.reason}:{symbol}:{now.isoformat()}")
                continue
            price = opens.get(symbol)
            if price is None or not isfinite(price) or price <= 0:
                continue
            old_quantity = self.positions.get(symbol, 0.0)
            delta = order.target_quantity - old_quantity
            old_equity = max(self.equity({**self.marks, symbol: price}), 1e-12)
            old_notional = old_quantity * price
            notional = abs(delta * price)
            fee = notional * FEE_RATE
            slippage = notional * SLIPPAGE_RATE
            self.cash -= delta * price + fee + slippage
            if abs(order.target_quantity) <= 1e-12:
                self.positions.pop(symbol, None)
                self.average_entry.pop(symbol, None)
                if order.reason in {
                    "chatgpt_mandatory_exit",
                    "chatgpt_reduction_repair",
                }:
                    self.mandatory_exits.discard(symbol)
            else:
                if old_quantity == 0 or old_quantity * order.target_quantity <= 0:
                    self.average_entry[symbol] = price
                elif abs(order.target_quantity) > abs(old_quantity):
                    added = abs(order.target_quantity - old_quantity)
                    self.average_entry[symbol] = (
                        abs(old_quantity) * self.average_entry.get(symbol, price) + added * price
                    ) / abs(order.target_quantity)
                self.positions[symbol] = order.target_quantity
            self.marks[symbol] = price
            self.mark_times[symbol] = now
            self.sequence += 1
            post_notional = self.positions.get(symbol, 0.0) * price
            instruction_id = self._instruction_id(order)
            self.instructions.append({
                "instruction_id": instruction_id, "decision_time": order.decision_time,
                "rank_source_time": order.decision_time if order.reason == "scheduled_rebalance" else None,
                "symbol": symbol,
                "side": "LONG" if order.target_quantity > 0 else "SHORT" if order.target_quantity < 0 else "FLAT",
                "instruction_mode": "target_quantity",
                "requested_delta_notional": delta * price,
                "constrained_delta_notional": delta * price,
                "requested_target_weight": order.target_weight,
                "source_event_id": order.event_id, "reason_code": order.reason,
                "priority": order.priority, "run_id": self.run_id,
            })
            self.trades.append({
                "signal_time": order.decision_time, "fill_time": now, "symbol": symbol,
                "sequence": self.sequence, "side": "BUY" if delta > 0 else "SELL",
                "old_weight": old_notional / old_equity,
                "target_weight": order.target_weight,
                "filled_weight": post_notional / old_equity,
                "turnover": notional / old_equity, "reference_price": price,
                "fill_price": price, "notional": notional, "status": "FILLED",
                "constraint_flags": order.reason, "run_id": self.run_id,
            })
            self.costs.append({
                "timestamp": now, "symbol": symbol, "fee_cost": fee,
                "slippage_cost": slippage, "funding_cashflow": 0.0,
                "total_cost": fee + slippage, "run_id": self.run_id,
            })
            self.minute_fee += fee
            self.minute_slippage += slippage
            self.minute_turnover += notional / old_equity
            equity = self.equity()
            self.positions_rows.append({
                "timestamp": now, "symbol": symbol,
                "quantity": self.positions.get(symbol, 0.0),
                "signed_notional": post_notional,
                "target_weight": order.target_weight,
                "actual_weight": post_notional / equity if equity else 0.0,
                "mark_price": price,
                "unrealized_pnl": self.positions.get(symbol, 0.0)
                * (price - self.average_entry.get(symbol, price)),
                "run_id": self.run_id,
            })
            if order.event_id:
                for row in reversed(self.risks):
                    if row["event_id"] == order.event_id:
                        row["fill_time"] = now
                        break
            if order.reason == "grok_exposure_enforcer":
                self.enforcer_release = now + HOUR
            del self.pending[symbol]
        if self.flatten_latched and not self.positions:
            self.flatten_latched = False
            self.flatten_completed_at = now

    def apply_funding(
        self,
        now: datetime,
        rows: Iterable[FundingPoint | tuple[str, float] | tuple[str, float, datetime]],
    ) -> None:
        for item in rows:
            if isinstance(item, FundingPoint):
                continue
            symbol, rate = item[:2]
            quantity = self.positions.get(symbol, 0.0)
            if quantity == 0:
                continue
            price = self.marks.get(symbol)
            if price is None or not isfinite(rate):
                raise RuntimeError(f"missing required funding valuation for held {symbol} at {now}")
            cashflow = -quantity * price * rate
            self.cash += cashflow
            self.minute_funding += cashflow
            self.costs.append({
                "timestamp": now, "symbol": symbol, "fee_cost": 0.0,
                "slippage_cost": 0.0, "funding_cashflow": cashflow,
                "total_cost": -cashflow, "run_id": self.run_id,
            })

    def record_minute(self, now: datetime) -> None:
        equity = self.equity()
        if not isfinite(equity):
            raise RuntimeError(f"non-finite equity for {self.strategy.strategy_id}")
        notionals = self.notionals()
        gross = sum(abs(value) for value in notionals.values())
        net = sum(notionals.values())
        base = max(abs(self.previous_equity), 1e-12)
        net_return = equity / self.previous_equity - 1.0 if self.previous_equity else 0.0
        fee_return = self.minute_fee / base
        slip_return = self.minute_slippage / base
        funding_return = self.minute_funding / base
        gross_price_return = net_return + fee_return + slip_return - funding_return
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = equity / self.peak_equity - 1.0 if self.peak_equity else 0.0
        self.returns.append({
            "timestamp": now, "gross_price_return": gross_price_return,
            "fee_cost": fee_return, "slippage_cost": slip_return,
            "funding_return": funding_return, "net_return": net_return,
            "equity": equity, "drawdown": drawdown,
            "gross_exposure": gross / equity if equity else float("inf"),
            "net_exposure": net / equity if equity else float("inf"),
            "turnover": self.minute_turnover, "run_id": self.run_id,
        })
        self.previous_equity = equity
        self.minute_fee = self.minute_slippage = self.minute_funding = self.minute_turnover = 0.0

    def flush(self) -> None:
        if self.workspace is None:
            return
        self.part_sequence += 1
        tables = (
            ("targets", self.targets, TARGET_SCHEMA),
            ("trades", self.trades, TRADE_SCHEMA),
            ("positions", self.positions_rows, POSITION_SCHEMA),
            ("costs", self.costs, COST_SCHEMA),
            ("returns", self.returns, RETURN_SCHEMA),
            ("rankings", self.rankings, RANKING_SCHEMA),
            ("position_instructions", self.instructions, INSTRUCTION_SCHEMA),
        )
        for name, rows, schema in tables:
            if not rows:
                continue
            directory = self.workspace / name
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"part-{self.part_sequence:04d}.parquet"
            _frame(rows, schema).write_parquet(path, compression="zstd")
            rows.clear()
        completed_risks = [
            row for row in self.risks
            if row["fill_time"] is not None
            or row["action"] in {"hold", "superseded", "expired_without_fill"}
        ]
        if completed_risks:
            directory = self.workspace / "risk_events"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"part-{self.part_sequence:04d}.parquet"
            _frame(completed_risks, RISK_SCHEMA).write_parquet(path, compression="zstd")
            completed_ids = {str(row["event_id"]) for row in completed_risks}
            self.risks[:] = [row for row in self.risks if str(row["event_id"]) not in completed_ids]

    def checkpoint(self) -> dict[str, object]:
        if any((self.targets, self.trades, self.positions_rows, self.costs, self.returns, self.rankings, self.instructions)):
            raise RuntimeError("account rows must be flushed before checkpoint")
        return {
            "strategy_id": self.strategy.strategy_id,
            "run_id": self.run_id,
            "cash": self.cash,
            "positions": self.positions,
            "average_entry": self.average_entry,
            "target_weights": self.target_weights,
            "pending": {
                symbol: {
                    "symbol": order.symbol,
                    "target_quantity": order.target_quantity,
                    "target_weight": order.target_weight,
                    "decision_time": _dt(order.decision_time),
                    "eligible_at": _dt(order.eligible_at),
                    "expires_at": _dt(order.expires_at),
                    "reason": order.reason,
                    "priority": order.priority,
                    "event_id": order.event_id,
                }
                for symbol, order in self.pending.items()
            },
            "marks": self.marks,
            "mark_times": {symbol: _dt(value) for symbol, value in self.mark_times.items()},
            "peak_equity": self.peak_equity,
            "previous_equity": self.previous_equity,
            "breaker_on": self.breaker_on,
            "risk_scale": self.risk_scale,
            "cooldown_until": {symbol: _dt(value) for symbol, value in self.cooldown_until.items()},
            "enforcer_release": _dt(self.enforcer_release),
            "mandatory_exits": sorted(self.mandatory_exits),
            "flatten_latched": self.flatten_latched,
            "flatten_completed_at": _dt(self.flatten_completed_at),
            "insolvent": self.insolvent,
            "sequence": self.sequence,
            "instruction_sequence": self.instruction_sequence,
            "risk_sequence": self.risk_sequence,
            "pending_risks": [
                {
                    key: _dt(value) if isinstance(value, datetime) else value
                    for key, value in row.items()
                }
                for row in self.risks
            ],
            "claude_universe_anchor": self.claude_universe_anchor,
            "claude_position_anchor": self.claude_position_anchor,
            "claude_flash_threshold": self.claude_flash_threshold,
            "warnings": sorted(self.warnings),
            "part_sequence": self.part_sequence,
        }

    def restore(self, payload: Mapping[str, object]) -> None:
        if payload["strategy_id"] != self.strategy.strategy_id or payload["run_id"] != self.run_id:
            raise RuntimeError("benchmark checkpoint identity mismatch")
        self.cash = float(payload["cash"])
        self.positions = {str(k): float(v) for k, v in dict(payload["positions"]).items()}
        self.average_entry = {str(k): float(v) for k, v in dict(payload["average_entry"]).items()}
        self.target_weights = {str(k): float(v) for k, v in dict(payload["target_weights"]).items()}
        self.pending = {
            str(symbol): PendingOrder(
                symbol=str(item["symbol"]),
                target_quantity=float(item["target_quantity"]),
                target_weight=float(item["target_weight"]),
                decision_time=_parse_dt(item["decision_time"]),  # type: ignore[arg-type]
                eligible_at=_parse_dt(item["eligible_at"]),  # type: ignore[arg-type]
                expires_at=_parse_dt(item.get("expires_at")),  # type: ignore[arg-type]
                reason=str(item["reason"]), priority=int(item["priority"]),
                event_id=None if item.get("event_id") is None else str(item["event_id"]),
            )
            for symbol, raw in dict(payload["pending"]).items()
            for item in [dict(raw)]
        }
        self.marks = {str(k): float(v) for k, v in dict(payload["marks"]).items()}
        self.mark_times = {str(k): _parse_dt(str(v)) for k, v in dict(payload["mark_times"]).items()}  # type: ignore[assignment]
        self.peak_equity = float(payload["peak_equity"])
        self.previous_equity = float(payload["previous_equity"])
        self.breaker_on = bool(payload["breaker_on"])
        self.risk_scale = float(payload["risk_scale"])
        self.cooldown_until = {str(k): _parse_dt(str(v)) for k, v in dict(payload["cooldown_until"]).items()}  # type: ignore[assignment]
        self.enforcer_release = _parse_dt(payload.get("enforcer_release"))  # type: ignore[arg-type]
        self.mandatory_exits = set(str(value) for value in payload["mandatory_exits"])
        self.flatten_latched = bool(payload["flatten_latched"])
        self.flatten_completed_at = _parse_dt(payload.get("flatten_completed_at"))  # type: ignore[arg-type]
        self.insolvent = bool(payload["insolvent"])
        self.sequence = int(payload["sequence"])
        self.instruction_sequence = int(payload["instruction_sequence"])
        self.risk_sequence = int(payload["risk_sequence"])
        self.risks = [
            {
                key: _parse_dt(value) if key in {"evaluation_time", "trigger_time", "fill_time"} else value
                for key, value in dict(raw).items()
            }
            for raw in payload.get("pending_risks", [])
        ]
        self.claude_universe_anchor = {str(k): float(v) for k, v in dict(payload["claude_universe_anchor"]).items()}
        self.claude_position_anchor = {str(k): float(v) for k, v in dict(payload["claude_position_anchor"]).items()}
        self.claude_flash_threshold = {str(k): float(v) for k, v in dict(payload["claude_flash_threshold"]).items()}
        self.warnings = set(str(value) for value in payload["warnings"])
        self.part_sequence = int(payload["part_sequence"])
        if self.workspace is not None:
            for path in self.workspace.glob("*/*.parquet"):
                sequence = int(path.stem.split("-")[-1])
                if sequence > self.part_sequence:
                    path.unlink()


@dataclass
class MarketState:
    hourly: dict[str, deque[HourPoint]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=721)))
    funding: dict[str, deque[FundingPoint]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=256)))
    first_seen: dict[str, datetime] = field(default_factory=dict)
    latest_close: dict[str, float] = field(default_factory=dict)
    latest_close_time: dict[str, datetime] = field(default_factory=dict)

    def add_hour(self, symbol: str, point: HourPoint) -> None:
        self.hourly[symbol].append(point)
        self.first_seen.setdefault(symbol, point.available_at - HOUR)
        self.latest_close[symbol] = point.close
        self.latest_close_time[symbol] = point.available_at

    def add_funding(self, symbol: str, point: FundingPoint) -> None:
        self.funding[symbol].append(point)

    def view(self, now: datetime) -> MarketView:
        qv: dict[str, float] = {}
        candidates: list[str] = []
        threshold = now - timedelta(days=30)
        cutoff = now - timedelta(hours=24)
        for symbol, points in self.hourly.items():
            if self.first_seen.get(symbol, now) > threshold:
                continue
            recent = [point for point in points if cutoff < point.available_at <= now]
            minute_count = sum(point.minute_count for point in recent)
            volume = sum(point.quote_volume for point in recent)
            if minute_count < 1426 or not isfinite(volume):
                continue
            qv[symbol] = volume
            candidates.append(symbol)
        eligible = tuple(sorted(candidates, key=lambda symbol: (-qv[symbol], symbol))[:100])
        return MarketView(
            now=now, eligible=eligible,
            hourly={symbol: tuple(self.hourly[symbol]) for symbol in eligible},
            funding={symbol: tuple(self.funding[symbol]) for symbol in eligible},
            quote_volume_24h=qv,
        )


def _run_id(
    strategy: FrozenStrategy,
    snapshot_hash: str,
    source_commit: str,
    submission_identity: Mapping[str, str],
) -> str:
    return f"evt-{content_sha256([ENGINE_VERSION, strategy.strategy_id, strategy.factor_version, snapshot_hash, source_commit, submission_identity, CORE_START.isoformat(), CORE_END.isoformat(), FEE_RATE, SLIPPAGE_RATE])[:24]}"


def materialize_months(normalized: Path, derived: Path, status: Path) -> None:
    derived.mkdir(parents=True, exist_ok=True)
    temporary_root = derived / "duckdb-tmp"
    temporary_root.mkdir(exist_ok=True)
    for cursor in _months(RAW_START.date(), TAIL_END.date()):
        target = derived / f"bars-{cursor.year:04d}-{cursor.month:02d}.parquet"
        if target.is_file():
            continue
        source = normalized / f"bars/schema=v1/dataset_version=*/interval=1m/year={cursor.year:04d}/month={cursor.month:02d}/*.parquet"
        temporary = target.with_suffix(".parquet.tmp")
        connection = duckdb.connect()
        try:
            connection.execute("SET memory_limit='2GB'")
            connection.execute(f"SET temp_directory='{temporary_root.as_posix()}'")
            connection.execute("SET threads=2")
            query = f"""
                COPY (
                    SELECT open_time, close_time, symbol, open, close, quote_volume
                    FROM read_parquet('{source.as_posix()}', hive_partitioning=false)
                    ORDER BY open_time, symbol
                ) TO '{temporary.as_posix()}'
                (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 122880)
            """
            connection.execute(query)
        finally:
            connection.close()
        os.replace(temporary, target)
        _atomic_json(status, {"status": "running", "stage": "materialize", "month": cursor.isoformat(), "updated_at": datetime.now(UTC).isoformat()})


def _months(start: date, end: date) -> Iterable[date]:
    cursor = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while cursor <= final:
        yield cursor
        cursor = date(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)


def _days(start: date, end: date) -> Iterable[date]:
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(days=1)


def _daily_frame(derived: Path, day: date) -> pl.DataFrame:
    path = derived / f"bars-{day.year:04d}-{day.month:02d}.parquet"
    start = datetime.combine(day, time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    return (
        pl.scan_parquet(path, hive_partitioning=False)
        .filter((pl.col("open_time") >= start) & (pl.col("open_time") < end))
        .collect(engine="streaming")
        .sort(["open_time", "symbol"])
    )


def _hourly_points(frame: pl.DataFrame) -> dict[datetime, list[tuple[str, HourPoint]]]:
    if not frame.height:
        return {}
    rows = (
        frame.with_columns(pl.col("open_time").dt.truncate("1h").alias("hour"))
        .group_by(["hour", "symbol"])
        .agg(
            pl.col("close").sort_by("open_time").last().alias("close"),
            pl.len().alias("minute_count"),
            pl.col("quote_volume").sum().alias("quote_volume"),
        )
        .sort(["hour", "symbol"])
    )
    result: dict[datetime, list[tuple[str, HourPoint]]] = defaultdict(list)
    for row in rows.to_dicts():
        available = row["hour"] + HOUR
        result[available].append((str(row["symbol"]), HourPoint(
            available_at=available, close=float(row["close"]),
            minute_count=int(row["minute_count"]), quote_volume=float(row["quote_volume"]),
        )))
    return result


def _minute_frames(frame: pl.DataFrame) -> dict[datetime, dict[str, dict[str, object]]]:
    result: dict[datetime, dict[str, dict[str, object]]] = {}
    for group in frame.partition_by("open_time", maintain_order=True):
        opened = group.item(0, "open_time")
        result[opened] = {str(row["symbol"]): row for row in group.to_dicts()}
    return result


def _funding_events(path: Path) -> dict[datetime, list[tuple[str, float, datetime]]]:
    frame = pl.scan_parquet(path, hive_partitioning=True).select(
        "funding_time", "symbol", "funding_rate"
    ).collect(engine="streaming").sort(["funding_time", "symbol"])
    result: dict[datetime, list[tuple[str, float, datetime]]] = defaultdict(list)
    for row in frame.to_dicts():
        observed = row["funding_time"]
        bucket = observed.replace(second=0, microsecond=0)
        if observed != bucket:
            bucket += MINUTE
        result[bucket].append((str(row["symbol"]), float(row["funding_rate"]), observed))
    return result


def _record_rankings(account: Account, decision: StrategyDecision) -> None:
    denominator = max(1, len(decision.scores) - 1)
    for row in decision.scores:
        account.rankings.append({
            "timestamp": decision.signal_time, "rank_clock": "factor",
            "symbol": row.symbol, "factor_name": account.strategy.display_name,
            "raw_score": row.score, "ordinal_rank": row.ordinal_rank,
            "percentile_rank": 1.0 - (row.ordinal_rank - 1) / denominator,
            "sample_count": row.sample_count,
            "factor_version": account.strategy.factor_version,
            "universe_version": "llmcs-hourly-top100-v1", "run_id": account.run_id,
        })


def _queue_reduction(
    account: Account,
    now: datetime,
    targets: Mapping[str, float],
    reason: str,
    event_id: str,
    *,
    expires_at: datetime | None = None,
) -> None:
    equity = account.equity()
    for symbol, quantity in list(account.positions.items()):
        price = account.marks.get(symbol)
        if price is None:
            continue
        weight = targets.get(symbol, 0.0)
        target_quantity = weight * equity / price
        if abs(target_quantity) > abs(quantity) + 1e-12:
            target_quantity = quantity
        account.queue(PendingOrder(
            symbol=symbol, target_quantity=target_quantity, target_weight=weight,
            decision_time=now, eligible_at=now, expires_at=expires_at,
            reason=reason, priority=5, event_id=event_id,
        ))


def _minute_risk(account: Account, now: datetime, completed: Mapping[str, dict[str, object]], view: MarketView | None) -> None:
    for symbol, row in completed.items():
        close = float(row["close"])
        if close > 0 and isfinite(close):
            account.marks[symbol] = close
            account.mark_times[symbol] = now
    equity = account.equity()
    if equity <= 0 and not account.insolvent:
        account.insolvent = True
        event = account.risk_event(now, symbol=None, event_type="insolvency", trigger_level=0.0, observed=equity, action="close", reason="non_positive_equity")
        account.queue_flatten(now, "non_positive_equity", priority=0)
        for order in account.pending.values():
            if order.reason == "non_positive_equity":
                order.event_id = event
        return
    weights = account.weights()
    if account.strategy is CHATGPT and account.positions:
        stale = [symbol for symbol in account.positions if now - account.mark_times.get(symbol, datetime.min.replace(tzinfo=UTC)) >= timedelta(minutes=5)]
        gross = sum(abs(value) for value in weights.values())
        maximum = max((abs(value) for value in weights.values()), default=0.0)
        if stale:
            event = account.risk_event(now, symbol=stale[0], event_type="stale_price", trigger_level=300.0, observed=(now - account.mark_times[stale[0]]).total_seconds(), action="close", reason="stale_price_flatten")
            account.queue_flatten(now, "stale_price_flatten", priority=1)
            for order in account.pending.values():
                if order.reason == "stale_price_flatten": order.event_id = event
        elif gross > 1.0 + 1e-12 or maximum > 0.10 + 1e-12:
            # Submitted repair target: balanced 0.49 sides and <=0.098 each.
            longs = {s: abs(v) for s, v in account.notionals().items() if v > 0 and s not in account.mandatory_exits}
            shorts = {s: abs(v) for s, v in account.notionals().items() if v < 0 and s not in account.mandatory_exits}
            event = account.risk_event(now, symbol=None, event_type="exposure_repair", trigger_level=1.0, observed=max(gross, maximum / 0.10), action="reduce", reason="chatgpt_reduction_repair")
            if not longs or not shorts:
                account.queue_flatten(now, "chatgpt_empty_side", priority=2)
            else:
                long_total, short_total = sum(longs.values()), sum(shorts.values())
                max_share = max(max(value / long_total for value in longs.values()), max(value / short_total for value in shorts.values()))
                gamma = min(0.49, 0.098 / max_share)
                gross_notional = sum(abs(value) for value in account.notionals().values())
                budget = equity - COST_RATE * gross_notional
                if budget <= 0:
                    account.insolvent = True
                    account.queue_flatten(now, "chatgpt_repair_insolvency", priority=0)
                else:
                    side_notional = min(
                        long_total, short_total,
                        gamma * budget / (1.0 - 2.0 * COST_RATE * gamma),
                    )
                    for symbol, quantity in list(account.positions.items()):
                        price = account.marks.get(symbol)
                        if price is None:
                            continue
                        if symbol in account.mandatory_exits:
                            target_quantity = 0.0
                        elif quantity > 0:
                            target_quantity = quantity * side_notional / long_total
                        else:
                            target_quantity = quantity * side_notional / short_total
                        account.queue(PendingOrder(
                            symbol=symbol, target_quantity=target_quantity,
                            target_weight=(target_quantity * price / equity),
                            decision_time=now, eligible_at=now, expires_at=None,
                            reason="chatgpt_reduction_repair", priority=5,
                            event_id=event,
                        ))
    elif account.strategy is KIMI:
        account.peak_equity = max(account.peak_equity, equity)
        drawdown = 1.0 - equity / account.peak_equity
        if not account.breaker_on and drawdown > 0.15:
            account.breaker_on, account.risk_scale = True, 0.5
            event = account.risk_event(now, symbol=None, event_type="drawdown_breaker", trigger_level=0.15, observed=drawdown, action="reduce", reason="kimi_drawdown_on")
            targets = {symbol: weight * 0.5 for symbol, weight in account.target_weights.items()}
            _queue_reduction(
                account, now, targets, "kimi_drawdown_on", event,
                expires_at=now + timedelta(minutes=60),
            )
        elif account.breaker_on and drawdown < 0.10:
            account.breaker_on, account.risk_scale = False, 1.0
            account.risk_event(now, symbol=None, event_type="drawdown_breaker_reset", trigger_level=0.10, observed=drawdown, action="hold", reason="kimi_drawdown_off")
    elif account.strategy is GROK and account.positions:
        if any(symbol not in completed for symbol in account.positions):
            return
        if account.enforcer_release is None or now >= account.enforcer_release:
            long_gross = sum(max(0.0, value) for value in weights.values())
            short_gross = sum(max(0.0, -value) for value in weights.values())
            gross = long_gross + short_gross
            maximum = max((abs(value) for value in weights.values()), default=0.0)
            if long_gross > 0.5 or short_gross > 0.5 or gross > 1.0 or maximum > 0.10:
                projected = dict(weights)
                for symbol, value in list(projected.items()):
                    if abs(value) > 0.10: projected[symbol] = 0.09 if value > 0 else -0.09
                pg_long = sum(max(0.0, value) for value in projected.values())
                pg_short = sum(max(0.0, -value) for value in projected.values())
                if pg_long > 0.5:
                    projected = {s: v * 0.48 / pg_long if v > 0 else v for s, v in projected.items()}
                if pg_short > 0.5:
                    projected = {s: v * 0.48 / pg_short if v < 0 else v for s, v in projected.items()}
                pg = sum(abs(value) for value in projected.values())
                if pg > 1.0: projected = {s: v * 0.95 / pg for s, v in projected.items()}
                event = account.risk_event(now, symbol=None, event_type="exposure_enforcer", trigger_level=1.0, observed=max(gross, maximum / 0.10), action="reduce", reason="grok_exposure_enforcer")
                _queue_reduction(account, now, projected, "grok_exposure_enforcer", event)
    elif account.strategy is CLAUDE and account.positions and view is not None:
        moves = []
        for symbol in view.eligible:
            if symbol not in completed:
                continue
            anchor = account.claude_universe_anchor.get(symbol)
            close = account.marks.get(symbol)
            if anchor and close and anchor > 0 and close > 0:
                moves.append(log(close / anchor))
        if len(moves) >= 10:
            center = median(moves)
            for symbol, quantity in list(account.positions.items()):
                if symbol not in completed:
                    continue
                if symbol in account.pending and account.pending[symbol].priority <= 5:
                    continue
                anchor = account.claude_position_anchor.get(symbol)
                close = account.marks.get(symbol)
                threshold = account.claude_flash_threshold.get(symbol)
                if not anchor or not close or threshold is None:
                    continue
                excess = log(close / anchor) - center
                triggered = (quantity < 0 and excess >= threshold) or (quantity > 0 and excess <= -threshold)
                if triggered:
                    account.cooldown_until[symbol] = now + timedelta(hours=48)
                    event = account.risk_event(now, symbol=symbol, event_type="market_relative_flash", trigger_level=threshold, observed=abs(excess), action="close", reason="claude_flash_exit")
                    account.queue(PendingOrder(symbol=symbol, target_quantity=0.0, target_weight=0.0, decision_time=now, eligible_at=now, expires_at=None, reason="claude_flash_exit", priority=2, event_id=event))


def _hourly_decisions(accounts: Iterable[Account], now: datetime, view: MarketView) -> None:
    for account in accounts:
        if account.insolvent or now >= CORE_END:
            continue
        scheduled = now.hour in account.strategy.decision_hours
        if account.strategy is CHATGPT:
            decision = decide(account.strategy, view, account.positions)
            _record_rankings(account, decision)
            if len(decision.scores) < 40 and account.positions:
                account.flatten_latched = True
                event = account.risk_event(
                    now, symbol=None, event_type="insufficient_universe",
                    trigger_level=40.0, observed=float(len(decision.scores)),
                    action="close", reason="chatgpt_insufficient_universe",
                )
                for symbol in account.positions:
                    account.queue(PendingOrder(
                        symbol=symbol, target_quantity=0.0, target_weight=0.0,
                        decision_time=now, eligible_at=now + MINUTE,
                        expires_at=None, reason="chatgpt_insufficient_universe",
                        priority=2, event_id=event,
                    ))
            scored = {row.symbol for row in decision.scores}
            lost = set(account.positions) - scored
            if lost:
                account.mandatory_exits.update(lost)
                for symbol in lost:
                    event = account.risk_event(now, symbol=symbol, event_type="eligibility_loss", trigger_level=1.0, observed=0.0, action="reduce", reason="chatgpt_mandatory_exit")
                    account.queue(PendingOrder(symbol=symbol, target_quantity=0.0, target_weight=0.0, decision_time=now, eligible_at=now + MINUTE, expires_at=None, reason="chatgpt_mandatory_exit", priority=5, event_id=event))
            if not scheduled:
                continue
            if account.flatten_completed_at is not None and now <= account.flatten_completed_at:
                continue
        elif account.strategy is GROK and not scheduled:
            account.pending = {
                symbol: order for symbol, order in account.pending.items()
                if order.priority < 30
            }
            dropped = set(account.positions) - set(view.eligible)
            if dropped:
                flatten = len(dropped) >= 4
                survivors = set(account.positions) - dropped
                if survivors:
                    signs = {account.positions[symbol] > 0 for symbol in survivors}
                    flatten = flatten or len(signs) < 2
                targets = set(account.positions) if flatten else dropped
                for symbol in targets:
                    account.queue(PendingOrder(symbol=symbol, target_quantity=0.0, target_weight=0.0, decision_time=now, eligible_at=now + MINUTE, expires_at=now + timedelta(minutes=61), reason="grok_hourly_ineligibility", priority=10))
            continue
        elif not scheduled:
            continue
        decision = decide(
            account.strategy, view, account.positions,
            risk_scale=account.risk_scale,
            cooldown_until=account.cooldown_until,
        )
        if account.strategy is CLAUDE and now == CORE_END - HOUR:
            scored = {row.symbol for row in decision.scores}
            current = account.weights()
            decision = StrategyDecision(
                strategy_id=decision.strategy_id,
                signal_time=decision.signal_time,
                scores=decision.scores,
                target_weights={
                    symbol: current[symbol]
                    for symbol in account.positions
                    if symbol in scored and symbol in current
                },
                metadata={**decision.metadata, "final_decision_exits_only": True},
            )
        account.latest_decision = decision
        if account.strategy is not CHATGPT:
            _record_rankings(account, decision)
        if account.strategy is CLAUDE:
            extras = {row.symbol: row.extras for row in decision.scores}
            for symbol, quantity in account.positions.items():
                value = extras.get(symbol, {}).get("ex24")
                if value is not None and isfinite(value) and ((quantity > 0 and value <= -0.22) or (quantity < 0 and value >= 0.22)):
                    account.cooldown_until[symbol] = now + timedelta(hours=48)
                    event = account.risk_event(
                        now, symbol=symbol,
                        event_type="market_relative_velocity",
                        trigger_level=0.22, observed=abs(float(value)),
                        action="close", reason="claude_velocity_exit",
                    )
                    account.queue(PendingOrder(
                        symbol=symbol, target_quantity=0.0, target_weight=0.0,
                        decision_time=now, eligible_at=now + MINUTE,
                        expires_at=None, reason="claude_velocity_exit",
                        priority=3, event_id=event,
                    ))
            account.claude_universe_anchor = {symbol: account.marks[symbol] for symbol in view.eligible if symbol in account.marks}
            account.claude_position_anchor = {symbol: account.marks[symbol] for symbol in decision.target_weights if symbol in account.marks}
            account.claude_flash_threshold = {
                symbol: max(0.10, min(0.25, 6.0 * float(extras[symbol]["sigma"])))
                for symbol in decision.target_weights if symbol in extras
            }
        if account.strategy is GROK:
            account.enforcer_release = None
            account.pending = {
                symbol: order for symbol, order in account.pending.items()
                if order.reason in {"terminal_liquidation", "non_positive_equity"}
            }
        else:
            account.pending = {
                symbol: order for symbol, order in account.pending.items()
                if order.priority < 30
            }
        if account.strategy is CHATGPT and any(
            order.priority < 30 for order in account.pending.values()
        ):
            continue
        if account.strategy is CLAUDE:
            account.queue_claude_targets(decision)
        else:
            account.queue_targets(
                decision,
                next_marks=account.marks,
                exact_post_cost=account.strategy is CHATGPT,
            )


def _publish(account: Account, root: Path, metadata: Mapping[str, object]) -> Path:
    final = root / account.run_id
    if final.exists():
        return final
    staging = root / f".{account.run_id}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    account.flush()
    artifact_specs = {
        "targets.parquet": ("targets", TARGET_SCHEMA),
        "trades.parquet": ("trades", TRADE_SCHEMA),
        "positions.parquet": ("positions", POSITION_SCHEMA),
        "costs.parquet": ("costs", COST_SCHEMA),
        "returns.parquet": ("returns", RETURN_SCHEMA),
        "rankings.parquet": ("rankings", RANKING_SCHEMA),
        "position_instructions.parquet": ("position_instructions", INSTRUCTION_SCHEMA),
        "risk_events.parquet": ("risk_events", RISK_SCHEMA),
    }
    hashes: dict[str, str] = {}
    rows: dict[str, int] = {}
    artifacts: dict[str, pl.LazyFrame] = {}
    for name, (directory_name, schema) in artifact_specs.items():
        parts = sorted((account.workspace / directory_name).glob("*.parquet")) if account.workspace else []
        frame = (
            pl.scan_parquet(parts, hive_partitioning=False)
            if parts else _frame([], schema).lazy()
        )
        path = staging / name
        frame.sink_parquet(path, compression="zstd")
        artifacts[name] = pl.scan_parquet(path, hive_partitioning=False)
        hashes[name] = sha256_file(path)
        rows[name] = int(artifacts[name].select(pl.len()).collect(engine="streaming").item())
    metrics = compute_run_metrics(artifacts["returns.parquet"], base_interval="1m")
    (staging / "metrics.json").write_text(metrics.to_json(), encoding="utf-8")
    hashes["metrics.json"] = sha256_file(staging / "metrics.json")
    manifest = {
        "schema_version": "llm-benchmark-event-run/v1",
        "run_id": account.run_id,
        "strategy_id": account.strategy.strategy_id,
        "strategy_name": account.strategy.display_name,
        "factor_version": account.strategy.factor_version,
        "engine_version": ENGINE_VERSION,
        "core_interval": [CORE_START.isoformat(), CORE_END.isoformat()],
        "initial_equity": INITIAL_EQUITY,
        "fee_rate": FEE_RATE, "slippage_rate": SLIPPAGE_RATE,
        "metadata": dict(metadata), "warnings": sorted(account.warnings),
        "artifacts": {name: {"sha256": hashes[name], "rows": rows.get(name)} for name in sorted(hashes)},
    }
    _atomic_json(staging / "manifest.json", manifest)
    os.replace(staging, final)
    return final


def _write_checkpoint(
    path: Path,
    *,
    next_day: date,
    accounts: Iterable[Account],
    previous_minute: Mapping[str, Mapping[str, object]],
) -> None:
    previous = {
        symbol: {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for symbol, row in previous_minute.items()
    }
    _atomic_json(path, {
        "schema_version": "llm-benchmark-checkpoint/v1",
        "next_day": next_day.isoformat(),
        "accounts": {account.strategy.strategy_id: account.checkpoint() for account in accounts},
        "previous_minute": previous,
        "updated_at": datetime.now(UTC).isoformat(),
    })


def _restore_checkpoint(
    path: Path,
    accounts: Iterable[Account],
) -> tuple[date | None, dict[str, dict[str, object]]]:
    if not path.is_file():
        return None, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    by_id = {account.strategy.strategy_id: account for account in accounts}
    for strategy_id, state in payload["accounts"].items():
        by_id[strategy_id].restore(state)
    previous: dict[str, dict[str, object]] = {}
    for symbol, row in payload["previous_minute"].items():
        previous[symbol] = {
            key: datetime.fromisoformat(value)
            if key in {"open_time", "close_time"} and isinstance(value, str)
            else value
            for key, value in row.items()
        }
    return date.fromisoformat(payload["next_day"]), previous


def run_benchmark(
    *,
    experiment_root: Path,
    source_commit: str,
    snapshot_hash: str,
    submission_identities: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    data_root = experiment_root / "data"
    normalized = data_root / "normalized"
    derived = data_root / "event-feed"
    jobs = experiment_root / "jobs"
    status = jobs / "run_benchmark.status.json"
    checkpoint_path = jobs / "run_benchmark.checkpoint.json"
    runs = experiment_root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    _atomic_json(status, {"status": "running", "stage": "materialize", "updated_at": datetime.now(UTC).isoformat()})
    materialize_months(normalized, derived, status)
    funding_path = normalized / "funding/schema=v1/dataset_version=*/year=*/month=*/*.parquet"
    funding_events = _funding_events(funding_path)
    market = MarketState()
    workspace_root = jobs / "llm_benchmark_workspaces"
    accounts = [
        Account(
            strategy=item,
            run_id=_run_id(
                item, snapshot_hash, source_commit,
                submission_identities[item.strategy_id],
            ),
            workspace=workspace_root / item.strategy_id,
        )
        for item in STRATEGIES
    ]
    for account in accounts:
        if account.workspace is not None:
            account.workspace.mkdir(parents=True, exist_ok=True)
    resume_day, restored_previous = _restore_checkpoint(checkpoint_path, accounts)
    if resume_day is None:
        for account in accounts:
            if account.workspace is not None and account.workspace.exists():
                shutil.rmtree(account.workspace)
                account.workspace.mkdir(parents=True)
    pending_hours: dict[datetime, list[tuple[str, HourPoint]]] = defaultdict(list)
    previous_minute: dict[str, dict[str, object]] = restored_previous
    current_view: MarketView | None = None
    processed = 0
    terminal_complete = False
    for day in _days(RAW_START.date(), TAIL_END.date()):
        frame = _daily_frame(derived, day)
        for available, points in _hourly_points(frame).items():
            pending_hours[available].extend(points)
        minutes = _minute_frames(frame) if day >= CORE_START.date() else {}
        day_start = datetime.combine(day, time(), tzinfo=UTC)
        rebuilding = resume_day is not None and day < resume_day
        for minute_index in range(1440):
            now = day_start + minute_index * MINUTE
            for symbol, point in pending_hours.pop(now, []):
                market.add_hour(symbol, point)
            funding = funding_events.get(now, [])
            for symbol, rate, settlement_time in funding:
                market.add_funding(symbol, FundingPoint(settlement_time, rate))
            if rebuilding:
                continue
            if now < CORE_START:
                continue
            current = minutes.get(now, {})
            if now.minute == 0:
                current_view = market.view(now)
            for account in accounts:
                _minute_risk(account, now, previous_minute, current_view)
                account.apply_funding(now, funding)
                if now >= CORE_END:
                    account.queue_flatten(now, "terminal_liquidation", priority=0)
                account.fill_due(now, {symbol: float(row["open"]) for symbol, row in current.items()})
            if now.minute == 0 and now < CORE_END and current_view is not None:
                for account in accounts:
                    for symbol in set(account.positions) | set(current_view.eligible):
                        if symbol in market.latest_close:
                            account.marks[symbol] = market.latest_close[symbol]
                            account.mark_times[symbol] = market.latest_close_time[symbol]
                _hourly_decisions(accounts, now, current_view)
            for account in accounts:
                account.record_minute(now)
            previous_minute = current
            if now >= CORE_END and all(not account.positions and not account.pending for account in accounts):
                terminal_complete = True
                break
        if terminal_complete:
            processed += 1
            for account in accounts:
                account.flush()
            break
        processed += 1
        if not rebuilding and processed % 7 == 0:
            for account in accounts:
                account.flush()
            _write_checkpoint(
                checkpoint_path,
                next_day=day + timedelta(days=1),
                accounts=accounts,
                previous_minute=previous_minute,
            )
        _atomic_json(status, {
            "status": "running", "stage": "event", "processed_days": processed,
            "last_day": day.isoformat(),
            "runs": {account.strategy.strategy_id: account.run_id for account in accounts},
            "updated_at": datetime.now(UTC).isoformat(),
        })
    if any(account.positions or account.pending for account in accounts):
        unclosed = {
            account.strategy.strategy_id: {
                "positions": sorted(account.positions), "orders": sorted(account.pending),
            }
            for account in accounts if account.positions or account.pending
        }
        raise RuntimeError(f"terminal execution tail ended with open state: {unclosed}")
    outputs = {}
    metadata = {
        "snapshot_sha256": snapshot_hash,
        "source_commit": source_commit,
        "submission_identities": {
            key: dict(value) for key, value in submission_identities.items()
        },
    }
    for account in accounts:
        outputs[account.strategy.strategy_id] = str(_publish(account, runs, metadata))
    result = {
        "status": "succeeded", "stage": "complete", "runs": outputs,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _atomic_json(status, result)
    return result
