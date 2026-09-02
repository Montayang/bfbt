"""Recoverable time-chunk orchestration for the V2 economic event loop."""

from __future__ import annotations

import multiprocessing
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from queue import Empty
from typing import Mapping, Sequence

import polars as pl

from bfbt.config.backtest import (
    BacktestConfig,
    BacktestPerformanceV2Config,
)
from bfbt.config.common import as_utc
from bfbt.config.durations import duration_seconds
from bfbt.data.hashing import content_sha256
from bfbt.data.v2_contracts import V2ReasonCode
from bfbt.engine.events import (
    ARBITRATION_TRACE_SCHEMA,
    INSTRUCTION_ARTIFACT_SCHEMA,
    link_risk_event_fills_lazy,
)
from bfbt.engine.v2 import (
    LINKED_TRADE_SCHEMA,
    POSITION_V2_SCHEMA,
    V2BacktestResult,
    V2ExecutionCheckpoint,
    run_v2_backtest_chunk,
    v2_engine_run_id,
)
from bfbt.engine.v2_checkpoint import (
    read_v2_execution_checkpoint,
    write_v2_execution_checkpoint,
)
from bfbt.engine.vectorized import (
    COST_SCHEMA,
    RETURN_SCHEMA,
    TARGET_SCHEMA,
    BacktestError,
    BacktestResult,
)
from bfbt.performance.chunks import TimeChunk, plan_time_chunks
from bfbt.performance.diagnostics import PerformanceMonitor
from bfbt.performance.memory import WorkerMemorySupervisor
from bfbt.performance.recovery import (
    V2ChunkCheckpoint,
    V2ChunkRunIdentity,
    V2ChunkWorkspace,
    V2WorkspaceError,
)
from bfbt.risk.state_machine import RISK_EVENT_SCHEMA

V2_CHUNKED_ENGINE_VERSION = "a30-chunked-v2-carry-forward-valuation"
UTC_MS = pl.Datetime("ms", "UTC")

_OUTPUT_SCHEMAS: dict[str, Mapping[str, pl.DataType]] = {
    "targets": TARGET_SCHEMA,
    "trades": LINKED_TRADE_SCHEMA,
    "positions": POSITION_V2_SCHEMA,
    "costs": COST_SCHEMA,
    "returns": RETURN_SCHEMA,
    "instructions": INSTRUCTION_ARTIFACT_SCHEMA,
    "risk_events": RISK_EVENT_SCHEMA,
    "arbitration_trace": ARBITRATION_TRACE_SCHEMA,
}


def _checked_time(value: datetime, *, field: str) -> datetime:
    try:
        checked = as_utc(value)
    except ValueError as exc:
        raise BacktestError(f"{field} must be expressed in UTC") from exc
    assert checked is not None
    return checked


def _count(frame: pl.LazyFrame | None) -> int:
    if frame is None:
        return 0
    return int(
        frame.select(pl.len().alias("rows"))
        .collect(engine="streaming")
        .item()
    )


def _filter_time(
    frame: pl.LazyFrame,
    column: str,
    *,
    start: datetime,
    end: datetime,
    inclusive_end: bool = False,
) -> pl.LazyFrame:
    upper = (
        pl.col(column) <= pl.lit(end)
        if inclusive_end
        else pl.col(column) < pl.lit(end)
    )
    return frame.filter((pl.col(column) >= pl.lit(start)) & upper)


def _scan_output(
    workspace: V2ChunkWorkspace,
    checkpoints: Sequence[V2ChunkCheckpoint],
    table: str,
) -> pl.LazyFrame:
    paths = [
        workspace.chunks_root
        / f"chunk-{checkpoint.ordinal:06d}"
        / "outputs"
        / f"{table}.parquet"
        for checkpoint in checkpoints
    ]
    if not paths:
        return pl.DataFrame(schema=dict(_OUTPUT_SCHEMAS[table])).lazy()
    return pl.concat(
        [pl.scan_parquet(path, hive_partitioning=False) for path in paths],
        how="vertical",
    )


def _part_identity(
    checkpoints: Sequence[V2ChunkCheckpoint],
    *,
    tables: frozenset[str],
) -> dict[str, list[str]]:
    output = {table: [] for table in sorted(tables)}
    for checkpoint in checkpoints:
        for item in checkpoint.output_parts:
            table = Path(item.relative_path).stem
            if table in output:
                output[table].append(item.sha256)
    return output


def _checkpoint_counters(
    checkpoint: V2ExecutionCheckpoint,
    *,
    input_rows: Mapping[str, int],
    worker_peak_rss_kib: int,
) -> dict[str, int]:
    return {
        "engine_sequence": checkpoint.sequence,
        "position_sequence": checkpoint.position.sequence,
        "risk_sequence": checkpoint.risk.sequence,
        "risk_evaluation_count": checkpoint.risk.evaluation_count,
        "position_state_rows": checkpoint.position.position_state_rows,
        "risk_state_rows": checkpoint.risk.risk_state_rows,
        "pending_risk_intents": checkpoint.risk.pending_intent_rows,
        "worker_peak_rss_kib": worker_peak_rss_kib,
        **{f"input_{name}_rows": rows for name, rows in input_rows.items()},
    }


def _write_chunk_outputs(
    transaction,
    result: V2BacktestResult,
) -> None:
    transaction.write_output_frame("targets", result.result.targets.collect())
    transaction.write_output_frame("trades", result.linked_trades)
    transaction.write_output_frame("positions", result.result.positions.collect())
    transaction.write_output_frame("costs", result.result.costs.collect())
    transaction.write_output_frame("returns", result.result.returns.collect())
    transaction.write_output_frame(
        "instructions", result.position_instructions
    )
    transaction.write_output_frame("risk_events", result.risk_events)
    transaction.write_output_frame(
        "arbitration_trace", result.arbitration_trace
    )


def _resume_checkpoint(
    workspace: V2ChunkWorkspace,
    committed: Sequence[V2ChunkCheckpoint],
) -> V2ExecutionCheckpoint | None:
    if not committed:
        return None
    last = committed[-1]
    directory = workspace.chunks_root / f"chunk-{last.ordinal:06d}"
    return read_v2_execution_checkpoint(directory)


@dataclass(frozen=True)
class _ChunkWorkerRequest:
    strategy: pl.LazyFrame
    targets: pl.LazyFrame
    rankings: pl.LazyFrame
    trade_bars: pl.LazyFrame
    mark_bars: pl.LazyFrame | None
    funding: pl.LazyFrame | None
    config: BacktestConfig
    base_interval: str
    portfolio_version: str
    bars_dataset_version: str
    mark_dataset_version: str | None
    funding_dataset_version: str | None
    output_root: Path
    identity: V2ChunkRunIdentity
    plan: tuple[TimeChunk, ...]
    chunk: TimeChunk
    input_rows: dict[str, int]


def _execute_chunk_worker(request: _ChunkWorkerRequest) -> None:
    workspace = V2ChunkWorkspace(
        output_root=request.output_root,
        identity=request.identity,
    )
    committed = workspace.committed(request.plan)
    if len(committed) != request.chunk.ordinal:
        raise V2WorkspaceError(
            "worker checkpoint count does not match requested chunk"
        )
    state = _resume_checkpoint(workspace, committed)
    monitor = PerformanceMonitor(
        mode="chunked_v2_worker",
        chunk_interval=request.config.performance.chunk_interval,
        max_input_rows_per_chunk=(
            request.config.performance.max_input_rows_per_chunk
        ),
        max_incremental_rss_mib=(
            request.config.performance.max_incremental_rss_mib
        ),
    )
    started_at = monitor.start()
    monitor.check_rows(request.input_rows)
    result = run_v2_backtest_chunk(
        request.strategy,
        request.targets,
        request.rankings,
        request.trade_bars,
        request.mark_bars,
        request.funding,
        config=request.config,
        base_interval=request.base_interval,
        portfolio_version=request.portfolio_version,
        bars_dataset_version=request.bars_dataset_version,
        mark_dataset_version=request.mark_dataset_version,
        funding_dataset_version=request.funding_dataset_version,
        checkpoint=state,
        finalize=False,
    )
    transaction = workspace.begin(request.chunk)
    write_v2_execution_checkpoint(transaction, result.checkpoint)
    _write_chunk_outputs(transaction, result)
    output_rows = {
        "targets": result.result.targets.collect().height,
        "trades": result.linked_trades.height,
        "positions": result.result.positions.collect().height,
        "costs": result.result.costs.collect().height,
        "returns": result.result.returns.collect().height,
        "instructions": result.position_instructions.height,
        "risk_events": result.risk_events.height,
        "arbitration_trace": result.arbitration_trace.height,
    }
    monitor.checkpoint(
        phase="execution",
        ordinal=request.chunk.ordinal,
        start=request.chunk.start,
        end=request.chunk.end,
        input_rows=request.input_rows,
        output_rows=output_rows,
        started_at=started_at,
    )
    worker_peak_rss_kib = int(
        monitor.result().observed_peak_rss_mib * 1_024
    )
    transaction.commit(
        counters=_checkpoint_counters(
            result.checkpoint,
            input_rows=request.input_rows,
            worker_peak_rss_kib=worker_peak_rss_kib,
        )
    )


def _worker_entry(request: _ChunkWorkerRequest, errors) -> None:
    try:
        _execute_chunk_worker(request)
    except BaseException:
        errors.put(traceback.format_exc())
        raise


def _run_supervised_worker(
    request: _ChunkWorkerRequest,
    *,
    max_process_rss_mib: int,
) -> float:
    context = multiprocessing.get_context("spawn")
    errors = context.Queue(maxsize=1)
    process = context.Process(
        target=_worker_entry,
        args=(request, errors),
        name=f"bfbt-v2-chunk-{request.chunk.ordinal:06d}",
    )
    process.start()
    result = WorkerMemorySupervisor(
        max_process_rss_mib=max_process_rss_mib
    ).wait(process)
    if result.exitcode != 0:
        try:
            detail = errors.get(timeout=1.0)
        except Empty:
            detail = f"worker exited with code {result.exitcode}"
        raise BacktestError(
            f"V2 chunk worker {request.chunk.ordinal} failed:\n{detail}"
        )
    errors.close()
    errors.join_thread()
    return result.observed_peak_rss_mib


def _finalize_risk_events(
    events: pl.LazyFrame,
    trades: pl.LazyFrame,
    instructions: pl.LazyFrame,
    *,
    checkpoint: V2ExecutionCheckpoint,
) -> tuple[pl.LazyFrame, tuple[str, ...]]:
    risk_events = events
    warnings = set(checkpoint.warnings)
    terminal = checkpoint.risk.pending_intents
    if terminal.height:
        terminal_ids = terminal["event_id"].unique().to_list()
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
        warnings.add(f"risk_end_of_data_unfilled:{terminal.height}")
    risk_events = link_risk_event_fills_lazy(
        risk_events,
        trades,
        run_id=checkpoint.run_id,
        position_instructions=instructions,
    )
    return risk_events, tuple(sorted(warnings))


def run_v2_backtest_chunked(
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
    execution_start: datetime,
    execution_end: datetime,
    output_root: Path,
) -> V2BacktestResult:
    """Run or resume V2 slices while keeping full history in committed parts."""

    if config.config_version != "v2":
        raise BacktestError("V2 chunked execution requires config_version=v2")
    performance = config.performance
    assert isinstance(performance, BacktestPerformanceV2Config)
    if performance.mode != "chunked":
        raise BacktestError("V2 chunked execution requires performance.mode=chunked")
    if performance.max_process_rss_mib is None:
        raise BacktestError("V2 chunked execution requires max_process_rss_mib")
    start = _checked_time(execution_start, field="execution_start")
    end = _checked_time(execution_end, field="execution_end")
    if end <= start:
        raise BacktestError("execution_end must be greater than execution_start")
    base_seconds = duration_seconds(base_interval)
    chunk_seconds = duration_seconds(performance.chunk_interval)
    risk_seconds = duration_seconds(config.risk.evaluation_interval)
    if chunk_seconds % base_seconds or chunk_seconds % risk_seconds:
        raise BacktestError(
            "chunk_interval must be an integer multiple of base and risk intervals"
        )
    plan = plan_time_chunks(
        start=start,
        end=end,
        chunk_interval=performance.chunk_interval,
    )
    run_id = v2_engine_run_id(
        config=config,
        portfolio_version=portfolio_version,
        bars_dataset_version=bars_dataset_version,
        mark_dataset_version=mark_dataset_version,
        funding_dataset_version=funding_dataset_version,
        base_interval=base_interval,
    )
    dataset_identity = {
        "portfolio_version": portfolio_version,
        "bars_dataset_version": bars_dataset_version,
        "mark_dataset_version": mark_dataset_version,
        "funding_dataset_version": funding_dataset_version,
        "base_interval": base_interval,
    }
    identity = V2ChunkRunIdentity.from_plan(
        run_id=run_id,
        engine_version=V2_CHUNKED_ENGINE_VERSION,
        config_sha256=content_sha256(config),
        dataset_sha256=content_sha256(dataset_identity),
        chunk_interval=performance.chunk_interval,
        overlap_seconds=0,
        chunks=plan,
    )
    workspace = V2ChunkWorkspace(output_root=output_root, identity=identity)
    committed = workspace.committed(plan)
    if performance.resume_policy == "error_if_exists" and (
        committed or any(workspace.staging_root.iterdir())
    ):
        raise V2WorkspaceError(
            "V2 workspace already has committed or staged chunks"
        )
    resume_count = len(committed)
    state = _resume_checkpoint(workspace, committed)
    if state is not None and state.run_id != run_id:
        raise V2WorkspaceError("restored V2 engine run_id does not match")

    row_monitor = PerformanceMonitor(
        mode="chunked_v2_parent",
        chunk_interval=performance.chunk_interval,
        max_input_rows_per_chunk=performance.max_input_rows_per_chunk,
        max_incremental_rss_mib=performance.max_incremental_rss_mib,
    )
    worker_peaks: list[float] = []
    delay = timedelta(
        seconds=config.schedule.signal_delay_bars * base_seconds
    )
    for chunk in plan[resume_count:]:
        signal_start = chunk.start - delay
        signal_end = chunk.end - delay
        strategy_part = _filter_time(
            strategy,
            "signal_time",
            start=signal_start,
            end=signal_end,
        )
        targets_part = _filter_time(
            targets,
            "signal_time",
            start=signal_start,
            end=signal_end,
        )
        rankings_part = _filter_time(
            rankings,
            "timestamp",
            start=signal_start,
            end=chunk.end,
        )
        trade_part = _filter_time(
            trade_bars,
            "open_time",
            start=chunk.start,
            end=chunk.end,
        )
        mark_part = (
            _filter_time(
                mark_bars,
                "open_time",
                start=chunk.start,
                end=chunk.end,
            )
            if mark_bars is not None
            else None
        )
        funding_part = (
            _filter_time(
                funding,
                "funding_time",
                start=chunk.start,
                end=chunk.end,
                inclusive_end=True,
            )
            if funding is not None
            else None
        )
        if performance.sparse_execution:
            dependency_symbols = set(
                str(symbol)
                for symbol in strategy_part.select("symbol")
                .unique()
                .collect(engine="streaming")["symbol"]
                .to_list()
            )
            if state is not None:
                dependency_symbols.update(
                    str(symbol)
                    for symbol in state.position.positions["symbol"].to_list()
                )
            if dependency_symbols:
                ordered_symbols = sorted(dependency_symbols)
                trade_part = trade_part.filter(
                    pl.col("symbol").is_in(ordered_symbols)
                )
                if mark_part is not None:
                    mark_part = mark_part.filter(
                        pl.col("symbol").is_in(ordered_symbols)
                    )
                if funding_part is not None:
                    funding_part = funding_part.filter(
                        pl.col("symbol").is_in(ordered_symbols)
                    )
        input_rows = {
            "strategy": _count(strategy_part),
            "targets": _count(targets_part),
            "rankings": _count(rankings_part),
            "trade_bars": _count(trade_part),
            "mark_bars": _count(mark_part),
            "funding": _count(funding_part),
        }
        row_monitor.check_rows(input_rows)
        request = _ChunkWorkerRequest(
            strategy=strategy_part,
            targets=targets_part,
            rankings=rankings_part,
            trade_bars=trade_part,
            mark_bars=mark_part,
            funding=funding_part,
            config=config,
            base_interval=base_interval,
            portfolio_version=portfolio_version,
            bars_dataset_version=bars_dataset_version,
            mark_dataset_version=mark_dataset_version,
            funding_dataset_version=funding_dataset_version,
            output_root=output_root,
            identity=identity,
            plan=plan,
            chunk=chunk,
            input_rows=input_rows,
        )
        worker_peaks.append(
            _run_supervised_worker(
                request,
                max_process_rss_mib=performance.max_process_rss_mib,
            )
        )
        committed = workspace.committed(plan)
        if len(committed) != chunk.ordinal + 1:
            raise V2WorkspaceError(
                "successful worker did not atomically commit its chunk"
            )
        state = _resume_checkpoint(workspace, committed)

    committed = workspace.committed(plan)
    if len(committed) != len(plan):
        raise V2WorkspaceError("V2 execution ended without all chunks committed")
    final_state = _resume_checkpoint(workspace, committed)
    if final_state is None or final_state.risk.evaluation_count == 0:
        raise BacktestError("no complete V2 risk-clock snapshot was evaluated")
    outputs = {
        table: _scan_output(workspace, committed, table)
        for table in _OUTPUT_SCHEMAS
    }
    risk_events, warnings = _finalize_risk_events(
        outputs["risk_events"],
        outputs["trades"],
        outputs["instructions"],
        checkpoint=final_state,
    )
    final_state = replace(final_state, warnings=warnings)
    persisted_worker_peak_mib = max(
        (
            checkpoint.counters.get("worker_peak_rss_kib", 0) / 1_024
            for checkpoint in committed
        ),
        default=0.0,
    )
    result_parts = _part_identity(
        committed,
        tables=frozenset(
            {"targets", "trades", "positions", "costs", "returns"}
        ),
    )
    audit_parts = _part_identity(
        committed,
        tables=frozenset(
            {"instructions", "risk_events", "arbitration_trace", "trades"}
        ),
    )
    result_hash = content_sha256(
        {
            "engine": V2_CHUNKED_ENGINE_VERSION,
            "run_id": run_id,
            "parts": result_parts,
            "warnings": list(warnings),
        }
    )
    audit_hash = content_sha256(
        {
            "engine": V2_CHUNKED_ENGINE_VERSION,
            "run_id": run_id,
            "parts": audit_parts,
            "terminal_risk_event_ids": (
                final_state.risk.pending_intents["event_id"].to_list()
            ),
        }
    )
    diagnostics = {
        "mode": "chunked_v2",
        "chunk_interval": performance.chunk_interval,
        "committed_chunks": len(committed),
        "resumed_chunks": resume_count,
        "max_process_rss_mib": performance.max_process_rss_mib,
        "observed_process_rss_mib": max(
            [persisted_worker_peak_mib, *worker_peaks]
        ),
        "max_position_state_rows_observed": (
            final_state.max_position_state_rows_observed
        ),
        "max_risk_state_rows_observed": (
            final_state.max_risk_state_rows_observed
        ),
        "max_pending_risk_intents_observed": (
            final_state.max_pending_risk_intents_observed
        ),
        "input_trade_bar_rows": final_state.input_trade_bar_rows,
        "input_risk_bar_rows": final_state.input_risk_bar_rows,
    }
    result = BacktestResult(
        run_id=run_id,
        result_hash=result_hash,
        targets=outputs["targets"],
        trades=outputs["trades"],
        positions=outputs["positions"],
        costs=outputs["costs"],
        returns=outputs["returns"],
        warnings=warnings,
        diagnostics=diagnostics,
        presorted=True,
        execution_mode="chunked_v2",
    )
    return V2BacktestResult(
        result=result,
        position_instructions=outputs["instructions"],
        risk_events=risk_events,
        linked_trades=outputs["trades"],
        arbitration_trace=outputs["arbitration_trace"],
        audit_result_hash=audit_hash,
        checkpoint=final_state,
    )
