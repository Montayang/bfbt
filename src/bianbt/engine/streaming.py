"""Stateful bounded-memory ledger chunks equivalent to the A08 engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite

import polars as pl

from bianbt.config.backtest import BacktestConfig
from bianbt.data.hashing import content_sha256
from bianbt.engine.costs import fee_cost, fee_rate, slippage_cost, slippage_rate
from bianbt.engine.execution import (
    adverse_fill_price,
    fill_time,
    limit_turnover,
    updated_average_entry,
)
from bianbt.engine.funding import funding_cashflow
from bianbt.engine.vectorized import (
    COST_SCHEMA,
    EPSILON,
    POSITION_SCHEMA,
    RETURN_SCHEMA,
    TARGET_SCHEMA,
    TRADE_SCHEMA,
    UTC_MS,
    BacktestError,
    _explicit,
    _market_rows,
    _require,
)

STREAMING_ENGINE_VERSION = "a10-streaming-v1"


def _frame(
    rows: list[dict[str, object]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows).select(list(schema)).cast(schema)


@dataclass
class LedgerState:
    quantities: dict[str, float] = field(default_factory=dict)
    averages: dict[str, float] = field(default_factory=dict)
    requested_weights: dict[str, float] = field(default_factory=dict)
    previous_marks: dict[str, float] = field(default_factory=dict)
    equity: float = 1.0
    peak: float = 1.0
    sequence: int = 0
    warnings: set[str] = field(default_factory=set)
    last_open_time: datetime | None = None


@dataclass(frozen=True)
class LedgerChunk:
    targets: pl.DataFrame
    trades: pl.DataFrame
    positions: pl.DataFrame
    costs: pl.DataFrame
    returns: pl.DataFrame

    @property
    def row_counts(self) -> dict[str, int]:
        return {
            "targets": self.targets.height,
            "trades": self.trades.height,
            "positions": self.positions.height,
            "costs": self.costs.height,
            "returns": self.returns.height,
        }


class StreamingLedger:
    """Process ordered market chunks while retaining only portfolio state."""

    def __init__(
        self,
        *,
        config: BacktestConfig,
        base_interval: str,
        portfolio_version: str,
        bars_dataset_version: str,
        mark_dataset_version: str | None,
        funding_dataset_version: str | None,
        initial_equity: float = 1.0,
        excluded_funding_symbols: frozenset[str] = frozenset(),
        initial_warnings: frozenset[str] = frozenset(),
    ) -> None:
        self.config = config
        self.base_interval = base_interval
        self.portfolio_version = _explicit(
            "portfolio_version", portfolio_version
        )
        self.bars_dataset_version = _explicit(
            "bars_dataset_version", bars_dataset_version
        )
        if not isfinite(initial_equity) or initial_equity <= 0:
            raise BacktestError("initial_equity must be positive and finite")
        if config.schedule.signal_delay_bars < 1:
            raise BacktestError("next_bar_open requires signal_delay_bars >= 1")
        if config.execution.partial_fill:
            raise BacktestError("A10 does not support partial fills")
        if config.risk.enforce_liquidation:
            raise BacktestError("A10 does not implement liquidation")
        if config.valuation.price == "mark_close":
            self.mark_dataset_version = _explicit(
                "mark_dataset_version", mark_dataset_version
            )
        else:
            self.mark_dataset_version = self.bars_dataset_version
        if config.execution.funding.enabled:
            self.funding_dataset_version = _explicit(
                "funding_dataset_version", funding_dataset_version
            )
        else:
            self.funding_dataset_version = "disabled"
        self.excluded_funding_symbols = excluded_funding_symbols
        identity = {
            "engine": STREAMING_ENGINE_VERSION,
            "config": config.model_dump(mode="json"),
            "portfolio_version": self.portfolio_version,
            "bars_dataset_version": self.bars_dataset_version,
            "mark_dataset_version": self.mark_dataset_version,
            "funding_dataset_version": self.funding_dataset_version,
            "base_interval": base_interval,
            "initial_equity": initial_equity,
        }
        self.run_id = f"a10-{content_sha256(identity)[:24]}"
        self.state = LedgerState(equity=initial_equity, peak=initial_equity)
        for warning in sorted(initial_warnings):
            self.state.warnings.add(warning)
        for symbol in sorted(excluded_funding_symbols):
            self.state.warnings.add(f"funding_exclude_symbol:{symbol}")
        self._slippage_rate = slippage_rate(config.execution.slippage)
        fee_rate(config.execution.fee)

    def _targets(self, targets: pl.LazyFrame) -> list[dict[str, object]]:
        _require(
            targets,
            {
                "signal_time",
                "symbol",
                "score",
                "side",
                "unconstrained_weight",
                "target_weight",
                "constraint_flags",
                "portfolio_version",
            },
            "target",
        )
        rows = (
            targets.filter(
                pl.col("portfolio_version") == self.portfolio_version
            )
            .with_columns(pl.col("signal_time").cast(UTC_MS))
            .sort(["signal_time", "symbol"])
            .collect(engine="streaming")
            .to_dicts()
        )
        for row in rows:
            symbol = str(row["symbol"])
            if symbol in self.excluded_funding_symbols:
                row["target_weight"] = 0.0
                flags = str(row["constraint_flags"])
                row["constraint_flags"] = ";".join(
                    value for value in (flags, "FUNDING_MISSING") if value
                )
            row["run_id"] = self.run_id
        return rows

    def _funding(self, funding: pl.LazyFrame | None) -> list[dict[str, object]]:
        if not self.config.execution.funding.enabled:
            return []
        if funding is None:
            if self.config.execution.funding.missing_policy == "error":
                raise BacktestError("enabled funding with error policy requires data")
            policy = self.config.execution.funding.missing_policy
            self.state.warnings.add(f"funding_{policy}:no_dataset")
            return []
        _require(
            funding,
            {"funding_time", "symbol", "funding_rate", "dataset_version"},
            "funding",
        )
        return (
            funding.filter(
                pl.col("dataset_version") == self.funding_dataset_version
            )
            .with_columns(pl.col("funding_time").cast(UTC_MS))
            .sort(["funding_time", "symbol"])
            .collect(engine="streaming")
            .to_dicts()
        )

    def process(
        self,
        targets: pl.LazyFrame,
        trade_bars: pl.LazyFrame,
        mark_bars: pl.LazyFrame | None,
        funding: pl.LazyFrame | None,
    ) -> LedgerChunk:
        target_rows = self._targets(targets)
        trade_rows = _market_rows(
            trade_bars,
            dataset_version=self.bars_dataset_version,
            base_interval=self.base_interval,
            name="trade bars",
        )
        if self.config.valuation.price == "mark_close":
            if mark_bars is None:
                raise BacktestError("mark_close valuation requires mark_bars")
            valuation_rows = _market_rows(
                mark_bars,
                dataset_version=self.mark_dataset_version,
                base_interval=self.base_interval,
                name="mark bars",
            )
        else:
            valuation_rows = trade_rows
        funding_rows = self._funding(funding)
        trade_by_open: dict[datetime, dict[str, dict[str, object]]] = {}
        for row in trade_rows:
            trade_by_open.setdefault(row["open_time"], {})[
                str(row["symbol"])
            ] = row
        if self.state.last_open_time is not None and any(
            opened_at <= self.state.last_open_time
            for opened_at in trade_by_open
        ):
            raise BacktestError("market chunks overlap or are out of order")

        value_by_open: dict[datetime, dict[str, dict[str, object]]] = {}
        for row in valuation_rows:
            value_by_open.setdefault(row["open_time"], {})[
                str(row["symbol"])
            ] = row
        fill_targets: dict[datetime, dict[str, dict[str, object]]] = {}
        for row in target_rows:
            signal = row["signal_time"]
            assert isinstance(signal, datetime)
            filled_at = fill_time(
                signal,
                signal_delay_bars=self.config.schedule.signal_delay_bars,
                base_interval=self.base_interval,
            )
            symbol = str(row["symbol"])
            if symbol in fill_targets.setdefault(filled_at, {}):
                raise BacktestError("duplicate target signal_time/symbol")
            fill_targets[filled_at][symbol] = row
        unknown_fills = set(fill_targets) - set(trade_by_open)
        if unknown_fills:
            raise BacktestError(
                f"target fill times have no trade bar: {sorted(unknown_fills)!r}"
            )
        funding_by_time: dict[datetime, dict[str, dict[str, object]]] = {}
        for row in funding_rows:
            event = row["funding_time"]
            assert isinstance(event, datetime)
            symbol = str(row["symbol"])
            if symbol in funding_by_time.setdefault(event, {}):
                raise BacktestError("duplicate funding_time/symbol")
            funding_by_time[event][symbol] = row

        state = self.state
        trades_out: list[dict[str, object]] = []
        positions_out: list[dict[str, object]] = []
        costs_out: list[dict[str, object]] = []
        returns_out: list[dict[str, object]] = []
        for opened_at in sorted(trade_by_open):
            if state.last_open_time is not None and opened_at <= state.last_open_time:
                raise BacktestError("market chunks overlap or are out of order")
            trade_market = trade_by_open[opened_at]
            value_market = value_by_open.get(opened_at, {})
            close_times = {row["close_time"] for row in trade_market.values()}
            if len(close_times) != 1:
                raise BacktestError("trade bars at one open_time must share close_time")
            closed_at = next(iter(close_times))
            assert isinstance(closed_at, datetime)
            valuation_close_times = {
                row["close_time"] for row in value_market.values()
            }
            if valuation_close_times and valuation_close_times != {closed_at}:
                raise BacktestError("valuation and trade bars must share close_time")
            start_equity = state.equity
            gap_pnl = 0.0
            for symbol, quantity in state.quantities.items():
                if abs(quantity) <= EPSILON:
                    continue
                valuation = value_market.get(symbol)
                if valuation is None or symbol not in state.previous_marks:
                    raise BacktestError(
                        f"missing valuation bar for held symbol {symbol}"
                    )
                gap_pnl += quantity * (
                    float(valuation["open"]) - state.previous_marks[symbol]
                )
            pretrade_equity = start_equity + gap_pnl
            if pretrade_equity <= 0:
                raise BacktestError("equity became non-positive before execution")
            symbol_costs: dict[str, dict[str, float]] = {}
            turnover_notional = 0.0
            execution_basis_pnl = 0.0
            target_batch = fill_targets.get(opened_at)
            if target_batch is not None:
                requested = {
                    symbol: float(row["target_weight"])
                    for symbol, row in target_batch.items()
                }
                active = set(state.quantities) | set(requested)
                old_weights = {}
                for symbol in active:
                    reference = trade_market.get(symbol)
                    if reference is None:
                        raise BacktestError(
                            f"missing trade bar for target/position symbol {symbol}"
                        )
                    old_weights[symbol] = (
                        state.quantities.get(symbol, 0.0)
                        * float(reference["open"])
                        / pretrade_equity
                    )
                limited, scale, _ = limit_turnover(
                    old_weights, requested, self.config.portfolio.max_turnover
                )
                if sum(abs(value) for value in limited.values()) > (
                    self.config.risk.leverage + 1e-9
                ):
                    raise BacktestError(
                        "requested gross exposure exceeds risk.leverage"
                    )
                signal_times = {
                    row["signal_time"] for row in target_batch.values()
                }
                if len(signal_times) != 1:
                    raise BacktestError(
                        "one fill batch cannot contain different signal times"
                    )
                signal_time = next(iter(signal_times))
                for symbol in sorted(active):
                    reference_row = trade_market[symbol]
                    if not bool(reference_row["is_complete"]):
                        raise BacktestError(f"incomplete fill bar for {symbol}")
                    reference_price = float(reference_row["open"])
                    old_quantity = state.quantities.get(symbol, 0.0)
                    requested_weight = requested.get(symbol, 0.0)
                    filled_weight = limited[symbol]
                    new_quantity = (
                        filled_weight * pretrade_equity / reference_price
                    )
                    quantity_delta = new_quantity - old_quantity
                    notional = abs(quantity_delta * reference_price)
                    valuation_row = value_market.get(symbol)
                    if valuation_row is None:
                        raise BacktestError(
                            f"missing valuation bar for traded symbol {symbol}"
                        )
                    execution_basis_pnl += quantity_delta * (
                        float(valuation_row["open"]) - reference_price
                    )
                    requested_row = target_batch.get(symbol)
                    flags = (
                        str(requested_row["constraint_flags"])
                        if requested_row is not None
                        else ""
                    )
                    if scale < 1.0 - EPSILON:
                        flags = ";".join(
                            value for value in (flags, "MAX_TURNOVER") if value
                        )
                    state.requested_weights[symbol] = filled_weight
                    if notional <= EPSILON:
                        state.quantities[symbol] = new_quantity
                        continue
                    fee_value = fee_cost(notional, self.config.execution.fee)
                    slip_value = slippage_cost(
                        notional, self.config.execution.slippage
                    )
                    values = symbol_costs.setdefault(
                        symbol, {"fee": 0.0, "slippage": 0.0, "funding": 0.0}
                    )
                    values["fee"] += fee_value
                    values["slippage"] += slip_value
                    turnover_notional += notional
                    state.sequence += 1
                    trades_out.append(
                        {
                            "signal_time": signal_time,
                            "fill_time": opened_at,
                            "symbol": symbol,
                            "sequence": state.sequence,
                            "side": "BUY" if quantity_delta > 0 else "SELL",
                            "old_weight": old_weights.get(symbol, 0.0),
                            "target_weight": requested_weight,
                            "filled_weight": filled_weight,
                            "turnover": notional / pretrade_equity,
                            "reference_price": reference_price,
                            "fill_price": adverse_fill_price(
                                reference_price, quantity_delta, self._slippage_rate
                            ),
                            "notional": notional,
                            "status": "FILLED",
                            "constraint_flags": flags,
                            "run_id": self.run_id,
                        }
                    )
                    average = updated_average_entry(
                        old_quantity,
                        new_quantity,
                        state.averages.get(symbol),
                        reference_price,
                    )
                    state.quantities[symbol] = new_quantity
                    if average is None:
                        state.averages.pop(symbol, None)
                    else:
                        state.averages[symbol] = average
                state.quantities = {
                    symbol: value
                    for symbol, value in state.quantities.items()
                    if abs(value) > EPSILON
                }
                state.requested_weights = {
                    symbol: value
                    for symbol, value in state.requested_weights.items()
                    if abs(value) > EPSILON
                }

            intrabar_pnl = execution_basis_pnl
            for symbol, quantity in state.quantities.items():
                valuation = value_market.get(symbol)
                if valuation is None:
                    raise BacktestError(
                        f"missing valuation bar for held symbol {symbol}"
                    )
                if not bool(valuation["is_complete"]):
                    raise BacktestError(f"incomplete valuation bar for {symbol}")
                intrabar_pnl += quantity * (
                    float(valuation["close"]) - float(valuation["open"])
                )
            funding_total = 0.0
            if self.config.execution.funding.enabled:
                event_times = [
                    event
                    for event in funding_by_time
                    if opened_at < event <= closed_at
                ]
                for event in sorted(event_times):
                    records = funding_by_time[event]
                    for symbol, quantity in state.quantities.items():
                        record = records.get(symbol)
                        if record is None:
                            policy = self.config.execution.funding.missing_policy
                            if policy == "error":
                                raise BacktestError(
                                    f"missing funding record for {symbol} at {event}"
                                )
                            state.warnings.add(
                                f"funding_{policy}:{event.isoformat()}:{symbol}"
                            )
                            continue
                        valuation = value_market[symbol]
                        price = record.get("mark_price")
                        funding_price = (
                            float(price)
                            if price is not None
                            and isfinite(float(price))
                            and float(price) > 0
                            else float(valuation["close"])
                        )
                        cash = funding_cashflow(
                            quantity,
                            funding_price,
                            float(record["funding_rate"]),
                        )
                        funding_total += cash
                        symbol_costs.setdefault(
                            symbol,
                            {"fee": 0.0, "slippage": 0.0, "funding": 0.0},
                        )["funding"] += cash
            fee_total = sum(value["fee"] for value in symbol_costs.values())
            slippage_total = sum(
                value["slippage"] for value in symbol_costs.values()
            )
            gross_pnl = gap_pnl + intrabar_pnl
            gross_return = gross_pnl / start_equity
            fee_return = fee_total / start_equity
            slippage_return = slippage_total / start_equity
            funding_return = funding_total / start_equity
            net_return = (
                gross_return - fee_return - slippage_return + funding_return
            )
            state.equity = start_equity * (1.0 + net_return)
            if not isfinite(state.equity) or state.equity <= 0:
                raise BacktestError("equity became non-positive or non-finite")
            state.peak = max(state.peak, state.equity)
            gross_notional = 0.0
            net_notional = 0.0
            for symbol, quantity in sorted(state.quantities.items()):
                valuation = value_market[symbol]
                mark_price = float(valuation["close"])
                signed_notional = quantity * mark_price
                gross_notional += abs(signed_notional)
                net_notional += signed_notional
                average = state.averages.get(symbol, mark_price)
                positions_out.append(
                    {
                        "timestamp": closed_at,
                        "symbol": symbol,
                        "quantity": quantity,
                        "signed_notional": signed_notional,
                        "target_weight": state.requested_weights.get(symbol, 0.0),
                        "actual_weight": signed_notional / state.equity,
                        "mark_price": mark_price,
                        "unrealized_pnl": quantity * (mark_price - average),
                        "run_id": self.run_id,
                    }
                )
            for symbol, values in sorted(symbol_costs.items()):
                costs_out.append(
                    {
                        "timestamp": closed_at,
                        "symbol": symbol,
                        "fee_cost": values["fee"] / start_equity,
                        "slippage_cost": values["slippage"] / start_equity,
                        "funding_cashflow": values["funding"] / start_equity,
                        "total_cost": (
                            values["fee"] + values["slippage"] - values["funding"]
                        )
                        / start_equity,
                        "run_id": self.run_id,
                    }
                )
            returns_out.append(
                {
                    "timestamp": closed_at,
                    "gross_price_return": gross_return,
                    "fee_cost": fee_return,
                    "slippage_cost": slippage_return,
                    "funding_return": funding_return,
                    "net_return": net_return,
                    "equity": state.equity,
                    "drawdown": state.equity / state.peak - 1.0,
                    "gross_exposure": gross_notional / state.equity,
                    "net_exposure": net_notional / state.equity,
                    "turnover": turnover_notional / start_equity,
                    "run_id": self.run_id,
                }
            )
            state.previous_marks = {
                symbol: float(row["close"])
                for symbol, row in value_market.items()
            }
            state.last_open_time = opened_at
        return LedgerChunk(
            targets=_frame(target_rows, TARGET_SCHEMA),
            trades=_frame(trades_out, TRADE_SCHEMA),
            positions=_frame(positions_out, POSITION_SCHEMA),
            costs=_frame(costs_out, COST_SCHEMA),
            returns=_frame(returns_out, RETURN_SCHEMA),
        )
