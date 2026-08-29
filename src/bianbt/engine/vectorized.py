"""Deterministic stateful ledger over columnar market inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping

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

ENGINE_CODE_VERSION = "a08-engine-v1"
EPSILON = 1e-12
UTC_MS = pl.Datetime("ms", "UTC")


class BacktestError(ValueError):
    """A run cannot satisfy deterministic execution or accounting semantics."""


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    result_hash: str
    targets: pl.LazyFrame
    trades: pl.LazyFrame
    positions: pl.LazyFrame
    costs: pl.LazyFrame
    returns: pl.LazyFrame
    warnings: tuple[str, ...]
    diagnostics: Mapping[str, object] | None = None
    presorted: bool = False
    execution_mode: str = "in_memory"


TARGET_SCHEMA = {
    "signal_time": UTC_MS,
    "symbol": pl.String,
    "score": pl.Float64,
    "side": pl.String,
    "unconstrained_weight": pl.Float64,
    "target_weight": pl.Float64,
    "constraint_flags": pl.String,
    "portfolio_version": pl.String,
    "run_id": pl.String,
}
TRADE_SCHEMA = {
    "signal_time": UTC_MS,
    "fill_time": UTC_MS,
    "symbol": pl.String,
    "sequence": pl.Int64,
    "side": pl.String,
    "old_weight": pl.Float64,
    "target_weight": pl.Float64,
    "filled_weight": pl.Float64,
    "turnover": pl.Float64,
    "reference_price": pl.Float64,
    "fill_price": pl.Float64,
    "notional": pl.Float64,
    "status": pl.String,
    "constraint_flags": pl.String,
    "run_id": pl.String,
}
POSITION_SCHEMA = {
    "timestamp": UTC_MS,
    "symbol": pl.String,
    "quantity": pl.Float64,
    "signed_notional": pl.Float64,
    "target_weight": pl.Float64,
    "actual_weight": pl.Float64,
    "mark_price": pl.Float64,
    "unrealized_pnl": pl.Float64,
    "run_id": pl.String,
}
COST_SCHEMA = {
    "timestamp": UTC_MS,
    "symbol": pl.String,
    "fee_cost": pl.Float64,
    "slippage_cost": pl.Float64,
    "funding_cashflow": pl.Float64,
    "total_cost": pl.Float64,
    "run_id": pl.String,
}
RETURN_SCHEMA = {
    "timestamp": UTC_MS,
    "gross_price_return": pl.Float64,
    "fee_cost": pl.Float64,
    "slippage_cost": pl.Float64,
    "funding_return": pl.Float64,
    "net_return": pl.Float64,
    "equity": pl.Float64,
    "drawdown": pl.Float64,
    "gross_exposure": pl.Float64,
    "net_exposure": pl.Float64,
    "turnover": pl.Float64,
    "run_id": pl.String,
}


def _frame(rows: list[dict[str, object]], schema: dict[str, pl.DataType]) -> pl.LazyFrame:
    if not rows:
        return pl.DataFrame(schema=schema).lazy()
    return pl.DataFrame(rows).select(list(schema)).cast(schema).lazy()


def _require(frame: pl.LazyFrame, columns: set[str], name: str) -> None:
    missing = columns - set(frame.collect_schema().names())
    if missing:
        raise BacktestError(f"{name} input is missing columns: {sorted(missing)}")


def _explicit(name: str, value: str | None) -> str:
    if not value or value.lower() == "latest":
        raise BacktestError(f"{name} must be explicit")
    return value


def _market_rows(
    frame: pl.LazyFrame,
    *,
    dataset_version: str,
    base_interval: str,
    name: str,
) -> list[dict[str, object]]:
    _require(
        frame,
        {
            "open_time",
            "close_time",
            "symbol",
            "interval",
            "open",
            "close",
            "is_complete",
            "dataset_version",
        },
        name,
    )
    rows = (
        frame.filter(
            (pl.col("dataset_version") == dataset_version)
            & (pl.col("interval") == base_interval)
        )
        .with_columns(
            pl.col("open_time").cast(UTC_MS),
            pl.col("close_time").cast(UTC_MS),
        )
        .sort(["open_time", "symbol"])
        .collect()
        .to_dicts()
    )
    if not rows:
        raise BacktestError(f"{name} has no rows for the requested version/interval")
    keys: set[tuple[object, object]] = set()
    for row in rows:
        key = (row["open_time"], row["symbol"])
        if key in keys:
            raise BacktestError(f"{name} contains duplicate open_time/symbol rows")
        keys.add(key)
        for field in ("open", "close"):
            value = float(row[field])
            if not isfinite(value) or value <= 0:
                raise BacktestError(f"{name} contains invalid {field} price")
    return rows


def _json_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def run_vectorized_backtest(
    targets: pl.LazyFrame,
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
    initial_equity: float = 1.0,
) -> BacktestResult:
    """Run a correctness-first ledger; A10 adds bounded chunk orchestration."""

    portfolio_version = _explicit("portfolio_version", portfolio_version)
    bars_dataset_version = _explicit(
        "bars_dataset_version", bars_dataset_version
    )
    if not isfinite(initial_equity) or initial_equity <= 0:
        raise BacktestError("initial_equity must be positive and finite")
    if config.schedule.signal_delay_bars < 1:
        raise BacktestError("next_bar_open requires signal_delay_bars >= 1")
    if config.execution.partial_fill:
        raise BacktestError("A08 does not support partial fills")
    if config.risk.enforce_liquidation:
        raise BacktestError("A08 does not implement liquidation")
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
    target_rows = (
        targets.filter(pl.col("portfolio_version") == portfolio_version)
        .with_columns(pl.col("signal_time").cast(UTC_MS))
        .sort(["signal_time", "symbol"])
        .collect()
        .to_dicts()
    )
    if not target_rows:
        raise BacktestError("target input has no rows for portfolio_version")
    trade_rows = _market_rows(
        trade_bars,
        dataset_version=bars_dataset_version,
        base_interval=base_interval,
        name="trade bars",
    )
    if config.valuation.price == "mark_close":
        if mark_bars is None:
            raise BacktestError("mark_close valuation requires mark_bars")
        mark_version = _explicit("mark_dataset_version", mark_dataset_version)
        valuation_rows = _market_rows(
            mark_bars,
            dataset_version=mark_version,
            base_interval=base_interval,
            name="mark bars",
        )
    else:
        mark_version = bars_dataset_version
        valuation_rows = trade_rows
    preflight_warnings: set[str] = set()
    funding_rows: list[dict[str, object]] = []
    if config.execution.funding.enabled:
        if funding is None:
            policy = config.execution.funding.missing_policy
            if policy == "error":
                raise BacktestError("enabled funding with error policy requires data")
            funding_version = f"{policy}-no-data"
            preflight_warnings.add(f"funding_{policy}:no_dataset")
        else:
            funding_version = _explicit(
                "funding_dataset_version", funding_dataset_version
            )
            _require(
                funding,
                {"funding_time", "symbol", "funding_rate", "dataset_version"},
                "funding",
            )
            funding_rows = (
                funding.filter(pl.col("dataset_version") == funding_version)
                .with_columns(pl.col("funding_time").cast(UTC_MS))
                .sort(["funding_time", "symbol"])
                .collect()
                .to_dicts()
            )
            if not funding_rows:
                policy = config.execution.funding.missing_policy
                if policy == "error":
                    raise BacktestError("funding input has no rows for dataset_version")
                preflight_warnings.add(f"funding_{policy}:empty_dataset")
    else:
        funding_version = "disabled"
    if (
        config.execution.funding.enabled
        and config.execution.funding.missing_policy == "exclude_symbol"
    ):
        event_symbols: dict[object, set[str]] = {}
        for row in funding_rows:
            event_symbols.setdefault(row["funding_time"], set()).add(
                str(row["symbol"])
            )
        target_symbols = {str(row["symbol"]) for row in target_rows}
        excluded = {
            symbol
            for symbol in target_symbols
            if not event_symbols
            or any(symbol not in symbols for symbols in event_symbols.values())
        }
        for row in target_rows:
            symbol = str(row["symbol"])
            if symbol not in excluded:
                continue
            row["target_weight"] = 0.0
            flags = str(row["constraint_flags"])
            row["constraint_flags"] = ";".join(
                item for item in (flags, "FUNDING_MISSING") if item
            )
        for symbol in sorted(excluded):
            preflight_warnings.add(f"funding_exclude_symbol:{symbol}")
    identity = {
        "engine": ENGINE_CODE_VERSION,
        "config": config.model_dump(mode="json"),
        "portfolio_version": portfolio_version,
        "bars_dataset_version": bars_dataset_version,
        "mark_dataset_version": mark_version,
        "funding_dataset_version": funding_version,
        "base_interval": base_interval,
        "initial_equity": initial_equity,
    }
    run_id = f"a08-{content_sha256(identity)[:24]}"
    for row in target_rows:
        row["run_id"] = run_id

    trade_by_open: dict[datetime, dict[str, dict[str, object]]] = {}
    for row in trade_rows:
        trade_by_open.setdefault(row["open_time"], {})[str(row["symbol"])] = row
    value_by_open: dict[datetime, dict[str, dict[str, object]]] = {}
    for row in valuation_rows:
        value_by_open.setdefault(row["open_time"], {})[str(row["symbol"])] = row
    fill_targets: dict[datetime, dict[str, dict[str, object]]] = {}
    for row in target_rows:
        signal = row["signal_time"]
        assert isinstance(signal, datetime)
        filled_at = fill_time(
            signal,
            signal_delay_bars=config.schedule.signal_delay_bars,
            base_interval=base_interval,
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

    quantities: dict[str, float] = {}
    averages: dict[str, float] = {}
    requested_state: dict[str, float] = {}
    previous_marks: dict[str, float] = {}
    equity = initial_equity
    peak = initial_equity
    trade_output: list[dict[str, object]] = []
    position_output: list[dict[str, object]] = []
    cost_output: list[dict[str, object]] = []
    return_output: list[dict[str, object]] = []
    warnings: set[str] = set(preflight_warnings)
    sequence = 0
    slip_rate = slippage_rate(config.execution.slippage)
    fee_rate(config.execution.fee)

    for opened_at in sorted(trade_by_open):
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
            raise BacktestError(
                "valuation and trade bars must share close_time"
            )
        start_equity = equity
        gap_pnl = 0.0
        for symbol, quantity in quantities.items():
            if abs(quantity) <= EPSILON:
                continue
            valuation = value_market.get(symbol)
            if valuation is None or symbol not in previous_marks:
                raise BacktestError(f"missing valuation bar for held symbol {symbol}")
            gap_pnl += quantity * (
                float(valuation["open"]) - previous_marks[symbol]
            )
        pretrade_equity = start_equity + gap_pnl
        if pretrade_equity <= 0:
            raise BacktestError("equity became non-positive before execution")
        symbol_costs: dict[str, dict[str, float]] = {}
        bar_turnover_notional = 0.0
        execution_basis_pnl = 0.0
        target_batch = fill_targets.get(opened_at)
        if target_batch is not None:
            requested = {
                symbol: float(row["target_weight"])
                for symbol, row in target_batch.items()
            }
            active_symbols = set(quantities) | set(requested)
            old_weights: dict[str, float] = {}
            for symbol in active_symbols:
                reference = trade_market.get(symbol)
                if reference is None:
                    raise BacktestError(
                        f"missing trade bar for target/position symbol {symbol}"
                    )
                old_weights[symbol] = (
                    quantities.get(symbol, 0.0)
                    * float(reference["open"])
                    / pretrade_equity
                )
            limited, scale, _ = limit_turnover(
                old_weights, requested, config.portfolio.max_turnover
            )
            if sum(abs(value) for value in limited.values()) > (
                config.risk.leverage + 1e-9
            ):
                raise BacktestError("requested gross exposure exceeds risk.leverage")
            signal_times = {
                row["signal_time"] for row in target_batch.values()
            }
            if len(signal_times) != 1:
                raise BacktestError(
                    "one fill batch cannot contain different signal times"
                )
            signal_time = next(iter(signal_times))
            for symbol in sorted(active_symbols):
                reference_row = trade_market[symbol]
                if not bool(reference_row["is_complete"]):
                    raise BacktestError(f"incomplete fill bar for {symbol}")
                reference_price = float(reference_row["open"])
                old_quantity = quantities.get(symbol, 0.0)
                requested_weight = requested.get(symbol, 0.0)
                filled_weight = limited[symbol]
                new_quantity = filled_weight * pretrade_equity / reference_price
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
                static_flags = (
                    str(requested_row["constraint_flags"])
                    if requested_row is not None
                    else ""
                )
                flags = static_flags
                if scale < 1.0 - EPSILON:
                    flags = ";".join(
                        value for value in (flags, "MAX_TURNOVER") if value
                    )
                requested_state[symbol] = filled_weight
                if notional <= EPSILON:
                    quantities[symbol] = new_quantity
                    continue
                fee_value = fee_cost(notional, config.execution.fee)
                slip_value = slippage_cost(
                    notional, config.execution.slippage
                )
                costs = symbol_costs.setdefault(
                    symbol, {"fee": 0.0, "slippage": 0.0, "funding": 0.0}
                )
                costs["fee"] += fee_value
                costs["slippage"] += slip_value
                bar_turnover_notional += notional
                sequence += 1
                trade_output.append(
                    {
                        "signal_time": signal_time,
                        "fill_time": opened_at,
                        "symbol": symbol,
                        "sequence": sequence,
                        "side": "BUY" if quantity_delta > 0 else "SELL",
                        "old_weight": old_weights.get(symbol, 0.0),
                        "target_weight": requested_weight,
                        "filled_weight": filled_weight,
                        "turnover": notional / pretrade_equity,
                        "reference_price": reference_price,
                        "fill_price": adverse_fill_price(
                            reference_price, quantity_delta, slip_rate
                        ),
                        "notional": notional,
                        "status": "FILLED",
                        "constraint_flags": flags,
                        "run_id": run_id,
                    }
                )
                average = updated_average_entry(
                    old_quantity,
                    new_quantity,
                    averages.get(symbol),
                    reference_price,
                )
                quantities[symbol] = new_quantity
                if average is None:
                    averages.pop(symbol, None)
                else:
                    averages[symbol] = average
            quantities = {
                symbol: value
                for symbol, value in quantities.items()
                if abs(value) > EPSILON
            }
            requested_state = {
                symbol: value
                for symbol, value in requested_state.items()
                if abs(value) > EPSILON
            }

        intrabar_pnl = 0.0
        intrabar_pnl += execution_basis_pnl
        for symbol, quantity in quantities.items():
            valuation = value_market.get(symbol)
            if valuation is None:
                raise BacktestError(f"missing valuation bar for held symbol {symbol}")
            if not bool(valuation["is_complete"]):
                raise BacktestError(f"incomplete valuation bar for {symbol}")
            intrabar_pnl += quantity * (
                float(valuation["close"]) - float(valuation["open"])
            )
        funding_total = 0.0
        if config.execution.funding.enabled:
            event_times = [
                event
                for event in funding_by_time
                if opened_at < event <= closed_at
            ]
            for event in sorted(event_times):
                records = funding_by_time[event]
                for symbol, quantity in quantities.items():
                    record = records.get(symbol)
                    if record is None:
                        policy = config.execution.funding.missing_policy
                        if policy == "error":
                            raise BacktestError(
                                f"missing funding record for {symbol} at {event}"
                            )
                        warnings.add(f"funding_{policy}:{event.isoformat()}:{symbol}")
                        continue
                    valuation = value_market[symbol]
                    price = record.get("mark_price")
                    funding_price = (
                        float(price)
                        if price is not None and isfinite(float(price)) and float(price) > 0
                        else float(valuation["close"])
                    )
                    cash = funding_cashflow(
                        quantity, funding_price, float(record["funding_rate"])
                    )
                    funding_total += cash
                    symbol_costs.setdefault(
                        symbol,
                        {"fee": 0.0, "slippage": 0.0, "funding": 0.0},
                    )["funding"] += cash
        fee_total = sum(item["fee"] for item in symbol_costs.values())
        slippage_total = sum(
            item["slippage"] for item in symbol_costs.values()
        )
        gross_pnl = gap_pnl + intrabar_pnl
        gross_return = gross_pnl / start_equity
        fee_return = fee_total / start_equity
        slippage_return = slippage_total / start_equity
        funding_return = funding_total / start_equity
        net_return = (
            gross_return - fee_return - slippage_return + funding_return
        )
        equity = start_equity * (1.0 + net_return)
        if not isfinite(equity) or equity <= 0:
            raise BacktestError("equity became non-positive or non-finite")
        peak = max(peak, equity)
        gross_notional = 0.0
        net_notional = 0.0
        for symbol, quantity in sorted(quantities.items()):
            valuation = value_market[symbol]
            mark_price = float(valuation["close"])
            signed_notional = quantity * mark_price
            gross_notional += abs(signed_notional)
            net_notional += signed_notional
            average = averages.get(symbol, mark_price)
            position_output.append(
                {
                    "timestamp": closed_at,
                    "symbol": symbol,
                    "quantity": quantity,
                    "signed_notional": signed_notional,
                    "target_weight": requested_state.get(symbol, 0.0),
                    "actual_weight": signed_notional / equity,
                    "mark_price": mark_price,
                    "unrealized_pnl": quantity * (mark_price - average),
                    "run_id": run_id,
                }
            )
        for symbol, values in sorted(symbol_costs.items()):
            cost_output.append(
                {
                    "timestamp": closed_at,
                    "symbol": symbol,
                    "fee_cost": values["fee"] / start_equity,
                    "slippage_cost": values["slippage"] / start_equity,
                    "funding_cashflow": values["funding"] / start_equity,
                    "total_cost": (
                        values["fee"]
                        + values["slippage"]
                        - values["funding"]
                    )
                    / start_equity,
                    "run_id": run_id,
                }
            )
        return_output.append(
            {
                "timestamp": closed_at,
                "gross_price_return": gross_return,
                "fee_cost": fee_return,
                "slippage_cost": slippage_return,
                "funding_return": funding_return,
                "net_return": net_return,
                "equity": equity,
                "drawdown": equity / peak - 1.0,
                "gross_exposure": gross_notional / equity,
                "net_exposure": net_notional / equity,
                "turnover": bar_turnover_notional / start_equity,
                "run_id": run_id,
            }
        )
        previous_marks = {
            symbol: float(row["close"])
            for symbol, row in value_market.items()
        }

    result_payload = {
        "run_id": run_id,
        "targets": _json_rows(
            [
                {column: row[column] for column in TARGET_SCHEMA}
                for row in target_rows
            ]
        ),
        "trades": _json_rows(trade_output),
        "positions": _json_rows(position_output),
        "costs": _json_rows(cost_output),
        "returns": _json_rows(return_output),
        "warnings": sorted(warnings),
    }
    result_hash = f"a08-{content_sha256(result_payload)[:24]}"
    return BacktestResult(
        run_id=run_id,
        result_hash=result_hash,
        targets=_frame(target_rows, TARGET_SCHEMA),
        trades=_frame(trade_output, TRADE_SCHEMA),
        positions=_frame(position_output, POSITION_SCHEMA),
        costs=_frame(cost_output, COST_SCHEMA),
        returns=_frame(return_output, RETURN_SCHEMA),
        warnings=tuple(sorted(warnings)),
    )
