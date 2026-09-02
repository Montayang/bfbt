"""User-run acceptance suite for A14 bounded historical Rank state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from bfbt.config.backtest import PortfolioV2Config
from bfbt.portfolio.base import PortfolioError
from bfbt.portfolio.constraints import construct_portfolio
from bfbt.portfolio.history import (
    RankHistoryBuffer,
    RankStateBudgetExceeded,
    iter_rank_snapshots,
)
from bfbt.portfolio.ranking import build_rank_snapshots

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    BACKTEST_ROOT
    / "tests"
    / "fixtures"
    / "portfolio"
    / "acceptance_14"
    / "factor_scores.csv"
)
FACTOR_NAME = "fixture_momentum"
FACTOR_VERSION = "factor-a14"
UNIVERSE_VERSION = "universe-a14"


def _scores() -> pl.LazyFrame:
    return pl.scan_csv(FIXTURE, try_parse_dates=True).with_columns(
        pl.col("timestamp").cast(pl.Datetime("ms", "UTC")),
        pl.col("value").cast(pl.Float64),
    )


def _decisions(scores: pl.LazyFrame | None = None) -> pl.LazyFrame:
    source = scores if scores is not None else _scores()
    return source.filter(pl.col("timestamp").dt.hour().is_in([0, 4, 8]))


def _config(*, clock: str, lag: int) -> PortfolioV2Config:
    return PortfolioV2Config.model_validate(
        {
            "selection": {
                "clock": clock,
                "lag": lag,
                "long": {"ranks": [1]},
                "short": {},
            },
            "sizing": {
                "mode": "target_weight",
                "weighting": "equal",
                "target_gross_exposure": 1.0,
                "target_net_exposure": 1.0,
            },
            "constraints": {},
        }
    )


def _construct(
    *,
    clock: str,
    lag: int,
    decisions: pl.LazyFrame | None = None,
    rank_scores: pl.LazyFrame | None = None,
    state: RankHistoryBuffer | None = None,
    max_lag: int = 8,
    max_rows: int = 100,
):
    return construct_portfolio(
        decisions if decisions is not None else _decisions(),
        _config(clock=clock, lag=lag),
        factor_name=FACTOR_NAME,
        rank_scores=rank_scores if rank_scores is not None else _scores(),
        rank_state=state,
        max_rank_lag=max_lag,
        max_rank_state_rows=max_rows,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )


def _diagnostics(result) -> pl.DataFrame:
    assert result.selection_diagnostics is not None
    return result.selection_diagnostics.collect().sort(
        ["decision_time", "side", "requested_rank", "symbol"]
    )


def test_factor_clock_lag_one_uses_strictly_previous_factor_snapshot() -> None:
    result = _construct(clock="factor", lag=1)
    targets = result.frame.collect().sort("signal_time")
    diagnostics = _diagnostics(result)

    assert targets.select("signal_time", "symbol").rows() == [
        (datetime(2026, 2, 1, 4, tzinfo=timezone.utc), "BUSDT"),
        (datetime(2026, 2, 1, 8, tzinfo=timezone.utc), "CUSDT"),
    ]
    selected = diagnostics.filter(
        pl.col("reason_code") == "SELECTED_HISTORICAL_RANK"
    )
    assert selected.select(
        pl.col("decision_time").dt.hour(),
        pl.col("rank_source_time").dt.hour(),
        "symbol",
    ).rows() == [(4, 3, "BUSDT"), (8, 7, "CUSDT")]
    assert (
        selected["rank_source_time"] < selected["decision_time"]
    ).all()


def test_rebalance_clock_lag_one_uses_previous_rebalance_snapshot() -> None:
    result = _construct(
        clock="rebalance",
        lag=1,
        rank_scores=_decisions(),
    )
    targets = result.frame.collect()
    diagnostics = _diagnostics(result)

    assert targets.select(
        pl.col("signal_time").dt.hour(), "symbol"
    ).rows() == [(8, "CUSDT")]
    missing = diagnostics.filter(
        pl.col("reason_code")
        == "HISTORICAL_RANK_NOT_CURRENTLY_ELIGIBLE"
    )
    assert missing.select(
        pl.col("decision_time").dt.hour(),
        pl.col("rank_source_time").dt.hour(),
        "symbol",
    ).rows() == [(4, 0, "AUSDT")]


def test_first_decision_has_insufficient_history_and_never_reads_future() -> None:
    result = _construct(clock="factor", lag=1)
    diagnostics = _diagnostics(result)
    first = diagnostics.filter(pl.col("decision_time").dt.hour() == 0)

    assert first["reason_code"].to_list() == ["INSUFFICIENT_RANK_HISTORY"]
    assert first["rank_source_time"].null_count() == 1
    assert result.frame.collect().filter(
        pl.col("signal_time").dt.hour() == 0
    ).is_empty()


def test_serialized_chunk_boundary_equals_unbroken_execution(
    tmp_path: Path,
) -> None:
    full = _construct(clock="factor", lag=1)
    before = _scores().filter(pl.col("timestamp").dt.hour() < 4)
    after = _scores().filter(pl.col("timestamp").dt.hour() >= 4)

    first = _construct(
        clock="factor",
        lag=1,
        decisions=_decisions(before),
        rank_scores=before,
    )
    assert first.rank_state is not None
    state_path = tmp_path / "rank-state.parquet"
    first.rank_state.export_state().write_parquet(state_path)
    restored = RankHistoryBuffer(
        lag=1,
        max_rank_lag=8,
        max_state_rows=100,
        restored_state=pl.read_parquet(state_path),
    )
    second = _construct(
        clock="factor",
        lag=1,
        decisions=_decisions(after),
        rank_scores=after,
        state=restored,
    )

    chunked_targets = pl.concat(
        [first.frame.collect(), second.frame.collect()]
    ).sort(["signal_time", "symbol"])
    chunked_diagnostics = pl.concat(
        [_diagnostics(first), _diagnostics(second)]
    ).sort(["decision_time", "side", "requested_rank", "symbol"])
    assert_frame_equal(chunked_targets, full.frame.collect())
    assert_frame_equal(chunked_diagnostics, _diagnostics(full))


def test_state_rows_are_o_n_times_fixed_lag() -> None:
    lag_one = _construct(clock="factor", lag=1)
    lag_two = _construct(clock="factor", lag=2)
    assert lag_one.rank_state is not None
    assert lag_two.rank_state is not None

    assert lag_one.rank_state.stats.snapshot_count == 1
    assert lag_one.rank_state.stats.state_rows == 3
    assert lag_two.rank_state.stats.snapshot_count == 2
    assert lag_two.rank_state.stats.state_rows == 6


def test_rank_lag_and_state_row_hard_limits_fail_before_publication() -> None:
    with pytest.raises(RankStateBudgetExceeded, match="max_rank_lag"):
        _construct(clock="factor", lag=2, max_lag=1)
    with pytest.raises(RankStateBudgetExceeded, match="max_rank_state_rows"):
        _construct(clock="factor", lag=2, max_rows=5)


def test_streaming_batches_never_split_a_rank_snapshot() -> None:
    rankings = build_rank_snapshots(
        _scores(),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
        rank_clock="factor",
    )
    snapshots = list(
        iter_rank_snapshots(
            rankings,
            chunk_size=2,
            state_row_limit=100,
            retain_history=True,
        )
    )

    assert len(snapshots) == 9
    assert [item.height for item in snapshots] == [3, 3, 3, 3, 2, 3, 3, 3, 3]
    assert [item["timestamp"][0].hour for item in snapshots] == list(range(9))


def test_duplicate_or_rewound_chunk_state_is_rejected() -> None:
    before = _scores().filter(pl.col("timestamp").dt.hour() < 4)
    first = _construct(
        clock="factor",
        lag=1,
        decisions=_decisions(before),
        rank_scores=before,
    )
    assert first.rank_state is not None
    duplicate_boundary = _scores().filter(
        pl.col("timestamp").dt.hour().is_between(3, 4)
    )

    with pytest.raises(PortfolioError, match="strictly"):
        _construct(
            clock="factor",
            lag=1,
            decisions=_decisions(duplicate_boundary),
            rank_scores=duplicate_boundary,
            state=first.rank_state,
        )
