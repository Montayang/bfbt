"""User-run acceptance suite for A13 current-snapshot exact Rank."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from bfbt.artifacts.store import RunArtifactStore
from bfbt.config.backtest import PortfolioConfig, PortfolioV2Config
from bfbt.data.schemas import get_schema_definition
from bfbt.portfolio.constraints import construct_portfolio
from bfbt.portfolio.ranking import build_rank_snapshots
from bfbt.portfolio.selection import v1_rank_counts

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
SCORES = (
    BACKTEST_ROOT
    / "tests"
    / "fixtures"
    / "portfolio"
    / "acceptance_13"
    / "scores.csv"
)
FACTOR_NAME = "fixture_momentum"
FACTOR_VERSION = "factor-a13"
UNIVERSE_VERSION = "universe-a13"


def _scores() -> pl.LazyFrame:
    return (
        pl.scan_csv(SCORES, try_parse_dates=True)
        .with_columns(
            pl.col("timestamp").cast(pl.Datetime("ms", "UTC")),
            pl.col("value").cast(pl.Float64),
        )
    )


def _v2_portfolio(
    *,
    long: dict[str, object],
    short: dict[str, object],
    lag: int = 0,
) -> PortfolioV2Config:
    return PortfolioV2Config.model_validate(
        {
            "selection": {
                "clock": "rebalance",
                "lag": lag,
                "long": long,
                "short": short,
            },
            "sizing": {
                "mode": "target_weight",
                "weighting": "equal",
                "target_gross_exposure": 1.0,
                "target_net_exposure": 0.0,
            },
            "constraints": {},
        }
    )


def _rankings(scores: pl.LazyFrame | None = None) -> pl.LazyFrame:
    return build_rank_snapshots(
        scores if scores is not None else _scores(),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )


def test_rank_snapshot_has_stable_ties_percentiles_and_sample_counts() -> None:
    rows = _rankings().collect()
    first = rows.filter(pl.col("timestamp") == rows["timestamp"][0])

    assert first["symbol"].to_list() == [
        "BTCUSDT", "ETHUSDT", "XRPUSDT", "ADAUSDT"
    ]
    assert first["ordinal_rank"].to_list() == [1, 2, 3, 4]
    assert first["percentile_rank"].to_list() == pytest.approx(
        [1.0, 2 / 3, 1 / 3, 0.0]
    )
    assert first["sample_count"].to_list() == [4, 4, 4, 4]
    assert rows.group_by("timestamp").len().sort("timestamp")[
        "len"
    ].to_list() == [4, 3, 1]


def test_rank_is_identical_after_input_order_is_reversed() -> None:
    baseline = _rankings().collect()
    reversed_scores = _scores().sort(
        ["timestamp", "symbol"], descending=[True, True]
    )
    repeated = _rankings(reversed_scores).collect()

    assert repeated.equals(baseline)


def test_can_long_rank_2_and_short_rank_1_independently() -> None:
    result = construct_portfolio(
        _scores(),
        _v2_portfolio(long={"ranks": [2]}, short={"ranks": [1]}),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )
    first_time = result.frame.select("signal_time").collect()["signal_time"][0]
    first = result.frame.filter(pl.col("signal_time") == first_time).collect()

    assert first.select("symbol", "side").rows() == [
        ("BTCUSDT", "SHORT"),
        ("ETHUSDT", "LONG"),
    ]
    assert first["target_weight"].to_list() == pytest.approx([-0.5, 0.5])


def test_multiple_ranks_ranges_and_one_sided_selection_are_supported() -> None:
    ranged = construct_portfolio(
        _scores(),
        _v2_portfolio(long={"ranges": [[2, 3]]}, short={"ranks": [1]}),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    first_time = ranged["signal_time"][0]
    assert ranged.filter(pl.col("signal_time") == first_time).select(
        "symbol", "side"
    ).rows() == [
        ("BTCUSDT", "SHORT"),
        ("ETHUSDT", "LONG"),
        ("XRPUSDT", "LONG"),
    ]

    one_sided = construct_portfolio(
        _scores(),
        _v2_portfolio(long={"ranks": [1, 3]}, short={}),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    assert one_sided["side"].unique().to_list() == ["LONG"]


def test_out_of_range_is_audited_and_never_backfilled() -> None:
    result = construct_portfolio(
        _scores(),
        _v2_portfolio(long={"ranks": [4]}, short={"ranks": [1]}),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )
    assert result.selection_diagnostics is not None
    diagnostics = result.selection_diagnostics.collect()

    assert (
        diagnostics.filter(
            pl.col("reason_code") == "SELECTED_CURRENT_RANK"
        )["symbol"].is_not_null().all()
    )
    assert diagnostics.filter(
        pl.col("reason_code") == "RANK_OUT_OF_RANGE"
    ).select(
        "requested_rank", "sample_count", "reason_code"
    ).rows() == [
        (4, 3, "RANK_OUT_OF_RANGE"),
        (4, 1, "RANK_OUT_OF_RANGE"),
    ]
    selected = result.frame.collect()
    later_counts = selected.group_by("signal_time").len().sort("signal_time")
    assert later_counts["len"].to_list() == [2, 1, 1]


def test_v1_adapter_counts_match_legacy_economic_targets() -> None:
    count_config = PortfolioConfig(
        construction="long_short_count", long_count=1, short_count=1
    )
    adapted = pl.DataFrame({"sample_count": [4]}).lazy().select(
        *[
            expression.alias(name)
            for expression, name in zip(
                v1_rank_counts(count_config, pl.col("sample_count")),
                ("long_count", "short_count"),
            )
        ]
    ).collect()
    assert adapted.row(0) == (1, 1)

    legacy = construct_portfolio(
        _scores().filter(
            pl.col("timestamp")
            == pl.datetime(2026, 1, 1, time_zone="UTC")
        ),
        count_config,
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    assert legacy.select("symbol", "side", "target_weight").rows() == [
        ("ADAUSDT", "SHORT", -0.5),
        ("ETHUSDT", "LONG", 0.5),
    ]


def test_rankings_artifact_is_sorted_unique_and_owned_by_final_run(
    tmp_path: Path,
) -> None:
    definition = get_schema_definition("rankings", "v1")
    root = tmp_path / "run"
    path = root / "tables" / "rankings.parquet"
    RunArtifactStore._write_table(
        root,
        "tables/rankings.parquet",
        _rankings(),
        run_id="acceptance-a13-run",
        sort_by=definition.sort_key,
    )
    restored = pl.read_parquet(path)

    assert restored.columns == definition.schema.names
    assert restored.select(definition.primary_key).is_duplicated().any() is False
    assert restored["run_id"].unique().to_list() == ["acceptance-a13-run"]
    assert restored.select(definition.sort_key).rows() == sorted(
        restored.select(definition.sort_key).rows()
    )


def test_history_rank_is_owned_by_a14_without_breaking_a13_contracts() -> None:
    result = construct_portfolio(
        _scores(),
        _v2_portfolio(long={"ranks": [1]}, short={}, lag=1),
        factor_name=FACTOR_NAME,
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    )

    assert result.rank_state is not None
    assert result.rank_state.lag == 1
