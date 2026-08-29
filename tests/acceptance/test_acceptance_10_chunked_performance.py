"""Offline acceptance suite for A10 bounded chunk execution."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal
from typer.testing import CliRunner

from bianbt.application.planning import contracts_scan_end
from bianbt.cli import app
from bianbt.config.backtest import BacktestConfig
from bianbt.engine.streaming import StreamingLedger
from bianbt.engine.vectorized import BacktestError, run_vectorized_backtest
from bianbt.performance.chunks import ChunkPlanError, plan_time_chunks
from bianbt.performance.diagnostics import (
    PerformanceMonitor,
    RowBudgetExceeded,
)
from bianbt.performance.spool import (
    ChunkWorkspace,
    cleanup_stale_workspaces,
)
from bianbt.universe.point_in_time import _bar_metrics

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
PORTFOLIO_VERSION = "portfolio-a10-fixture"
BARS_VERSION = "bars-a10-fixture"
FUNDING_VERSION = "funding-a10-fixture"


def _config() -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "schedule": {"signal_delay_bars": 1},
            "portfolio": {
                "long_quantile": 0.5,
                "short_quantile": 0.5,
            },
            "execution": {
                "fee": {"model": "fixed_bps", "taker_bps": 1.0},
                "slippage": {"model": "fixed_bps", "bps": 2.0},
                "funding": {"enabled": True, "missing_policy": "error"},
            },
            "valuation": {"price": "trade_close"},
            "risk": {"leverage": 2.0},
            "performance": {
                "mode": "chunked",
                "chunk_interval": "3m",
                "max_input_rows_per_chunk": 1000,
                "max_incremental_rss_mib": 512,
            },
        }
    )


def _targets() -> pl.LazyFrame:
    rows = []
    for minute, weights in (
        (0, {"A": 0.5, "B": -0.5}),
        (3, {"A": -0.5, "B": 0.5}),
    ):
        signal = START + timedelta(minutes=minute)
        for symbol, weight in weights.items():
            rows.append(
                {
                    "signal_time": signal,
                    "symbol": symbol,
                    "score": weight,
                    "side": "LONG" if weight > 0 else "SHORT",
                    "unconstrained_weight": weight,
                    "target_weight": weight,
                    "constraint_flags": "",
                    "portfolio_version": PORTFOLIO_VERSION,
                }
            )
    return pl.DataFrame(rows).lazy()


def _bars() -> pl.LazyFrame:
    paths = {
        "A": ((100, 100), (100, 102), (102, 103), (103, 101), (101, 99), (99, 98)),
        "B": ((100, 100), (100, 98), (98, 97), (97, 99), (99, 101), (101, 102)),
    }
    rows = []
    for symbol, prices in paths.items():
        for minute, (opened, closed) in enumerate(prices):
            opened_at = START + timedelta(minutes=minute)
            rows.append(
                {
                    "open_time": opened_at,
                    "close_time": opened_at + timedelta(minutes=1),
                    "symbol": symbol,
                    "interval": "1m",
                    "open": float(opened),
                    "close": float(closed),
                    "is_complete": True,
                    "dataset_version": BARS_VERSION,
                }
            )
    return pl.DataFrame(rows).lazy()


def _funding() -> pl.LazyFrame:
    rows = []
    for event, rate in (
        (START + timedelta(minutes=3), 0.001),
        (START + timedelta(minutes=5), -0.0005),
    ):
        for symbol in ("A", "B"):
            rows.append(
                {
                    "funding_time": event,
                    "symbol": symbol,
                    "funding_rate": rate,
                    "mark_price": 100.0,
                    "dataset_version": FUNDING_VERSION,
                }
            )
    return pl.DataFrame(rows).lazy()


def _full_result():
    return run_vectorized_backtest(
        _targets(),
        _bars(),
        None,
        _funding(),
        config=_config(),
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=BARS_VERSION,
        mark_dataset_version=None,
        funding_dataset_version=FUNDING_VERSION,
    )


def _chunked_frames() -> dict[str, pl.DataFrame]:
    ledger = StreamingLedger(
        config=_config(),
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=BARS_VERSION,
        mark_dataset_version=None,
        funding_dataset_version=FUNDING_VERSION,
    )
    outputs: dict[str, list[pl.DataFrame]] = {
        name: [] for name in ("targets", "trades", "positions", "costs", "returns")
    }
    bars = _bars()
    funding = _funding()
    targets = _targets()
    for ordinal, (start, end) in enumerate(
        (
            (START, START + timedelta(minutes=3)),
            (START + timedelta(minutes=3), START + timedelta(minutes=6)),
        )
    ):
        signal_start = start - timedelta(minutes=1)
        signal_end = end - timedelta(minutes=1)
        target_part = targets.filter(
            (pl.col("signal_time") >= signal_start)
            & (pl.col("signal_time") < signal_end)
        )
        bar_part = bars.filter(
            (pl.col("open_time") >= start) & (pl.col("open_time") < end)
        )
        funding_part = funding.filter(
            (pl.col("funding_time") >= start)
            & (pl.col("funding_time") <= end)
        )
        result = ledger.process(target_part, bar_part, None, funding_part)
        for name in outputs:
            outputs[name].append(getattr(result, name))
        assert ledger.state.last_open_time == end - timedelta(minutes=1)
        assert ledger.state.sequence > ordinal
    return {
        name: pl.concat(parts, how="vertical")
        for name, parts in outputs.items()
    }


def _without_run_id(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.drop("run_id")


def test_chunk_planner_has_exact_overlap_and_no_gaps() -> None:
    chunks = plan_time_chunks(
        start=START,
        end=START + timedelta(minutes=8),
        chunk_interval="3m",
        overlap_seconds=120,
        earliest_input=START - timedelta(minutes=2),
    )
    assert len(chunks) == 3
    assert chunks[0].input_start == START - timedelta(minutes=2)
    assert chunks[1].start == chunks[0].end
    assert chunks[1].input_start == chunks[1].start - timedelta(minutes=2)
    assert chunks[-1].end == START + timedelta(minutes=8)


def test_chunk_planner_rejects_invalid_ranges() -> None:
    with pytest.raises(ChunkPlanError, match="greater than start"):
        plan_time_chunks(start=START, end=START, chunk_interval="1m")
    with pytest.raises(ChunkPlanError, match="non-negative"):
        plan_time_chunks(
            start=START,
            end=START + timedelta(minutes=1),
            chunk_interval="1m",
            overlap_seconds=-1,
        )


def test_current_contract_snapshot_is_scanned_when_historical_snapshots_disabled() -> None:
    member_end = START + timedelta(days=10)
    config = SimpleNamespace(
        universe=SimpleNamespace(
            point_in_time=SimpleNamespace(use_contract_snapshots=False)
        ),
        backtest=SimpleNamespace(
            run=SimpleNamespace(end=START + timedelta(days=1))
        ),
    )
    member = SimpleNamespace(available_to=member_end)
    assert contracts_scan_end(config, member) == member_end


def test_historical_contract_snapshot_scan_stops_at_run_end() -> None:
    run_end = START + timedelta(days=1)
    config = SimpleNamespace(
        universe=SimpleNamespace(
            point_in_time=SimpleNamespace(use_contract_snapshots=True)
        ),
        backtest=SimpleNamespace(run=SimpleNamespace(end=run_end)),
    )
    member = SimpleNamespace(available_to=START + timedelta(days=10))
    assert contracts_scan_end(config, member) == run_end


def test_monitor_enforces_row_budget_and_has_stable_artifact() -> None:
    monitor = PerformanceMonitor(
        mode="chunked",
        chunk_interval="3m",
        max_input_rows_per_chunk=1000,
        max_incremental_rss_mib=512,
    )
    started = monitor.start()
    monitor.checkpoint(
        phase="analysis",
        ordinal=0,
        start=START,
        end=START + timedelta(minutes=3),
        input_rows={"bars": 12},
        output_rows={"targets": 4},
        started_at=started,
    )
    artifact = monitor.result().to_artifact_dict()
    assert artifact["memory_budget_passed"] is True
    assert "elapsed_seconds" not in artifact["chunks"][0]
    with pytest.raises(RowBudgetExceeded, match="exceed"):
        monitor.check_rows({"bars": 1001})


def test_spool_preserves_ordered_parts_and_cleans_workspace(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    with ChunkWorkspace(root) as workspace:
        path = workspace.path
        workspace.spool.append_frame("values", pl.DataFrame({"x": [1, 2]}))
        workspace.spool.append_frame("values", pl.DataFrame({"x": [3]}))
        assert workspace.spool.row_count("values") == 3
        assert workspace.spool.scan("values").collect()["x"].to_list() == [1, 2, 3]
    assert not path.exists()


def test_stale_cleanup_never_touches_published_runs(tmp_path: Path) -> None:
    output = tmp_path / "runs"
    stale = output / ".work" / "a10-stale"
    stale.mkdir(parents=True)
    (stale / "workspace.json").write_text(
        json.dumps({"created_unix": 1.0, "pid": 999_999_999}),
        encoding="utf-8",
    )
    published = output / "a09-published"
    published.mkdir(parents=True)
    dry = cleanup_stale_workspaces(
        output, older_than_seconds=10, now_unix=100.0
    )
    assert dry == (stale,)
    assert stale.exists()
    cleanup_stale_workspaces(
        output, older_than_seconds=10, apply=True, now_unix=100.0
    )
    assert not stale.exists()
    assert published.exists()


@pytest.mark.parametrize(
    ("table", "sort_by"),
    [
        ("targets", ["signal_time", "symbol"]),
        ("trades", ["fill_time", "symbol", "sequence"]),
        ("positions", ["timestamp", "symbol"]),
        ("costs", ["timestamp", "symbol"]),
        ("returns", ["timestamp"]),
    ],
)
def test_chunked_ledgers_equal_in_memory_ledgers(
    table: str, sort_by: list[str]
) -> None:
    expected = _without_run_id(getattr(_full_result(), table).collect()).sort(sort_by)
    actual = _without_run_id(_chunked_frames()[table]).sort(sort_by)
    assert_frame_equal(actual, expected, check_exact=True)


def test_streaming_ledger_rejects_overlapping_market_chunks() -> None:
    ledger = StreamingLedger(
        config=_config(),
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=BARS_VERSION,
        mark_dataset_version=None,
        funding_dataset_version=FUNDING_VERSION,
    )
    first = _bars().filter(pl.col("open_time") < START + timedelta(minutes=3))
    ledger.process(
        _targets().filter(pl.col("signal_time") == START),
        first,
        None,
        _funding().filter(pl.col("funding_time") <= START + timedelta(minutes=3)),
    )
    with pytest.raises(BacktestError, match="overlap"):
        ledger.process(
            _targets().filter(pl.col("signal_time") > START),
            first,
            None,
            _funding(),
        )


def test_universe_history_offsets_preserve_absolute_counts() -> None:
    bars = pl.DataFrame(
        {
            "open_time": [START, START + timedelta(minutes=1)],
            "close_time": [
                START + timedelta(minutes=1),
                START + timedelta(minutes=2),
            ],
            "symbol": ["A", "A"],
            "interval": ["1m", "1m"],
            "quote_volume": [10.0, 20.0],
            "is_complete": [True, True],
            "dataset_version": [BARS_VERSION, BARS_VERSION],
        }
    ).lazy()
    prior = START - timedelta(days=10)
    offsets = pl.DataFrame(
        {
            "symbol": ["A"],
            "history_bars_offset": [5],
            "prior_first_bar_open": [prior],
        }
    ).lazy()
    metrics = _bar_metrics(
        bars,
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        history_offsets=offsets,
    ).collect()
    assert metrics["history_bars"].to_list() == [6, 7]
    assert metrics["first_bar_open"].to_list() == [prior, prior]


def test_performance_cli_contracts() -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["performance", "--help"]).exit_code == 0
    planned = runner.invoke(
        app,
        [
            "performance",
            "plan",
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:08:00Z",
            "--chunk-interval",
            "3m",
            "--overlap-seconds",
            "120",
        ],
    )
    assert planned.exit_code == 0
    assert "chunks=3" in planned.stdout
