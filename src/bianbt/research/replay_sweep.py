"""Bounded research sweeps over shared, already-materialized replay inputs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from bianbt.config.backtest import BacktestConfig, PortfolioV2Config
from bianbt.config.durations import duration_seconds
from bianbt.data.hashing import content_sha256
from bianbt.engine.v2 import (
    V2ExecutionCheckpoint,
    run_v2_backtest,
    run_v2_backtest_chunk,
)
from bianbt.performance.chunks import plan_time_chunks
from bianbt.performance.memory import AbsoluteMemoryMonitor
from bianbt.portfolio.constraints import finalize_v2_selections


class ReplaySweepError(ValueError):
    """A replay sweep is unsafe, unbounded, or mixes signal dependencies."""


@dataclass(frozen=True)
class ReplaySweepCandidate:
    name: str
    config: BacktestConfig


@dataclass(frozen=True)
class ReplaySweepResult:
    name: str
    result_hash: str
    ending_equity: float
    total_return: float
    max_drawdown: float
    trade_count: int
    warning_count: int


def _selection_identity(config: BacktestConfig) -> dict[str, object]:
    portfolio = config.portfolio
    if not isinstance(portfolio, PortfolioV2Config):
        raise ReplaySweepError("replay sweep requires config_version=v2")
    return {
        "run_start": config.run.start,
        "run_end": config.run.end,
        "dataset_version": config.run.dataset_version,
        "factor_interval": config.schedule.factor_interval,
        "rebalance_interval": config.schedule.rebalance_interval,
        "selection": portfolio.selection.model_dump(mode="json"),
    }


def run_replay_sweep(
    *,
    candidates: tuple[ReplaySweepCandidate, ...],
    selections: pl.DataFrame,
    rankings: pl.DataFrame,
    trade_bars: pl.DataFrame,
    mark_bars: pl.DataFrame | None,
    funding: pl.DataFrame | None,
    base_interval: str,
    factor_version: str,
    universe_version: str,
    bars_dataset_version: str,
    mark_dataset_version: str | None,
    funding_dataset_version: str | None,
    max_candidates: int = 256,
) -> tuple[ReplaySweepResult, ...]:
    """Replay independent configs while sharing one bounded market-data load."""

    if not candidates:
        raise ReplaySweepError("replay sweep requires at least one candidate")
    if len(candidates) > max_candidates:
        raise ReplaySweepError(
            f"candidate count {len(candidates)} exceeds max_candidates={max_candidates}"
        )
    names = [candidate.name for candidate in candidates]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ReplaySweepError("candidate names must be non-empty and unique")
    expected = _selection_identity(candidates[0].config)
    for candidate in candidates:
        if candidate.config.performance.mode != "in_memory":
            raise ReplaySweepError(
                "bounded shared-input sweep currently requires performance.mode=in_memory"
            )
        if _selection_identity(candidate.config) != expected:
            raise ReplaySweepError(
                "sweep candidates must share run range, dataset, clocks, and selection"
            )

    output: list[ReplaySweepResult] = []
    for candidate in candidates:
        portfolio = candidate.config.portfolio
        assert isinstance(portfolio, PortfolioV2Config)
        targets, portfolio_version = finalize_v2_selections(
            selections.lazy(),
            portfolio,
            factor_version=factor_version,
            universe_version=universe_version,
        )
        result = run_v2_backtest(
            selections.lazy(),
            targets,
            rankings.lazy(),
            trade_bars.lazy(),
            mark_bars.lazy() if mark_bars is not None else None,
            funding.lazy() if funding is not None else None,
            config=candidate.config,
            base_interval=base_interval,
            portfolio_version=portfolio_version,
            bars_dataset_version=bars_dataset_version,
            mark_dataset_version=mark_dataset_version,
            funding_dataset_version=funding_dataset_version,
        )
        returns = result.result.returns.select(
            "equity", "drawdown"
        ).collect(engine="streaming")
        initial = candidate.config.capital.initial_equity
        ending = float(returns.item(-1, "equity"))
        output.append(
            ReplaySweepResult(
                name=candidate.name,
                result_hash=result.result.result_hash,
                ending_equity=ending,
                total_return=ending / initial - 1.0,
                max_drawdown=float(returns["drawdown"].min()),
                trade_count=result.linked_trades.height,
                warning_count=len(result.result.warnings),
            )
        )
    return tuple(output)


def run_chunked_replay_sweep(
    *,
    candidates: tuple[ReplaySweepCandidate, ...],
    selections: pl.LazyFrame,
    rankings: pl.LazyFrame,
    trade_bars: pl.LazyFrame,
    mark_bars: pl.LazyFrame | None,
    funding: pl.LazyFrame | None,
    execution_start: datetime,
    execution_end: datetime,
    base_interval: str,
    factor_version: str,
    universe_version: str,
    bars_dataset_version: str,
    mark_dataset_version: str | None,
    funding_dataset_version: str | None,
    max_candidates: int = 64,
) -> tuple[ReplaySweepResult, ...]:
    """Advance many states chunk by chunk while loading each market slice once."""

    if not candidates or len(candidates) > max_candidates:
        raise ReplaySweepError(
            f"candidate count {len(candidates)} must be within 1..{max_candidates}"
        )
    expected = _selection_identity(candidates[0].config)
    chunk_interval = candidates[0].config.performance.chunk_interval
    for candidate in candidates:
        if candidate.config.performance.mode != "chunked":
            raise ReplaySweepError("chunked sweep requires performance.mode=chunked")
        if candidate.config.performance.chunk_interval != chunk_interval:
            raise ReplaySweepError("chunked sweep candidates must share chunk_interval")
        if _selection_identity(candidate.config) != expected:
            raise ReplaySweepError(
                "sweep candidates must share run range, dataset, clocks, and selection"
            )
    names = [candidate.name for candidate in candidates]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ReplaySweepError("candidate names must be non-empty and unique")

    portfolios: dict[str, tuple[pl.LazyFrame, str]] = {}
    for candidate in candidates:
        portfolio = candidate.config.portfolio
        assert isinstance(portfolio, PortfolioV2Config)
        portfolios[candidate.name] = finalize_v2_selections(
            selections,
            portfolio,
            factor_version=factor_version,
            universe_version=universe_version,
        )
    plan = plan_time_chunks(
        start=execution_start,
        end=execution_end,
        chunk_interval=chunk_interval,
    )
    checkpoints: dict[str, V2ExecutionCheckpoint | None] = {
        candidate.name: None for candidate in candidates
    }
    hashes: dict[str, list[str]] = {candidate.name: [] for candidate in candidates}
    trade_counts = {candidate.name: 0 for candidate in candidates}
    endings = {
        candidate.name: candidate.config.capital.initial_equity
        for candidate in candidates
    }
    drawdowns = {candidate.name: 0.0 for candidate in candidates}
    warnings: dict[str, set[str]] = {candidate.name: set() for candidate in candidates}
    memory = AbsoluteMemoryMonitor(
        max_process_rss_mib=min(
            candidate.config.performance.max_process_rss_mib
            for candidate in candidates
            if candidate.config.performance.max_process_rss_mib is not None
        )
    )
    base_seconds = duration_seconds(base_interval)
    delay = timedelta(
        seconds=candidates[0].config.schedule.signal_delay_bars * base_seconds
    )
    for chunk in plan:
        signal_start = chunk.start - delay
        signal_end = chunk.end - delay
        selection_part = selections.filter(
            (pl.col("signal_time") >= signal_start)
            & (pl.col("signal_time") < signal_end)
        ).collect(engine="streaming")
        ranking_part = rankings.filter(
            (pl.col("timestamp") >= signal_start)
            & (pl.col("timestamp") < chunk.end)
        ).collect(engine="streaming")
        dependency_symbols = set(str(item) for item in selection_part["symbol"].to_list())
        for checkpoint in checkpoints.values():
            if checkpoint is not None:
                dependency_symbols.update(
                    str(item) for item in checkpoint.position.positions["symbol"].to_list()
                )
        trade_part = trade_bars.filter(
            (pl.col("open_time") >= chunk.start)
            & (pl.col("open_time") < chunk.end)
        )
        mark_part = (
            mark_bars.filter(
                (pl.col("open_time") >= chunk.start)
                & (pl.col("open_time") < chunk.end)
            )
            if mark_bars is not None else None
        )
        funding_part = (
            funding.filter(
                (pl.col("funding_time") >= chunk.start)
                & (pl.col("funding_time") <= chunk.end)
            )
            if funding is not None else None
        )
        if dependency_symbols:
            symbols = sorted(dependency_symbols)
            trade_part = trade_part.filter(pl.col("symbol").is_in(symbols))
            if mark_part is not None:
                mark_part = mark_part.filter(pl.col("symbol").is_in(symbols))
            if funding_part is not None:
                funding_part = funding_part.filter(pl.col("symbol").is_in(symbols))
        shared_trade = trade_part.collect(engine="streaming")
        shared_mark = mark_part.collect(engine="streaming") if mark_part is not None else None
        shared_funding = (
            funding_part.collect(engine="streaming") if funding_part is not None else None
        )
        memory.checkpoint(phase="shared_input", ordinal=chunk.ordinal)
        for candidate in candidates:
            targets, portfolio_version = portfolios[candidate.name]
            target_part = targets.filter(
                (pl.col("signal_time") >= signal_start)
                & (pl.col("signal_time") < signal_end)
            ).collect(engine="streaming")
            result = run_v2_backtest_chunk(
                selection_part.lazy(),
                target_part.lazy(),
                ranking_part.lazy(),
                shared_trade.lazy(),
                shared_mark.lazy() if shared_mark is not None else None,
                shared_funding.lazy() if shared_funding is not None else None,
                config=candidate.config,
                base_interval=base_interval,
                portfolio_version=portfolio_version,
                bars_dataset_version=bars_dataset_version,
                mark_dataset_version=mark_dataset_version,
                funding_dataset_version=funding_dataset_version,
                checkpoint=checkpoints[candidate.name],
                finalize=False,
            )
            checkpoints[candidate.name] = result.checkpoint
            hashes[candidate.name].append(result.result.result_hash)
            trade_counts[candidate.name] += result.linked_trades.height
            warnings[candidate.name].update(result.result.warnings)
            returns = result.result.returns.select("equity", "drawdown").collect()
            if returns.height:
                endings[candidate.name] = float(returns.item(-1, "equity"))
                drawdowns[candidate.name] = min(
                    drawdowns[candidate.name], float(returns["drawdown"].min())
                )
            memory.checkpoint(
                phase=f"candidate:{candidate.name}", ordinal=chunk.ordinal
            )

    return tuple(
        ReplaySweepResult(
            name=candidate.name,
            result_hash=content_sha256(
                {"candidate": candidate.name, "chunk_hashes": hashes[candidate.name]}
            ),
            ending_equity=endings[candidate.name],
            total_return=(
                endings[candidate.name] / candidate.config.capital.initial_equity - 1.0
            ),
            max_drawdown=drawdowns[candidate.name],
            trade_count=trade_counts[candidate.name],
            warning_count=len(warnings[candidate.name]),
        )
        for candidate in candidates
    )
