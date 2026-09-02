"""Bounded LazyFrame orchestration and checkpoint continuation."""

from __future__ import annotations

from datetime import timedelta

import polars as pl

from bfbt.config.backtest import BacktestConfig
from bfbt.config.durations import duration_seconds
from bfbt.data.hashing import content_sha256
from bfbt.engine.fast_matrix.kernel import run_fast_matrix
from bfbt.engine.fast_matrix.result import MatrixResult
from bfbt.engine.fast_matrix.target_schedule import TargetSchedule


IDENTITY_CHUNK_ROWS = 10_000


def _frame_digest(frame: pl.DataFrame) -> str:
    """Hash bounded row chunks without materializing a whole Python row catalog."""

    return content_sha256([
        {
            "row_count": part.height,
            "sha256": content_sha256([
                {
                    name: value.isoformat() if hasattr(value, "isoformat") else value
                    for name, value in row.items()
                }
                for row in part.to_dicts()
            ]),
        }
        for part in frame.iter_slices(IDENTITY_CHUNK_ROWS)
    ])


def _window(frame: pl.LazyFrame, column: str, start, end) -> pl.LazyFrame:
    return frame.filter((pl.col(column) >= start) & (pl.col(column) < end))


def run_fast_matrix_chunked(
    schedule: TargetSchedule,
    trade_bars: pl.LazyFrame,
    *, config: BacktestConfig, market_identity: str,
    mark_bars: pl.LazyFrame | None = None,
    funding: pl.LazyFrame | None = None,
) -> MatrixResult:
    """Scan only one configured time block at once and carry compact state."""

    performance = config.performance
    if performance.mode != "chunked":
        raise ValueError("chunked Fast Matrix requires performance.mode=chunked")
    bounds = trade_bars.select(
        pl.col("open_time").min().alias("start"),
        pl.col("close_time").max().alias("terminal"),
    ).collect(engine="streaming").row(0, named=True)
    if bounds["start"] is None:
        raise ValueError("trade bars are empty")
    covered = set(
        trade_bars.filter(
            pl.col("open_time").cast(pl.Datetime("ms", "UTC")).is_in(
                pl.Series("rebalance_time", schedule.rebalance_times, dtype=pl.Datetime("ms", "UTC")).implode()
            )
        )
        .select("open_time").unique().collect(engine="streaming")["open_time"].to_list()
    )
    if set(schedule.rebalance_times) - covered:
        raise ValueError("rebalance times are outside the executable market input")
    step = timedelta(seconds=duration_seconds(performance.chunk_interval))
    cursor, terminal = bounds["start"], bounds["terminal"]
    checkpoint = None
    returns: list[pl.DataFrame] = []
    rebalances: list[pl.DataFrame] = []
    chunks = 0
    while cursor < terminal:
        end = min(cursor + step, terminal)
        chunk_rebalances = tuple(
            value for value in schedule.rebalance_times
            if cursor <= value < end
        )
        chunk_schedule = TargetSchedule(
            frame=schedule.frame.filter(
                (pl.col("fill_time") >= cursor) & (pl.col("fill_time") < end)
            ),
            rebalance_times=chunk_rebalances,
            schedule_id=schedule.schedule_id,
            parent_manifest_sha256=schedule.parent_manifest_sha256,
        )
        result = run_fast_matrix(
            chunk_schedule, _window(trade_bars, "open_time", cursor, end),
            config=config, market_identity=market_identity,
            mark_bars=None if mark_bars is None else _window(mark_bars, "open_time", cursor, end),
            funding=None if funding is None else funding.filter(
                (pl.col("funding_time") > cursor) & (pl.col("funding_time") <= end)
            ),
            checkpoint=checkpoint, finalize=end >= terminal,
            max_market_rows=performance.max_input_rows_per_chunk,
            validate_schedule_coverage=False,
        )
        checkpoint = result.checkpoint
        returns.append(result.returns)
        rebalances.append(result.rebalance_summary)
        chunks += 1
        cursor = end
    assert checkpoint is not None
    merged_returns = pl.concat(returns, how="vertical")
    merged_rebalances = pl.concat(rebalances, how="vertical")
    digest = content_sha256({
        "run_id": result.run_id,
        "chunk_count": chunks,
        "returns_sha256": _frame_digest(merged_returns),
        "rebalances_sha256": _frame_digest(merged_rebalances),
        "terminal_equity": checkpoint.previous_equity,
        "sequence": checkpoint.sequence,
    })
    return MatrixResult(
        run_id=result.run_id, result_hash=digest, returns=merged_returns,
        rebalance_summary=merged_rebalances, checkpoint=checkpoint,
        warnings=(), diagnostics={
            **result.diagnostics, "execution_mode": "chunked",
            "chunk_count": chunks,
            "max_rows_per_chunk": max(item.height for item in returns),
        },
    )
