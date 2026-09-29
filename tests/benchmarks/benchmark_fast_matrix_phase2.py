"""Reproducible offline benchmark for the Fast Matrix phase-two batch path."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from time import perf_counter, process_time

import polars as pl

from bfbt.config.backtest import BacktestConfig
from bfbt.engine.fast_matrix.batch import run_fast_matrix_batch
from bfbt.engine.fast_matrix.kernel import run_fast_matrix
from bfbt.engine.fast_matrix.target_schedule import build_target_schedule

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")
TIME_COUNT = 360
SYMBOL_COUNT = 64
CANDIDATE_COUNT = 6
HELD_PER_CANDIDATE = 8
REBALANCE_EVERY = 30


def _config() -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "config_version": "v2",
            "engine": {"backend": "fast_matrix", "purpose": "research"},
            "schedule": {
                "factor_interval": "1m",
                "rebalance_interval": "30m",
                "signal_delay_bars": 1,
            },
            "portfolio": {
                "selection": {
                    "long": {"ranks": [1]},
                    "short": {"ranks": [2]},
                },
                "sizing": {
                    "mode": "target_weight",
                    "weighting": "equal",
                    "target_gross_exposure": 1.0,
                    "target_net_exposure": 0.0,
                },
            },
            "execution": {
                "fee": {"model": "fixed_bps", "taker_bps": 4.0},
                "slippage": {"model": "fixed_bps", "bps": 2.0},
                "funding": {"enabled": False},
            },
            "valuation": {"price": "trade_close"},
            "risk": {
                "leverage": 2.0,
                "evaluation_interval": "1m",
                "trigger_price": "trade",
                "fill_model": "next_bar_open",
                "intrabar_conflict": "worst_case",
                "reentry_policy": "next_scheduled_rebalance",
            },
            "capital": {"initial_equity": 10_000.0},
            "performance": {"max_input_rows_per_chunk": 100_000},
        }
    )


def _bars() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "open_time": START + timedelta(minutes=minute),
                "close_time": START + timedelta(minutes=minute + 1),
                "symbol": f"S{symbol:03d}",
                "open": 50.0 + symbol + minute * (0.002 + symbol * 0.00001),
                "close": 50.0
                + symbol
                + (minute + 1) * (0.002 + symbol * 0.00001),
            }
            for minute in range(TIME_COUNT)
            for symbol in range(SYMBOL_COUNT)
        ]
    ).with_columns(
        pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS)
    )


def _candidates():
    result = {}
    rebalance_minutes = tuple(range(1, TIME_COUNT, REBALANCE_EVERY))
    for candidate in range(CANDIDATE_COUNT):
        rows = []
        for rebalance_number, fill_minute in enumerate(rebalance_minutes):
            offset = (candidate * HELD_PER_CANDIDATE + rebalance_number) % SYMBOL_COUNT
            selected = [
                (offset + index) % SYMBOL_COUNT
                for index in range(HELD_PER_CANDIDATE)
            ]
            for index, symbol in enumerate(selected):
                rows.append(
                    {
                        "signal_time": START + timedelta(minutes=fill_minute - 1),
                        "fill_time": START + timedelta(minutes=fill_minute),
                        "symbol": f"S{symbol:03d}",
                        "target_weight": (1.0 if index < HELD_PER_CANDIDATE / 2 else -1.0)
                        / HELD_PER_CANDIDATE,
                        "source_signal_id": f"signal-{candidate}",
                        "factor_version": "benchmark-factor",
                        "universe_version": "benchmark-universe",
                        "portfolio_version": "benchmark-portfolio",
                    }
                )
        schedule = build_target_schedule(
            pl.DataFrame(rows),
            rebalance_times=tuple(
                START + timedelta(minutes=minute) for minute in rebalance_minutes
            ),
            parent_manifest_sha256=f"{candidate + 1:064x}",
        )
        result[f"candidate-{candidate:02d}"] = schedule
    return result


def _measure(function):
    wall_started, cpu_started = perf_counter(), process_time()
    value = function()
    return value, perf_counter() - wall_started, process_time() - cpu_started


def main() -> None:
    config, bars, candidates = _config(), _bars(), _candidates()
    independent, independent_wall, independent_cpu = _measure(
        lambda: {
            name: run_fast_matrix(
                schedule,
                bars,
                config=config,
                market_identity=f"phase2-benchmark:{name}",
            )
            for name, schedule in sorted(candidates.items())
        }
    )
    batch, joint_wall, joint_cpu = _measure(
        lambda: run_fast_matrix_batch(
            candidates,
            bars,
            config=config,
            market_identity="phase2-benchmark",
        )
    )
    for name in sorted(candidates):
        assert batch.candidates[name].checkpoint == independent[name].checkpoint
        assert batch.candidates[name].result_hash == independent[name].result_hash
    print(
        json.dumps(
            {
                "shape": {
                    "market_times": TIME_COUNT,
                    "symbols": SYMBOL_COUNT,
                    "market_rows": bars.height,
                    "candidates": CANDIDATE_COUNT,
                    "held_per_candidate": HELD_PER_CANDIDATE,
                    "rebalance_every_bars": REBALANCE_EVERY,
                },
                "independent": {
                    "wall_seconds": independent_wall,
                    "cpu_seconds": independent_cpu,
                },
                "joint": {
                    "wall_seconds": joint_wall,
                    "cpu_seconds": joint_cpu,
                    **batch.diagnostics,
                },
                "speedup": independent_wall / joint_wall,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
