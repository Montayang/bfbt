"""A41 joint intervals, sparse holdings, and full economic equivalence."""

from __future__ import annotations

from datetime import timedelta

import polars as pl
from polars.testing import assert_frame_equal

from bfbt.config.backtest import BacktestConfig
from bfbt.engine.fast_matrix.batch import run_fast_matrix_batch
from bfbt.engine.fast_matrix.kernel import run_fast_matrix
from tests.acceptance.test_acceptance_34_matrix_research import (
    START,
    UTC_MS,
    _bars,
    _config,
    _schedule,
)


def test_joint_batch_preserves_mark_funding_and_sparse_state_economics() -> None:
    payload = _config().model_dump(mode="python")
    payload["execution"]["funding"] = {
        "enabled": True,
        "missing_policy": "error",
    }
    payload["valuation"] = {"price": "mark_close"}
    config = BacktestConfig.model_validate(payload)
    bars = _bars()
    mark = bars.with_columns((pl.col("close") - 0.25).alias("close"))
    funding = pl.DataFrame(
        {
            "funding_time": [START + timedelta(minutes=2)] * 2,
            "symbol": ["BTCUSDT", "ETHUSDT"],
            "funding_rate": [0.001, -0.0005],
            "mark_price": [102.0, 51.0],
        }
    ).with_columns(pl.col("funding_time").cast(UTC_MS))
    irrelevant = pl.DataFrame(
        [
            {
                "open_time": START + timedelta(minutes=minute),
                "close_time": START + timedelta(minutes=minute + 1),
                "symbol": f"UNUSED{symbol:02d}",
                "open": 10.0 + symbol,
                "close": 10.1 + symbol,
            }
            for minute in range(5)
            for symbol in range(18)
        ]
    ).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    )
    trade = pl.concat([bars, irrelevant]).sort(["open_time", "symbol"])
    mark_all = pl.concat(
        [
            mark,
            irrelevant.with_columns((pl.col("close") - 0.05).alias("close")),
        ]
    ).sort(["open_time", "symbol"])
    candidates = {"base": _schedule(), "half": _schedule(0.5)}
    batch = run_fast_matrix_batch(
        candidates,
        trade.lazy(),
        config=config,
        market_identity="a41-mark-funding",
        mark_bars=mark_all.lazy(),
        funding=funding.lazy(),
    )
    for name, schedule in candidates.items():
        standalone = run_fast_matrix(
            schedule,
            trade,
            config=config,
            market_identity=f"a41-mark-funding:{name}",
            mark_bars=mark_all,
            funding=funding,
        )
        assert_frame_equal(
            batch.candidates[name].returns,
            standalone.returns,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        assert_frame_equal(
            batch.candidates[name].rebalance_summary,
            standalone.rebalance_summary,
            rel_tol=1e-12,
            abs_tol=1e-12,
        )
        assert batch.candidates[name].checkpoint == standalone.checkpoint
        assert batch.candidates[name].result_hash == standalone.result_hash
    assert batch.diagnostics["peak_sparse_position_rows"] == 4
    assert batch.diagnostics["dense_candidate_symbol_cells"] == 40
    assert batch.diagnostics["peak_sparse_state_fraction"] == 0.1
