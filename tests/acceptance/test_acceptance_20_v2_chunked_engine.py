"""User-run A20 acceptance for recoverable V2 time-chunk execution."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

import bianbt.engine.v2_chunked as chunked_module
from bianbt.config.backtest import BacktestConfig
from bianbt.data.v2_contracts import V2ReasonCode
from bianbt.engine.v2 import run_v2_backtest
from bianbt.engine.v2_chunked import run_v2_backtest_chunked
from bianbt.performance.diagnostics import MemoryBudgetExceeded
from bianbt.performance.memory import WorkerMemorySupervisor

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
END = START + timedelta(minutes=7)
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT")
VERSION = "bars-a20"
PORTFOLIO_VERSION = "portfolio-a20"
UTC_MS = pl.Datetime("ms", "UTC")


def _config(*, mode: str, sparse: bool = False) -> BacktestConfig:
    performance = {
        "mode": mode,
        "chunk_interval": "3m",
        "max_input_rows_per_chunk": 10_000,
        "max_incremental_rss_mib": 512,
        "collect_diagnostics": True,
        "max_rank_lag": 2,
        "max_rank_state_rows": 100,
        "max_position_state_rows": 10,
        "max_pending_instructions": 20,
        "max_risk_state_rows": 10,
        "max_pending_risk_intents": 20,
        "sparse_execution": sparse,
    }
    if mode == "chunked":
        performance.update(
            {
                "max_process_rss_mib": 5632,
                "resume_policy": "resume",
            }
        )
    return BacktestConfig.model_validate(
        {
            "config_version": "v2",
            "run": {
                "name": "a20-offline",
                "start": START,
                "end": END - timedelta(minutes=1),
                "dataset_version": "snapshot-a20",
                "random_seed": 42,
            },
            "schedule": {
                "factor_interval": "1m",
                "rebalance_interval": "1m",
                "signal_delay_bars": 1,
            },
            "capital": {
                "currency": "USDT",
                "initial_equity": 1000.0,
                "margin_model": "simple_cross",
                "reserved_cost_buffer": 10.0,
            },
            "portfolio": {
                "selection": {
                    "mode": "rank_set",
                    "rank_order": "descending",
                    "clock": "rebalance",
                    "lag": 1,
                    "long": {"ranks": [2], "ranges": []},
                    "short": {"ranks": [1], "ranges": []},
                },
                "sizing": {
                    "mode": "fixed_margin",
                    "margin_amount": 100.0,
                    "reverse_policy": "flatten_then_open",
                },
                "constraints": {
                    "max_gross_exposure": 1.5,
                    "max_net_exposure": 1.0,
                    "max_symbol_weight": 0.8,
                    "max_symbol_notional": 600.0,
                    "max_consecutive_adds": 3,
                    "max_turnover": 1.0,
                },
            },
            "execution": {
                "fill_price": "next_bar_open",
                "partial_fill": False,
                "fee": {"model": "zero"},
                "slippage": {"model": "zero"},
                "funding": {"enabled": False, "missing_policy": "error"},
            },
            "valuation": {"price": "trade_close"},
            "risk": {
                "leverage": 2.0,
                "enforce_liquidation": False,
                "evaluation_interval": "1m",
                "trigger_price": "trade",
                "fill_model": "next_bar_open",
                "intrabar_conflict": "worst_case",
                "symbol_exits": {
                    "stop_loss": {
                        "enabled": True,
                        "distance": 0.01,
                        "action": "close",
                    },
                    "take_profit": {"enabled": False},
                    "trailing_stop": {"enabled": False},
                },
                "portfolio_exits": {
                    "stop_loss": None,
                    "take_profit": None,
                    "max_drawdown": None,
                },
                "cooldown_bars": 1,
                "reentry_policy": "next_scheduled_rebalance",
                "max_triggers_per_symbol": 1,
            },
            "performance": performance,
        }
    )


def _bars() -> pl.LazyFrame:
    rows = []
    for index in range(7):
        opened = START + timedelta(minutes=index)
        for offset, symbol in enumerate(SYMBOLS):
            price = 100.0 + offset * 20.0
            low = 98.0 if symbol == "BTCUSDT" and index == 2 else price - 0.5
            rows.append(
                {
                    "open_time": opened,
                    "close_time": opened + timedelta(minutes=1),
                    "symbol": symbol,
                    "interval": "1m",
                    "open": price,
                    "high": price + 0.5,
                    "low": low,
                    "close": price,
                    "is_complete": True,
                    "dataset_version": VERSION,
                }
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    ).lazy()


def _strategy() -> pl.LazyFrame:
    rows = [
        {
            "signal_time": START + timedelta(minutes=index),
            "rank_source_time": START + timedelta(minutes=index - 1),
            "symbol": symbol,
            "score": score,
            "side": side,
        }
        for index, symbol, score, side in (
            (0, "BTCUSDT", 2.0, "LONG"),
            (0, "ETHUSDT", 3.0, "SHORT"),
            (1, "BTCUSDT", 2.1, "LONG"),
            (1, "ETHUSDT", 3.1, "SHORT"),
            (2, "BTCUSDT", 2.2, "LONG"),
        )
    ]
    return pl.DataFrame(rows).with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.col("rank_source_time").cast(UTC_MS),
    ).lazy()


def _targets() -> pl.LazyFrame:
    return _strategy().with_columns(
        pl.lit(0.0).alias("unconstrained_weight"),
        pl.lit(0.0).alias("target_weight"),
        pl.lit("INCREMENTAL_SIZING").alias("constraint_flags"),
        pl.lit(PORTFOLIO_VERSION).alias("portfolio_version"),
    )


def _rankings() -> pl.LazyFrame:
    rows = []
    for index in range(3):
        timestamp = START + timedelta(minutes=index)
        for rank, symbol in enumerate(("ETHUSDT", "BTCUSDT", "BNBUSDT"), start=1):
            rows.append(
                {
                    "timestamp": timestamp,
                    "rank_clock": "rebalance",
                    "symbol": symbol,
                    "factor_name": "momentum",
                    "raw_score": float(4 - rank),
                    "ordinal_rank": rank,
                    "percentile_rank": float(3 - rank) / 2.0,
                    "sample_count": 3,
                    "factor_version": "momentum-v1",
                    "universe_version": "universe-v1",
                    "run_id": "placeholder",
                }
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("timestamp").cast(UTC_MS)
    ).lazy()


def _in_memory():
    return run_v2_backtest(
        _strategy(),
        _targets(),
        _rankings(),
        _bars(),
        None,
        None,
        config=_config(mode="in_memory"),
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=VERSION,
        mark_dataset_version=None,
        funding_dataset_version=None,
    )


def _chunked(root: Path, *, sparse: bool = False):
    return run_v2_backtest_chunked(
        _strategy(),
        _targets(),
        _rankings(),
        _bars(),
        None,
        None,
        config=_config(mode="chunked", sparse=sparse),
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=VERSION,
        mark_dataset_version=None,
        funding_dataset_version=None,
        execution_start=START,
        execution_end=END,
        output_root=root,
    )


def _without_ids(frame: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    return frame.select(columns).sort(columns[:2])


def test_a20_chunked_economics_equal_in_memory_across_risk_boundary(
    tmp_path: Path,
) -> None:
    expected = _in_memory()
    actual = _chunked(tmp_path / "runs")
    for table, columns in {
        "targets": [
            "signal_time",
            "symbol",
            "score",
            "side",
            "target_weight",
        ],
        "trades": [
            "fill_time",
            "symbol",
            "sequence",
            "side",
            "notional",
            "status",
        ],
        "positions": [
            "timestamp",
            "symbol",
            "quantity",
            "signed_notional",
            "actual_weight",
            "mark_price",
        ],
        "costs": [
            "timestamp",
            "symbol",
            "fee_cost",
            "slippage_cost",
            "funding_cashflow",
            "total_cost",
        ],
        "returns": [
            "timestamp",
            "net_return",
            "equity",
            "drawdown",
            "turnover",
        ],
    }.items():
        expected_frame = getattr(expected.result, table).collect()
        actual_frame = getattr(actual.result, table).collect()
        assert_frame_equal(
            _without_ids(actual_frame, columns),
            _without_ids(expected_frame, columns),
            check_exact=True,
        )
    cross_boundary = actual.risk_events.filter(
        pl.col("reason_code") == V2ReasonCode.STOP_LOSS_TRIGGERED.value
    ).collect()
    assert cross_boundary.height == 1
    assert cross_boundary.item(0, "fill_time") == START + timedelta(minutes=3)
    assert actual.result.diagnostics["committed_chunks"] == 3


def test_a28_sparse_chunked_reads_dependency_symbols_and_is_economic_equal(
    tmp_path: Path,
) -> None:
    full = _chunked(tmp_path / "full")
    sparse = _chunked(tmp_path / "sparse", sparse=True)
    for table, columns in {
        "trades": ["fill_time", "symbol", "side", "notional", "status"],
        "positions": ["timestamp", "symbol", "quantity", "signed_notional"],
        "costs": ["timestamp", "symbol", "total_cost"],
        "returns": ["timestamp", "net_return", "equity", "drawdown", "turnover"],
    }.items():
        expected = getattr(full.result, table).select(columns).collect()
        actual = getattr(sparse.result, table).select(columns).collect()
        assert_frame_equal(actual, expected, check_exact=True)
    assert (
        sparse.result.diagnostics["input_trade_bar_rows"]
        < full.result.diagnostics["input_trade_bar_rows"]
    )


def test_a30_missing_held_bar_carries_last_close_without_fake_execution(
    tmp_path: Path,
) -> None:
    config_payload = _config(mode="chunked", sparse=True).model_dump(
        mode="python"
    )
    config_payload["risk"]["symbol_exits"]["stop_loss"] = {
        "enabled": False
    }
    config_payload["run"]["end"] = START + timedelta(minutes=4)
    config = BacktestConfig.model_validate(config_payload)
    bars = []
    for index in range(5):
        opened = START + timedelta(minutes=index)
        symbols = ["BTCUSDT"]
        if index < 3:
            symbols.append("HUMAUSDT")
        for symbol in symbols:
            price = 200.0 if symbol == "BTCUSDT" else 100.0
            bars.append(
                {
                    "open_time": opened,
                    "close_time": opened + timedelta(minutes=1),
                    "symbol": symbol,
                    "interval": "1m",
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "is_complete": True,
                    "dataset_version": VERSION,
                }
            )
    bar_frame = pl.DataFrame(bars).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    ).lazy()
    strategy = pl.DataFrame(
        [
            {
                "signal_time": START,
                "rank_source_time": START,
                "symbol": "HUMAUSDT",
                "score": 1.0,
                "side": "LONG",
            },
            {
                "signal_time": START + timedelta(minutes=2),
                "rank_source_time": START + timedelta(minutes=2),
                "symbol": "BTCUSDT",
                "score": 1.0,
                "side": "LONG",
            },
        ]
    ).with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.col("rank_source_time").cast(UTC_MS),
    ).lazy()
    targets = strategy.with_columns(
        pl.lit(0.0).alias("unconstrained_weight"),
        pl.lit(0.0).alias("target_weight"),
        pl.lit("INCREMENTAL_SIZING").alias("constraint_flags"),
        pl.lit(PORTFOLIO_VERSION).alias("portfolio_version"),
    )

    result = run_v2_backtest_chunked(
        strategy,
        targets,
        _rankings(),
        bar_frame,
        None,
        None,
        config=config,
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=VERSION,
        mark_dataset_version=None,
        funding_dataset_version=None,
        execution_start=START,
        execution_end=START + timedelta(minutes=5),
        output_root=tmp_path / "runs",
    )

    trades = result.linked_trades.select("fill_time", "symbol").collect()
    assert trades["symbol"].to_list() == ["HUMAUSDT", "BTCUSDT"]
    terminal_huma = (
        result.result.positions.filter(pl.col("symbol") == "HUMAUSDT")
        .sort("timestamp")
        .collect()
        .tail(1)
    )
    assert terminal_huma.item(0, "mark_price") == 100.0
    assert any(
        warning.startswith("valuation_carry_forward:")
        and warning.endswith(":HUMAUSDT")
        for warning in result.result.warnings
    )
    assert result.checkpoint.last_close_marks["HUMAUSDT"] == 100.0


def test_a20_completed_workspace_resumes_without_recomputing(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    first = _chunked(root)
    second = _chunked(root)
    assert first.result.result_hash == second.result.result_hash
    assert second.result.diagnostics["resumed_chunks"] == 3
    assert second.result.returns.collect().height == 7


def test_a20_interruption_resumes_from_last_committed_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runs"
    real_worker = chunked_module._run_supervised_worker

    def interrupt(request, *, max_process_rss_mib):
        if request.chunk.ordinal == 1:
            raise RuntimeError("simulated server interruption")
        return real_worker(
            request,
            max_process_rss_mib=max_process_rss_mib,
        )

    monkeypatch.setattr(chunked_module, "_run_supervised_worker", interrupt)
    with pytest.raises(RuntimeError, match="simulated server interruption"):
        _chunked(root)
    monkeypatch.setattr(
        chunked_module,
        "_run_supervised_worker",
        real_worker,
    )
    resumed = _chunked(root)
    assert resumed.result.diagnostics["resumed_chunks"] == 1
    assert resumed.result.returns.collect().height == 7


def test_a20_v2_chunked_requires_absolute_memory_limit() -> None:
    payload = _config(mode="chunked").model_dump(mode="json")
    payload["performance"]["max_process_rss_mib"] = None
    with pytest.raises(ValueError, match="requires max_process_rss_mib"):
        BacktestConfig.model_validate(payload)


class _FakeProcess:
    pid = 123
    exitcode = None

    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False
        self.exitcode = -15

    def kill(self) -> None:
        self.alive = False
        self.exitcode = -9

    def join(self, timeout=None) -> None:
        return None


def test_a20_parent_supervisor_terminates_worker_above_absolute_limit() -> None:
    process = _FakeProcess()
    supervisor = WorkerMemorySupervisor(
        max_process_rss_mib=512,
        poll_seconds=0.001,
        reader=lambda pid: 513 * 1024 * 1024,
    )
    with pytest.raises(MemoryBudgetExceeded, match="pid=123"):
        supervisor.wait(process)
    assert process.terminated is True
