"""Joint multi-candidate Fast Matrix execution over shared market intervals."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from time import perf_counter
from typing import Mapping

import polars as pl

from bfbt.config.backtest import BacktestConfig
from bfbt.data.hashing import content_sha256
from bfbt.engine.costs import fee_rate, slippage_rate
from bfbt.engine.fast_matrix.capabilities import plan_backend
from bfbt.engine.fast_matrix.kernel import (
    EPSILON,
    MATRIX_ENGINE_VERSION,
    REBALANCE_SCHEMA,
    RETURN_SCHEMA,
    MatrixExecutionError,
    _collect,
    _frame,
    _identity,
    _json_rows,
    _validate_bars,
)
from bfbt.engine.fast_matrix.result import MatrixCheckpoint, MatrixResult
from bfbt.engine.fast_matrix.target_schedule import TargetSchedule


@dataclass(frozen=True)
class MatrixBatchResult:
    candidates: Mapping[str, MatrixResult]
    diagnostics: Mapping[str, object]


@dataclass
class _CandidateState:
    schedule: TargetSchedule
    run_id: str
    identity_sha256: str
    cash: float
    previous_equity: float
    peak_equity: float
    quantities: dict[str, float] = field(default_factory=dict)
    average_entry_prices: dict[str, float] = field(default_factory=dict)
    sequence: int = 0
    returns: list[pl.DataFrame] = field(default_factory=list)
    rebalances: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class _FundingEvent:
    timestamp: datetime
    rows: Mapping[str, tuple[float, float | None]]


def _price_histories(
    valuation: pl.DataFrame,
) -> dict[str, tuple[list[datetime], list[float]]]:
    histories: dict[str, tuple[list[datetime], list[float]]] = {}
    for group in valuation.partition_by("symbol", maintain_order=True):
        symbol = str(group.item(0, "symbol"))
        histories[symbol] = (
            group["open_time"].to_list(),
            [float(value) for value in group["close"].to_list()],
        )
    return histories


def _carried_close(
    histories: Mapping[str, tuple[list[datetime], list[float]]],
    symbol: str,
    timestamp: datetime,
    *,
    include_current: bool,
) -> float | None:
    history = histories.get(symbol)
    if history is None:
        return None
    times, prices = history
    index = (bisect_right if include_current else bisect_left)(times, timestamp) - 1
    return prices[index] if index >= 0 else None


def _funding_events(frame: pl.DataFrame | pl.LazyFrame | None) -> list[_FundingEvent]:
    if frame is None:
        return []
    actual = _collect(frame)
    required = {"funding_time", "symbol", "funding_rate"}
    missing = required - set(actual.columns)
    if missing:
        raise MatrixExecutionError(f"funding is missing columns: {sorted(missing)}")
    actual = actual.with_columns(
        pl.col("funding_time").cast(pl.Datetime("ms", "UTC"))
    ).sort(["funding_time", "symbol"])
    events: list[_FundingEvent] = []
    for group in actual.partition_by("funding_time", maintain_order=True):
        rows: dict[str, tuple[float, float | None]] = {}
        for row in group.to_dicts():
            mark = row.get("mark_price")
            rows[str(row["symbol"])] = (
                float(row["funding_rate"]),
                None if mark is None else float(mark),
            )
        events.append(_FundingEvent(group.item(0, "funding_time"), rows))
    return events


def _event_bar_indices(
    events: list[_FundingEvent],
    opens: list[datetime],
    closes: list[datetime],
) -> dict[int, list[_FundingEvent]]:
    by_bar: dict[int, list[_FundingEvent]] = {}
    for event in events:
        index = bisect_left(closes, event.timestamp)
        if index < len(opens) and opens[index] < event.timestamp <= closes[index]:
            by_bar.setdefault(index, []).append(event)
    return by_bar


def _target_snapshots(
    names: list[str],
    candidates: Mapping[str, TargetSchedule],
) -> tuple[
    dict[datetime, list[str]],
    dict[tuple[str, datetime], dict[str, tuple[datetime, float]]],
]:
    due: dict[datetime, list[str]] = {}
    snapshots: dict[tuple[str, datetime], dict[str, tuple[datetime, float]]] = {}
    for name in names:
        schedule = candidates[name]
        for timestamp in schedule.rebalance_times:
            due.setdefault(timestamp, []).append(name)
        for group in schedule.frame.partition_by("fill_time", maintain_order=True):
            timestamp = group.item(0, "fill_time")
            snapshots[(name, timestamp)] = {
                str(row["symbol"]): (row["signal_time"], float(row["target_weight"]))
                for row in group.select(
                    "signal_time", "symbol", "target_weight"
                ).to_dicts()
            }
    return due, snapshots


def _rebalance_candidate(
    state: _CandidateState,
    opened_at: datetime,
    targets: Mapping[str, tuple[datetime, float]],
    real_opens: Mapping[str, float],
    histories: Mapping[str, tuple[list[datetime], list[float]]],
    fee_fraction: float,
    slip_fraction: float,
) -> tuple[float, float, float]:
    symbols = sorted(set(state.quantities) | set(targets))
    open_marks: dict[str, float | None] = {
        symbol: real_opens.get(symbol)
        or _carried_close(histories, symbol, opened_at, include_current=False)
        for symbol in symbols
    }
    for symbol in state.quantities:
        if open_marks[symbol] is None:
            raise MatrixExecutionError("held symbol lacks current and carry-forward open")
    for symbol, (_, weight) in targets.items():
        if (
            symbol not in state.quantities
            and abs(weight) > EPSILON
            and symbol not in real_opens
        ):
            raise MatrixExecutionError(
                "rebalance would open a symbol without a real opening bar"
            )

    pretrade_equity = state.cash + sum(
        state.quantities[symbol]
        * (float(open_marks[symbol]) - state.average_entry_prices[symbol])
        for symbol in state.quantities
    )
    if not isfinite(pretrade_equity) or pretrade_equity <= 0:
        raise MatrixExecutionError("equity is non-positive at rebalance")

    changes: list[
        tuple[str, datetime, float, float, float, float, float, float, float]
    ] = []
    realized_total = turnover = 0.0
    new_quantities = dict(state.quantities)
    new_averages = dict(state.average_entry_prices)
    for symbol in symbols:
        old_quantity = state.quantities.get(symbol, 0.0)
        old_average = state.average_entry_prices.get(symbol, 0.0)
        held = abs(old_quantity) > EPSILON
        open_mark = (
            float(open_marks[symbol])
            if open_marks[symbol] is not None
            else float("nan")
        )
        signal_time, target_weight = targets.get(symbol, (opened_at, 0.0))
        old_notional = old_quantity * open_mark if held else 0.0
        target_notional = (
            old_notional
            if held and symbol not in real_opens
            else target_weight * pretrade_equity
        )
        delta_notional = target_notional - old_notional
        new_quantity = old_quantity + delta_notional / open_mark
        if old_quantity * new_quantity <= 0:
            closing_quantity = abs(old_quantity)
        else:
            closing_quantity = max(abs(old_quantity) - abs(new_quantity), 0.0)
        realized = closing_quantity * (open_mark - old_average) * (
            1.0 if old_quantity > 0 else -1.0 if old_quantity < 0 else 0.0
        )
        realized_total += realized
        turnover += abs(delta_notional)
        if abs(delta_notional) > EPSILON:
            changes.append(
                (
                    symbol,
                    signal_time,
                    target_weight,
                    old_notional,
                    target_notional,
                    delta_notional,
                    open_mark,
                    abs(delta_notional) * fee_fraction,
                    abs(delta_notional) * slip_fraction,
                )
            )
        if abs(new_quantity) <= EPSILON:
            new_quantities.pop(symbol, None)
            new_averages.pop(symbol, None)
        else:
            if abs(old_quantity) <= EPSILON or old_quantity * new_quantity <= 0:
                new_average = open_mark
            elif abs(new_quantity) > abs(old_quantity) + EPSILON:
                new_average = (
                    abs(old_quantity) * old_average
                    + (abs(new_quantity) - abs(old_quantity)) * open_mark
                ) / abs(new_quantity)
            else:
                new_average = old_average
            new_quantities[symbol] = new_quantity
            new_averages[symbol] = new_average

    fee_amount, slip_amount = turnover * fee_fraction, turnover * slip_fraction
    state.cash += realized_total - fee_amount - slip_amount
    state.quantities, state.average_entry_prices = new_quantities, new_averages
    for (
        symbol,
        signal_time,
        target_weight,
        old_notional,
        target_notional,
        delta_notional,
        open_mark,
        fee,
        slip,
    ) in changes:
        state.sequence += 1
        state.rebalances.append(
            {
                "signal_time": signal_time,
                "fill_time": opened_at,
                "symbol": symbol,
                "sequence": state.sequence,
                "target_weight": target_weight,
                "old_notional": old_notional,
                "target_notional": target_notional,
                "delta_notional": delta_notional,
                "reference_price": open_mark,
                "fee_amount": fee,
                "slippage_amount": slip,
                "run_id": state.run_id,
            }
        )
    return fee_amount, slip_amount, turnover


def _apply_funding(
    state: _CandidateState,
    events: list[_FundingEvent],
    opened_at: datetime,
    real_opens: Mapping[str, float],
    histories: Mapping[str, tuple[list[datetime], list[float]]],
    missing_policy: str,
) -> float:
    total = 0.0
    for event in events:
        if missing_policy == "error":
            missing = set(state.quantities) - set(event.rows)
            if missing:
                raise MatrixExecutionError(
                    "funding input is missing an active symbol"
                )
        for symbol, quantity in state.quantities.items():
            record = event.rows.get(symbol)
            if record is None:
                continue
            rate, mark_price = record
            price = mark_price
            if price is None:
                price = real_opens.get(symbol) or _carried_close(
                    histories, symbol, opened_at, include_current=False
                )
            if price is None:
                raise MatrixExecutionError(
                    "held symbol lacks current and carry-forward open"
                )
            total -= quantity * price * rate
    state.cash += total
    return total


def _value_interval(
    names: list[str],
    states: Mapping[str, _CandidateState],
    clock: pl.DataFrame,
    valuation: pl.DataFrame,
    fee_amounts: Mapping[str, float],
    slip_amounts: Mapping[str, float],
    turnover_amounts: Mapping[str, float],
    funding_amounts: Mapping[str, float],
) -> None:
    candidate_frame = pl.DataFrame({"candidate": names})
    grid = candidate_frame.join(clock, how="cross")
    holding_rows = [
        {
            "candidate": name,
            "symbol": symbol,
            "quantity": quantity,
            "average_entry_price": states[name].average_entry_prices[symbol],
        }
        for name in names
        for symbol, quantity in states[name].quantities.items()
    ]
    if holding_rows:
        holdings = pl.DataFrame(holding_rows)
        held_symbols = holdings.select("symbol").unique()
        requests = clock.select("open_time").join(
            held_symbols, how="cross"
        ).sort(["symbol", "open_time"])
        prices = requests.join_asof(
            valuation.select(
                "open_time",
                pl.col("symbol").cast(pl.String),
                pl.col("close").alias("close_mark"),
            ).sort(["symbol", "open_time"]),
            on="open_time",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        if prices.filter(pl.col("close_mark").is_null()).height:
            raise MatrixExecutionError(
                "held symbol lacks current and carry-forward close"
            )
        valued = holdings.join(prices, on="symbol", how="inner").with_columns(
            (
                pl.col("quantity")
                * (pl.col("close_mark") - pl.col("average_entry_price"))
            ).alias("unrealized"),
            (pl.col("quantity") * pl.col("close_mark")).alias(
                "signed_notional"
            ),
        )
        totals = valued.group_by(
            "candidate", "open_time", maintain_order=True
        ).agg(
            pl.col("unrealized").sum(),
            pl.col("signed_notional").abs().sum().alias("gross_notional"),
            pl.col("signed_notional").sum().alias("net_notional"),
        )
        grid = grid.join(totals, on=["candidate", "open_time"], how="left")
    else:
        grid = grid.with_columns(
            pl.lit(0.0).alias("unrealized"),
            pl.lit(0.0).alias("gross_notional"),
            pl.lit(0.0).alias("net_notional"),
        )

    scalar = pl.DataFrame(
        {
            "candidate": names,
            "cash": [states[name].cash for name in names],
            "initial_previous": [states[name].previous_equity for name in names],
            "initial_peak": [states[name].peak_equity for name in names],
            "fee_amount": [fee_amounts.get(name, 0.0) for name in names],
            "slip_amount": [slip_amounts.get(name, 0.0) for name in names],
            "turnover_notional": [
                turnover_amounts.get(name, 0.0) for name in names
            ],
            "funding_total": [
                funding_amounts.get(name, 0.0) for name in names
            ],
            "run_id": [states[name].run_id for name in names],
        }
    )
    first_open = clock.item(0, "open_time")
    valued_grid = (
        grid.join(scalar, on="candidate", how="left")
        .with_columns(
            pl.col("unrealized").fill_null(0.0),
            pl.col("gross_notional").fill_null(0.0),
            pl.col("net_notional").fill_null(0.0),
        )
        .with_columns(
            (pl.col("cash") + pl.col("unrealized")).alias("equity"),
            pl.when(pl.col("open_time") == first_open)
            .then("fee_amount")
            .otherwise(0.0)
            .alias("bar_fee"),
            pl.when(pl.col("open_time") == first_open)
            .then("slip_amount")
            .otherwise(0.0)
            .alias("bar_slip"),
            pl.when(pl.col("open_time") == first_open)
            .then("turnover_notional")
            .otherwise(0.0)
            .alias("bar_turnover"),
            pl.when(pl.col("open_time") == first_open)
            .then("funding_total")
            .otherwise(0.0)
            .alias("bar_funding"),
        )
        .sort(["candidate", "open_time"])
    )
    if valued_grid.filter(
        ~pl.col("equity").is_finite() | (pl.col("equity") <= 0)
    ).height:
        raise MatrixExecutionError("equity became non-positive or non-finite")
    valued_grid = valued_grid.with_columns(
        pl.col("equity")
        .shift(1)
        .over("candidate")
        .fill_null(pl.col("initial_previous"))
        .alias("previous_equity"),
        pl.max_horizontal(
            pl.col("equity").cum_max().over("candidate"),
            pl.col("initial_peak"),
        ).alias("peak_equity"),
    ).with_columns(
        (pl.col("equity") / pl.col("previous_equity") - 1.0).alias(
            "net_return"
        )
    ).with_columns(
        (
            pl.col("net_return")
            + pl.col("bar_fee") / pl.col("previous_equity")
            + pl.col("bar_slip") / pl.col("previous_equity")
            - pl.col("bar_funding") / pl.col("previous_equity")
        ).alias("gross_price_return"),
        (pl.col("bar_fee") / pl.col("previous_equity")).alias("fee_cost"),
        (pl.col("bar_slip") / pl.col("previous_equity")).alias(
            "slippage_cost"
        ),
        (pl.col("bar_funding") / pl.col("previous_equity")).alias(
            "funding_return"
        ),
        (pl.col("equity") / pl.col("peak_equity") - 1.0).alias("drawdown"),
        (pl.col("gross_notional") / pl.col("equity")).alias(
            "gross_exposure"
        ),
        (pl.col("net_notional") / pl.col("equity")).alias("net_exposure"),
        (pl.col("bar_turnover") / pl.col("previous_equity")).alias(
            "turnover"
        ),
    )
    for name in names:
        candidate_values = valued_grid.filter(pl.col("candidate") == name)
        output = candidate_values.select(
            pl.col("close_time").alias("timestamp"),
            "gross_price_return",
            "fee_cost",
            "slippage_cost",
            "funding_return",
            "net_return",
            "equity",
            "drawdown",
            "gross_exposure",
            "net_exposure",
            "turnover",
            "run_id",
        ).cast(RETURN_SCHEMA)
        states[name].returns.append(output)
        states[name].previous_equity = float(candidate_values.item(-1, "equity"))
        states[name].peak_equity = float(
            candidate_values.item(-1, "peak_equity")
        )


def run_fast_matrix_batch(
    candidates: Mapping[str, TargetSchedule],
    trade_bars: pl.DataFrame | pl.LazyFrame,
    *,
    config: BacktestConfig,
    market_identity: str,
    mark_bars: pl.DataFrame | pl.LazyFrame | None = None,
    funding: pl.DataFrame | pl.LazyFrame | None = None,
    max_candidates: int = 64,
    max_market_rows: int | None = None,
) -> MatrixBatchResult:
    """Run candidates jointly, recursing only at rebalance/funding boundaries."""

    started = perf_counter()
    if not candidates or len(candidates) > max_candidates:
        raise MatrixExecutionError(
            f"candidate count must be within 1..{max_candidates}"
        )
    decision = plan_backend(config)
    if decision.selected_backend != "fast_matrix":
        raise MatrixExecutionError(
            f"backend planner selected {decision.selected_backend}: "
            f"{decision.reason_codes}"
        )
    assert config.capital is not None
    trade = _validate_bars(_collect(trade_bars), "trade bars")
    if max_market_rows is not None and trade.height > max_market_rows:
        raise MatrixExecutionError(
            f"market rows {trade.height} exceed hard limit {max_market_rows}"
        )
    valuation = trade
    if config.valuation.price == "mark_close":
        if mark_bars is None:
            raise MatrixExecutionError("mark_close requires mark_bars")
        valuation = _validate_bars(_collect(mark_bars), "mark bars")

    clock = (
        trade.group_by("open_time", maintain_order=True)
        .agg(
            pl.col("close_time").n_unique().alias("close_count"),
            pl.col("close_time").first(),
        )
        .sort("open_time")
    )
    if clock.filter(pl.col("close_count") != 1).height:
        raise MatrixExecutionError("one market snapshot must share close_time")
    clock = clock.select("open_time", "close_time")
    opens: list[datetime] = clock["open_time"].to_list()
    closes: list[datetime] = clock["close_time"].to_list()
    valuation_times = set(valuation["open_time"].unique().to_list())
    if set(opens) - valuation_times:
        raise MatrixExecutionError("valuation snapshot is missing")

    names = sorted(candidates)
    for schedule in candidates.values():
        if set(schedule.rebalance_times) - set(opens):
            raise MatrixExecutionError(
                "rebalance times are outside the executable market input"
            )
    due, snapshots = _target_snapshots(names, candidates)
    histories = _price_histories(valuation)
    open_rows = {
        group.item(0, "open_time"): {
            str(row["symbol"]): float(row["open"])
            for row in group.select("symbol", "open").to_dicts()
        }
        for group in trade.partition_by("open_time", maintain_order=True)
    }
    events = (
        _funding_events(funding) if config.execution.funding.enabled else []
    )
    if (
        config.execution.funding.enabled
        and funding is None
        and config.execution.funding.missing_policy == "error"
    ):
        raise MatrixExecutionError("enabled funding requires funding input")
    events_by_bar = _event_bar_indices(events, opens, closes)
    open_index = {timestamp: index for index, timestamp in enumerate(opens)}
    boundary_indices = {0, *events_by_bar}
    boundary_indices.update(open_index[timestamp] for timestamp in due)
    boundaries = sorted(boundary_indices)

    states: dict[str, _CandidateState] = {}
    for name in names:
        run_id, digest = _identity(
            config, candidates[name], f"{market_identity}:{name}"
        )
        states[name] = _CandidateState(
            schedule=candidates[name],
            run_id=run_id,
            identity_sha256=digest,
            cash=config.capital.initial_equity,
            previous_equity=config.capital.initial_equity,
            peak_equity=config.capital.initial_equity,
        )

    fee_fraction = fee_rate(config.execution.fee)
    slip_fraction = slippage_rate(config.execution.slippage)
    peak_sparse_rows = 0
    for position, start_index in enumerate(boundaries):
        end_index = (
            boundaries[position + 1]
            if position + 1 < len(boundaries)
            else len(opens)
        )
        opened_at = opens[start_index]
        real_opens = open_rows[opened_at]
        fees: dict[str, float] = {}
        slips: dict[str, float] = {}
        turnovers: dict[str, float] = {}
        fundings: dict[str, float] = {}
        for name in due.get(opened_at, []):
            fees[name], slips[name], turnovers[name] = _rebalance_candidate(
                states[name],
                opened_at,
                snapshots.get((name, opened_at), {}),
                real_opens,
                histories,
                fee_fraction,
                slip_fraction,
            )
        if start_index in events_by_bar:
            for name in names:
                fundings[name] = _apply_funding(
                    states[name],
                    events_by_bar[start_index],
                    opened_at,
                    real_opens,
                    histories,
                    config.execution.funding.missing_policy,
                )
        peak_sparse_rows = max(
            peak_sparse_rows,
            sum(len(states[name].quantities) for name in names),
        )
        _value_interval(
            names,
            states,
            clock.slice(start_index, end_index - start_index),
            valuation,
            fees,
            slips,
            turnovers,
            fundings,
        )

    all_symbols = sorted(
        set(trade["symbol"].cast(pl.String).unique().to_list())
        | {
            str(symbol)
            for schedule in candidates.values()
            for symbol in schedule.frame["symbol"]
            .cast(pl.String)
            .unique()
            .to_list()
        }
    )
    final_time = opens[-1]
    elapsed = perf_counter() - started
    results: dict[str, MatrixResult] = {}
    for name in names:
        state = states[name]
        checkpoint = MatrixCheckpoint(
            identity_sha256=state.identity_sha256,
            symbols=tuple(all_symbols),
            quantities=tuple(
                state.quantities.get(symbol, 0.0) for symbol in all_symbols
            ),
            average_entry_prices=tuple(
                state.average_entry_prices.get(symbol, 0.0)
                for symbol in all_symbols
            ),
            last_close_prices=tuple(
                _carried_close(
                    histories, symbol, final_time, include_current=True
                )
                for symbol in all_symbols
            ),
            cash=state.cash,
            previous_equity=state.previous_equity,
            peak_equity=state.peak_equity,
            sequence=state.sequence,
            processed_bars=trade.height,
        )
        returns = (
            pl.concat(state.returns)
            if state.returns
            else _frame([], RETURN_SCHEMA)
        )
        rebalances = _frame(state.rebalances, REBALANCE_SCHEMA)
        result_hash = content_sha256(
            {
                "run_id": state.run_id,
                "returns": _json_rows(returns),
                "rebalances": _json_rows(rebalances),
                "checkpoint": checkpoint.__dict__,
            }
        )
        results[name] = MatrixResult(
            run_id=state.run_id,
            result_hash=result_hash,
            returns=returns,
            rebalance_summary=rebalances,
            checkpoint=checkpoint,
            warnings=(),
            diagnostics={
                "engine_version": MATRIX_ENGINE_VERSION,
                "execution_mode": "joint_batch",
                "market_rows": trade.height,
                "symbol_width": len(all_symbols),
                "elapsed_seconds": elapsed,
                "backend_decision": decision.as_dict(),
                "shared_state_boundaries": len(boundaries),
            },
        )

    dense_cells = len(names) * len(all_symbols)
    return MatrixBatchResult(
        candidates=results,
        diagnostics={
            "candidate_count": len(results),
            "shared_market_loads": 1,
            "shared_market_rows": trade.height,
            "shared_market_preprocess_passes": 1,
            "state_boundary_count": len(boundaries),
            "market_time_count": len(opens),
            "valuation_interval_count": len(boundaries),
            "peak_sparse_position_rows": peak_sparse_rows,
            "dense_candidate_symbol_cells": dense_cells,
            "peak_sparse_state_fraction": (
                peak_sparse_rows / dense_cells if dense_cells else 0.0
            ),
            "elapsed_seconds": elapsed,
        },
    )
