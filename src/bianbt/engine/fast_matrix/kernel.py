"""Pure-Polars target-weight accounting kernel with V2-compatible economics."""

from __future__ import annotations

from datetime import datetime
from math import isfinite
from time import perf_counter

import polars as pl

from bianbt.config.backtest import BacktestConfig, PortfolioV2Config, RiskV2Config
from bianbt.data.hashing import content_sha256
from bianbt.engine.costs import fee_rate, slippage_rate
from bianbt.engine.fast_matrix.capabilities import plan_backend
from bianbt.engine.fast_matrix.result import MatrixCheckpoint, MatrixResult
from bianbt.engine.fast_matrix.target_schedule import TargetSchedule

MATRIX_ENGINE_VERSION = "a35-fast-matrix-v1"
UTC_MS = pl.Datetime("ms", "UTC")
EPSILON = 1e-10
RETURN_SCHEMA = {
    "timestamp": UTC_MS, "gross_price_return": pl.Float64,
    "fee_cost": pl.Float64, "slippage_cost": pl.Float64,
    "funding_return": pl.Float64, "net_return": pl.Float64,
    "equity": pl.Float64, "drawdown": pl.Float64,
    "gross_exposure": pl.Float64, "net_exposure": pl.Float64,
    "turnover": pl.Float64, "run_id": pl.Categorical,
}
REBALANCE_SCHEMA = {
    "signal_time": UTC_MS, "fill_time": UTC_MS, "symbol": pl.Categorical,
    "sequence": pl.Int64, "target_weight": pl.Float64,
    "old_notional": pl.Float64, "target_notional": pl.Float64,
    "delta_notional": pl.Float64, "reference_price": pl.Float64,
    "fee_amount": pl.Float64, "slippage_amount": pl.Float64,
    "run_id": pl.Categorical,
}
STATE_SCHEMA = {
    "symbol": pl.String, "quantity": pl.Float64,
    "average_entry_price": pl.Float64, "last_close": pl.Float64,
}


class MatrixExecutionError(ValueError):
    pass


def _collect(frame: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame:
    return frame.collect(engine="streaming") if isinstance(frame, pl.LazyFrame) else frame


def _frame(rows: list[dict[str, object]], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(rows).select(list(schema)).cast(schema) if rows else pl.DataFrame(schema=schema)


def _validate_bars(frame: pl.DataFrame, label: str) -> pl.DataFrame:
    required = {"open_time", "close_time", "symbol", "open", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise MatrixExecutionError(f"{label} is missing columns: {sorted(missing)}")
    result = frame.with_columns(
        pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS)
    ).sort(["open_time", "symbol"])
    if result.group_by(["open_time", "symbol"]).len().filter(pl.col("len") > 1).height:
        raise MatrixExecutionError(f"{label} contains duplicate open_time/symbol")
    invalid = result.filter(
        ~pl.col("open").is_finite() | ~pl.col("close").is_finite()
        | (pl.col("open") <= 0) | (pl.col("close") <= 0)
    )
    if invalid.height:
        raise MatrixExecutionError(f"{label} contains invalid prices")
    return result


def _identity(config: BacktestConfig, schedule: TargetSchedule, market_identity: str) -> tuple[str, str]:
    digest = content_sha256({
        "engine": MATRIX_ENGINE_VERSION,
        "config": config.model_dump(mode="json"),
        "target_schedule_id": schedule.schedule_id,
        "market_identity": market_identity,
    })
    return f"fm-{digest[:24]}", digest


def _scalar(frame: pl.DataFrame, expression: pl.Expr) -> float:
    value = frame.select(expression).item()
    return float(value or 0.0)


def _json_rows(frame: pl.DataFrame) -> list[dict[str, object]]:
    return [
        {
            name: value.isoformat() if isinstance(value, datetime) else value
            for name, value in row.items()
        }
        for row in frame.to_dicts()
    ]


def run_fast_matrix(
    schedule: TargetSchedule,
    trade_bars: pl.DataFrame | pl.LazyFrame,
    *, config: BacktestConfig, market_identity: str,
    mark_bars: pl.DataFrame | pl.LazyFrame | None = None,
    funding: pl.DataFrame | pl.LazyFrame | None = None,
    checkpoint: MatrixCheckpoint | None = None,
    finalize: bool = True,
    max_market_rows: int | None = None,
    validate_schedule_coverage: bool = True,
) -> MatrixResult:
    """Run a bounded block: one Python loop over time, Polars across symbols."""

    started = perf_counter()
    decision = plan_backend(config)
    if decision.selected_backend != "fast_matrix":
        raise MatrixExecutionError(
            f"backend planner selected {decision.selected_backend}: {decision.reason_codes}"
        )
    portfolio, risk, capital = config.portfolio, config.risk, config.capital
    assert isinstance(portfolio, PortfolioV2Config)
    assert isinstance(risk, RiskV2Config)
    assert capital is not None
    trade = _validate_bars(_collect(trade_bars), "trade bars")
    if max_market_rows is not None and trade.height > max_market_rows:
        raise MatrixExecutionError(f"market rows {trade.height} exceed hard limit {max_market_rows}")
    if validate_schedule_coverage:
        missing_rebalances = set(schedule.rebalance_times) - set(
            trade["open_time"].unique().to_list()
        )
        if missing_rebalances:
            raise MatrixExecutionError(
                "rebalance times are outside the executable market input"
            )
    if config.valuation.price == "mark_close":
        if mark_bars is None:
            raise MatrixExecutionError("mark_close requires mark_bars")
        valuation = _validate_bars(_collect(mark_bars), "mark bars")
    else:
        valuation = trade
    run_id, identity_sha256 = _identity(config, schedule, market_identity)
    if checkpoint is not None and checkpoint.identity_sha256 != identity_sha256:
        raise MatrixExecutionError("checkpoint identity does not match this execution")

    symbols = sorted(
        set(trade["symbol"].unique().to_list())
        | set(schedule.frame["symbol"].unique().to_list())
        | (set(checkpoint.symbols) if checkpoint else set())
    )
    if checkpoint is None:
        state = pl.DataFrame({
            "symbol": symbols, "quantity": [0.0] * len(symbols),
            "average_entry_price": [0.0] * len(symbols),
            "last_close": [None] * len(symbols),
        }).cast(STATE_SCHEMA)
        cash = previous_equity = peak = capital.initial_equity
        sequence = processed = 0
    else:
        restored = {
            symbol: (
                checkpoint.quantities[index], checkpoint.average_entry_prices[index],
                checkpoint.last_close_prices[index],
            ) for index, symbol in enumerate(checkpoint.symbols)
        }
        state = pl.DataFrame({
            "symbol": symbols,
            "quantity": [restored.get(symbol, (0.0, 0.0, float("nan")))[0] for symbol in symbols],
            "average_entry_price": [restored.get(symbol, (0.0, 0.0, float("nan")))[1] for symbol in symbols],
            "last_close": [restored.get(symbol, (0.0, 0.0, None))[2] for symbol in symbols],
        }).cast(STATE_SCHEMA)
        cash, previous_equity, peak = checkpoint.cash, checkpoint.previous_equity, checkpoint.peak_equity
        sequence, processed = checkpoint.sequence, checkpoint.processed_bars

    target_groups = {
        group.item(0, "fill_time"): group.select(
            pl.col("symbol").cast(pl.String), "signal_time", "target_weight"
        )
        for group in schedule.frame.partition_by("fill_time", maintain_order=True)
    }
    rebalances = set(schedule.rebalance_times)
    valuation_groups = {
        group.item(0, "open_time"): group.select("symbol", pl.col("close").alias("valuation_close"))
        for group in valuation.partition_by("open_time", maintain_order=True)
    }
    funding_groups: list[tuple[datetime, pl.DataFrame]] = []
    if config.execution.funding.enabled:
        if funding is None and config.execution.funding.missing_policy == "error":
            raise MatrixExecutionError("enabled funding requires funding input")
        if funding is not None:
            funding_frame = _collect(funding).with_columns(pl.col("funding_time").cast(UTC_MS)).sort(["funding_time", "symbol"])
            funding_groups = [(group.item(0, "funding_time"), group) for group in funding_frame.partition_by("funding_time", maintain_order=True)]
    funding_cursor = 0
    fee_fraction, slip_fraction = fee_rate(config.execution.fee), slippage_rate(config.execution.slippage)
    returns_rows: list[dict[str, object]] = []
    rebalance_rows: list[dict[str, object]] = []

    # Deliberately the only market loop. Cross-sectional work is native Polars.
    for market in trade.partition_by("open_time", maintain_order=True):
        opened_at = market.item(0, "open_time")
        closes = set(market["close_time"].to_list())
        if len(closes) != 1:
            raise MatrixExecutionError("one market snapshot must share close_time")
        closed_at = next(iter(closes))
        state = state.join(
            market.select("symbol", pl.col("open").alias("real_open")), on="symbol", how="left"
        ).with_columns(
            pl.coalesce("real_open", "last_close").alias("open_mark"),
            (pl.col("quantity").abs() > EPSILON).alias("held"),
        )
        if state.filter(pl.col("held") & pl.col("open_mark").is_null()).height:
            raise MatrixExecutionError("held symbol lacks current and carry-forward open")
        fee_amount = slip_amount = turnover_notional = 0.0
        if opened_at in rebalances:
            targets = target_groups.get(opened_at)
            if targets is None:
                targets = pl.DataFrame(schema={"symbol": pl.String, "signal_time": UTC_MS, "target_weight": pl.Float64})
            state = state.join(targets, on="symbol", how="left").with_columns(
                pl.col("target_weight").fill_null(0.0), pl.col("signal_time").fill_null(opened_at)
            )
            if state.filter(
                ~pl.col("held")
                & (pl.col("target_weight").abs() > EPSILON)
                & pl.col("real_open").is_null()
            ).height:
                raise MatrixExecutionError("rebalance would open a symbol without a real opening bar")
            pretrade_equity = cash + _scalar(
                state, pl.when("held").then(pl.col("quantity") * (pl.col("open_mark") - pl.col("average_entry_price"))).otherwise(0.0).sum()
            )
            if not isfinite(pretrade_equity) or pretrade_equity <= 0:
                raise MatrixExecutionError("equity is non-positive at rebalance")
            state = state.with_columns(
                pl.when("held").then(pl.col("quantity") * pl.col("open_mark")).otherwise(0.0).alias("old_notional"),
                pl.when(pl.col("held") & pl.col("real_open").is_null())
                .then(pl.col("quantity") * pl.col("open_mark"))
                .otherwise(pl.col("target_weight") * pretrade_equity)
                .alias("target_notional"),
            ).with_columns(
                (pl.col("target_notional") - pl.col("old_notional")).alias("delta_notional")
            ).with_columns(
                (pl.col("quantity") + pl.col("delta_notional") / pl.col("open_mark")).alias("new_quantity")
            ).with_columns(
                pl.when(pl.col("quantity") * pl.col("new_quantity") <= 0).then(pl.col("quantity").abs())
                .otherwise((pl.col("quantity").abs() - pl.col("new_quantity").abs()).clip(lower_bound=0.0)).alias("closing_quantity")
            ).with_columns(
                (pl.col("closing_quantity") * (pl.col("open_mark") - pl.col("average_entry_price")) * pl.col("quantity").sign()).alias("realized_pnl")
            )
            turnover_notional = _scalar(state, pl.col("delta_notional").abs().sum())
            fee_amount, slip_amount = turnover_notional * fee_fraction, turnover_notional * slip_fraction
            cash += _scalar(state, pl.col("realized_pnl").sum()) - fee_amount - slip_amount
            traded = state.filter(pl.col("delta_notional").abs() > EPSILON)
            for row in traded.select(
                "signal_time", "symbol", "target_weight", "old_notional", "target_notional", "delta_notional", "open_mark"
            ).to_dicts():
                sequence += 1
                delta = float(row["delta_notional"])
                rebalance_rows.append({
                    "signal_time": row["signal_time"], "fill_time": opened_at,
                    "symbol": row["symbol"], "sequence": sequence,
                    "target_weight": row["target_weight"], "old_notional": row["old_notional"],
                    "target_notional": row["target_notional"], "delta_notional": delta,
                    "reference_price": row["open_mark"], "fee_amount": abs(delta) * fee_fraction,
                    "slippage_amount": abs(delta) * slip_fraction, "run_id": run_id,
                })
            state = state.with_columns(
                pl.when(pl.col("new_quantity").abs() <= EPSILON).then(0.0)
                .when((pl.col("quantity").abs() <= EPSILON) | (pl.col("quantity") * pl.col("new_quantity") <= 0)).then(pl.col("open_mark"))
                .when(pl.col("new_quantity").abs() > pl.col("quantity").abs() + EPSILON).then(
                    (pl.col("quantity").abs() * pl.col("average_entry_price") + (pl.col("new_quantity").abs() - pl.col("quantity").abs()) * pl.col("open_mark")) / pl.col("new_quantity").abs()
                ).otherwise(pl.col("average_entry_price")).alias("average_entry_price"),
                pl.col("new_quantity").alias("quantity"),
            ).select("symbol", "quantity", "average_entry_price", "last_close", "real_open", "open_mark", "held")

        funding_total = 0.0
        while funding_cursor < len(funding_groups):
            event_at, records = funding_groups[funding_cursor]
            if event_at <= opened_at:
                funding_cursor += 1
                continue
            if event_at > closed_at:
                break
            columns = ["symbol", "funding_rate"] + (["mark_price"] if "mark_price" in records.columns else [])
            funded = state.join(records.select(columns), on="symbol", how="left")
            active = pl.col("quantity").abs() > EPSILON
            if config.execution.funding.missing_policy == "error" and funded.filter(active & pl.col("funding_rate").is_null()).height:
                raise MatrixExecutionError("funding input is missing an active symbol")
            price = pl.coalesce("mark_price", "open_mark") if "mark_price" in funded.columns else pl.col("open_mark")
            event_cashflow = _scalar(funded, (-pl.col("quantity") * price * pl.col("funding_rate").fill_null(0.0)).sum())
            funding_total += event_cashflow
            cash += event_cashflow
            funding_cursor += 1

        values = valuation_groups.get(opened_at)
        if values is None:
            raise MatrixExecutionError("valuation snapshot is missing")
        state = state.join(values, on="symbol", how="left").with_columns(
            pl.coalesce("valuation_close", "last_close").alias("close_mark"),
            (pl.col("quantity").abs() > EPSILON).alias("held"),
        )
        if state.filter(pl.col("held") & pl.col("close_mark").is_null()).height:
            raise MatrixExecutionError("held symbol lacks current and carry-forward close")
        state = state.with_columns(
            pl.when("held").then(pl.col("quantity") * (pl.col("close_mark") - pl.col("average_entry_price"))).otherwise(0.0).alias("unrealized"),
            pl.when("held").then(pl.col("quantity") * pl.col("close_mark")).otherwise(0.0).alias("signed_notional"),
        )
        equity = cash + _scalar(state, pl.col("unrealized").sum())
        if not isfinite(equity) or equity <= 0:
            raise MatrixExecutionError("equity became non-positive or non-finite")
        peak = max(peak, equity)
        net_return = equity / previous_equity - 1.0
        returns_rows.append({
            "timestamp": closed_at,
            "gross_price_return": net_return + fee_amount / previous_equity + slip_amount / previous_equity - funding_total / previous_equity,
            "fee_cost": fee_amount / previous_equity, "slippage_cost": slip_amount / previous_equity,
            "funding_return": funding_total / previous_equity, "net_return": net_return,
            "equity": equity, "drawdown": equity / peak - 1.0,
            "gross_exposure": _scalar(state, pl.col("signed_notional").abs().sum()) / equity,
            "net_exposure": _scalar(state, pl.col("signed_notional").sum()) / equity,
            "turnover": turnover_notional / previous_equity, "run_id": run_id,
        })
        previous_equity = equity
        state = state.with_columns(pl.coalesce("valuation_close", "last_close").alias("last_close")).select(list(STATE_SCHEMA))
        processed += market.height

    checkpoint_result = MatrixCheckpoint(
        identity_sha256=identity_sha256, symbols=tuple(state["symbol"].to_list()),
        quantities=tuple(state["quantity"].to_list()),
        average_entry_prices=tuple(state["average_entry_price"].to_list()),
        last_close_prices=tuple(state["last_close"].to_list()),
        cash=cash, previous_equity=previous_equity, peak_equity=peak,
        sequence=sequence, processed_bars=processed,
    )
    returns = _frame(returns_rows, RETURN_SCHEMA)
    rebalances_frame = _frame(rebalance_rows, REBALANCE_SCHEMA)
    result_hash = content_sha256({
        "run_id": run_id, "returns": _json_rows(returns),
        "rebalances": _json_rows(rebalances_frame), "checkpoint": checkpoint_result.__dict__,
    })
    return MatrixResult(
        run_id=run_id, result_hash=result_hash, returns=returns,
        rebalance_summary=rebalances_frame, checkpoint=checkpoint_result,
        warnings=(), diagnostics={
            "engine_version": MATRIX_ENGINE_VERSION,
            "execution_mode": "chunk" if not finalize or checkpoint is not None else "in_memory",
            "market_rows": trade.height, "symbol_width": len(symbols),
            "elapsed_seconds": perf_counter() - started,
            "backend_decision": decision.as_dict(),
        },
    )
