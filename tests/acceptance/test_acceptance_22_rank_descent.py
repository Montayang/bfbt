from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from bianbt.config.backtest import PortfolioV2Config, RankSelectionConfig
from bianbt.portfolio.constraints import construct_portfolio
from bianbt.portfolio.history import (
    RankDescentTracker,
    RankStateBudgetExceeded,
)

START = datetime(2026, 6, 1, tzinfo=timezone.utc)
FACTOR_VERSION = "momentum-a22"
UNIVERSE_VERSION = "universe-a22"


def _selection(start: int = 5, top_n: int = 5) -> RankSelectionConfig:
    return RankSelectionConfig.model_validate(
        {
            "mode": "rank_descent",
            "clock": "factor",
            "lag": 0,
            "descent": {
                "start_rank_at_least": start,
                "entry_rank": 1,
                "equal_policy": "keep",
                "increase_policy": "reset",
            },
            "audit_top_n": top_n,
        }
    )


def _rankings(path: list[int], *, symbol: str = "TARGET") -> pl.LazyFrame:
    rows = []
    for index, rank in enumerate(path):
        rows.append(
            {
                "timestamp": START + timedelta(minutes=index),
                "rank_clock": "factor",
                "symbol": symbol,
                "factor_name": "momentum",
                "raw_score": 1.0 / rank,
                "ordinal_rank": rank,
                "percentile_rank": 1.0 - (rank - 1) / 9,
                "sample_count": 10,
                "factor_version": FACTOR_VERSION,
                "universe_version": UNIVERSE_VERSION,
                "run_id": "",
            }
        )
    return pl.DataFrame(
        rows,
        schema_overrides={"timestamp": pl.Datetime("ms", "UTC")},
    ).lazy()


def _run(path: list[int], *, start: int = 5):
    selection = _selection(start=start)
    tracker = RankDescentTracker(
        config=selection.descent,
        max_state_rows=100,
    )
    rankings = _rankings(path)
    selected, diagnostics = tracker.select(
        rankings,
        decision_times=rankings.select("timestamp"),
        selection=selection,
    )
    return tracker, selected.collect(), diagnostics.collect()


@pytest.mark.parametrize(
    "path",
    ([5, 4, 3, 2, 1], [5, 3, 1], [6, 4, 2, 1], [6, 6, 4, 4, 1]),
)
def test_a22_valid_nonincreasing_paths_trigger_once(path: list[int]) -> None:
    tracker, selected, diagnostics = _run(path)
    assert selected.select("symbol", "ordinal_rank").rows() == [("TARGET", 1)]
    assert diagnostics["reason_code"].to_list() == ["RANK_DESCENT_TRIGGERED"]
    assert tracker.stats.state_rows == 0


def test_a22_numeric_rank_increase_resets_sequence() -> None:
    tracker, selected, diagnostics = _run([5, 3, 4, 2, 1])
    assert selected.is_empty()
    assert diagnostics.is_empty()
    assert tracker.stats.state_rows == 0


def test_a22_missing_symbol_resets_and_chunk_restore_is_exact() -> None:
    selection = _selection()
    first = RankDescentTracker(config=selection.descent, max_state_rows=100)
    before = _rankings([6, 4])
    selected_before, _ = first.select(
        before,
        decision_times=before.select("timestamp"),
        selection=selection,
    )
    assert selected_before.collect().is_empty()
    restored = RankDescentTracker(
        config=selection.descent,
        max_state_rows=100,
        restored_state=first.export_state(),
    )
    after = _rankings([2, 1]).with_columns(
        (pl.col("timestamp") + pl.duration(minutes=2)).alias("timestamp")
    )
    selected_after, _ = restored.select(
        after,
        decision_times=after.select("timestamp"),
        selection=selection,
    )
    _, full, _ = _run([6, 4, 2, 1])
    assert_frame_equal(selected_after.collect(), full)

    missing = RankDescentTracker(config=selection.descent, max_state_rows=100)
    opening = _rankings([5])
    missing.select(
        opening,
        decision_times=opening.select("timestamp"),
        selection=selection,
    )
    other = _rankings([1], symbol="OTHER").with_columns(
        (pl.col("timestamp") + pl.duration(minutes=1)).alias("timestamp")
    )
    missing.select(
        other,
        decision_times=other.select("timestamp"),
        selection=selection,
    )
    final = _rankings([1]).with_columns(
        (pl.col("timestamp") + pl.duration(minutes=2)).alias("timestamp")
    )
    no_trigger, _ = missing.select(
        final,
        decision_times=final.select("timestamp"),
        selection=selection,
    )
    assert no_trigger.collect().is_empty()


def test_a22_full_rank_drives_state_while_only_top_n_is_published() -> None:
    symbols = [f"S{index}" for index in range(1, 7)]
    rows = []
    for minute in range(2):
        target_values = (
            [6.0, 5.0, 4.0, 3.0, 2.0, 1.0]
            if minute == 0
            else [5.0, 4.0, 3.0, 2.0, 1.0, 6.0]
        )
        for symbol, value in zip(symbols, target_values):
            rows.append(
                {
                    "timestamp": START + timedelta(minutes=minute),
                    "symbol": symbol,
                    "value": value,
                    "is_valid": True,
                    "factor_version": FACTOR_VERSION,
                    "universe_version": UNIVERSE_VERSION,
                }
            )
    scores = pl.DataFrame(
        rows,
        schema_overrides={"timestamp": pl.Datetime("ms", "UTC")},
    ).lazy()
    config = PortfolioV2Config.model_validate(
        {
            "selection": _selection(start=6).model_dump(mode="json"),
            "sizing": {
                "mode": "equity_fraction",
                "fraction": 1.0,
                "reverse_policy": "flatten_then_open",
            },
            "constraints": {},
        }
    )
    result = construct_portfolio(
        scores,
        config,
        factor_name="momentum",
        rank_scores=scores,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )
    assert result.rankings is not None
    assert result.rankings.collect()["ordinal_rank"].max() == 5
    assert result.selections is not None
    assert result.selections.collect().select("symbol", "ordinal_rank").rows() == [
        ("S6", 1)
    ]


def test_a22_rank_six_is_configuration_not_engine_code() -> None:
    _, selected, _ = _run([6, 3, 1], start=6)
    assert selected.height == 1
    with pytest.raises(ValueError, match="entry_rank"):
        RankSelectionConfig.model_validate(
            {
                "mode": "rank_descent",
                "descent": {"start_rank_at_least": 2, "entry_rank": 2},
            }
        )


def test_a22_state_budget_is_o_symbols_and_hard_limited() -> None:
    selection = _selection()
    tracker = RankDescentTracker(
        config=selection.descent,
        max_state_rows=1,
    )
    frame = pl.concat([_rankings([5]).collect(), _rankings([6], symbol="B").collect()])
    with pytest.raises(RankStateBudgetExceeded, match="max_rank_state_rows"):
        tracker.select(
            frame.lazy(),
            decision_times=frame.lazy().select("timestamp"),
            selection=selection,
        )
