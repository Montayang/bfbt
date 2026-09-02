"""User-run offline acceptance suite for A06; Codex does not execute it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from bfbt.cli import app
from bfbt.config.universe import UniverseConfig
from bfbt.data.resample import ResampleError, resample_bars
from bfbt.universe.filters import UniverseReason
from bfbt.universe.point_in_time import (
    UniverseBuildError,
    build_point_in_time_universe,
    build_schedule,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
BARS_VERSION = "bars-a06-fixture"
CONTRACTS_VERSION = "contracts-a06-fixture"


def _bars(
    *,
    symbol: str = "BTCUSDT",
    minutes: tuple[int, ...] = (0, 1, 2, 3, 4),
    complete: tuple[bool, ...] | None = None,
    quote_volume: float = 10.0,
    version: str = BARS_VERSION,
) -> pl.LazyFrame:
    flags = complete or tuple(True for _ in minutes)
    rows = []
    for index, (minute, is_complete) in enumerate(zip(minutes, flags)):
        open_time = START + timedelta(minutes=minute)
        price = 100.0 + index
        rows.append(
            {
                "open_time": open_time,
                "close_time": open_time + timedelta(minutes=1),
                "symbol": symbol,
                "interval": "1m",
                "open": price,
                "high": price + 2.0,
                "low": price - 1.0,
                "close": price + 1.0,
                "volume": 2.0,
                "quote_volume": quote_volume,
                "trades": 3,
                "taker_buy_volume": 1.0,
                "taker_buy_quote_volume": quote_volume / 2,
                "is_complete": is_complete,
                "source": "fixture",
                "source_object_id": f"{symbol}-{minute}",
                "dataset_version": version,
            }
        )
    return pl.DataFrame(rows).lazy()


def _contracts(rows: list[dict[str, object]]) -> pl.LazyFrame:
    defaults = {
        "contract_type": "PERPETUAL",
        "status": "TRADING",
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "onboard_time": START - timedelta(days=60),
        "delivery_time": None,
        "dataset_version": CONTRACTS_VERSION,
    }
    return pl.DataFrame([defaults | row for row in rows]).lazy()


def _config(**filter_updates: object) -> UniverseConfig:
    filters = {
        "trading_status_only": True,
        "min_listing_age_days": 0,
        "min_history_bars": 2,
        "rolling_quote_volume": {"window": "2m", "minimum": 15.0},
        "max_missing_ratio": {"window": "2m", "maximum": 0.0},
        "exclude_symbols": [],
    }
    filters.update(filter_updates)
    return UniverseConfig.model_validate(
        {
            "schedule": {"interval": "1m"},
            "filters": filters,
        }
    )


def _build(
    bars: pl.LazyFrame,
    contracts: pl.LazyFrame,
    *,
    config: UniverseConfig,
    start: datetime = START + timedelta(minutes=2),
    end: datetime = START + timedelta(minutes=3),
):
    schedule = build_schedule(start=start, end=end, interval="1m")
    return build_point_in_time_universe(
        bars,
        contracts,
        schedule,
        config=config,
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        contracts_dataset_version=CONTRACTS_VERSION,
    )


def test_trade_bars_resample_to_exact_utc_ohlcv() -> None:
    result = resample_bars(
        _bars(),
        dataset_name="bars",
        source_interval="1m",
        target_interval="5m",
        source_dataset_version=BARS_VERSION,
    )
    frame = result.frame.collect()
    row = frame.to_dicts()[0]
    assert isinstance(result.frame, pl.LazyFrame)
    assert result.expected_source_bars == 5
    assert row["open_time"] == START
    assert row["close_time"] == START + timedelta(minutes=5)
    assert row["open"] == 100.0
    assert row["high"] == 106.0
    assert row["low"] == 99.0
    assert row["close"] == 105.0
    assert row["volume"] == 10.0
    assert row["quote_volume"] == 50.0
    assert row["trades"] == 15
    assert row["is_complete"] is True


def test_resample_retains_gap_bucket_but_marks_it_incomplete() -> None:
    result = resample_bars(
        _bars(minutes=(0, 1, 3, 4)),
        dataset_name="bars",
        source_interval="1m",
        target_interval="5m",
        source_dataset_version=BARS_VERSION,
    )
    row = result.frame.collect().to_dicts()[0]
    assert row["is_complete"] is False
    assert row["close_time"] == START + timedelta(minutes=5)


def test_mark_bar_resample_has_no_fake_volume_columns() -> None:
    source = _bars().drop(
        "volume",
        "quote_volume",
        "trades",
        "taker_buy_volume",
        "taker_buy_quote_volume",
    )
    result = resample_bars(
        source,
        dataset_name="mark_bars",
        source_interval="1m",
        target_interval="5m",
        source_dataset_version=BARS_VERSION,
    )
    assert "volume" not in result.frame.collect_schema().names()
    assert result.frame.collect()["is_complete"].to_list() == [True]


def test_resample_and_schedule_reject_ambiguous_boundaries() -> None:
    with pytest.raises(ResampleError, match="larger integer multiple"):
        resample_bars(
            _bars(),
            dataset_name="bars",
            source_interval="5m",
            target_interval="1m",
            source_dataset_version=BARS_VERSION,
        )
    with pytest.raises(UniverseBuildError, match="align"):
        build_schedule(
            start=START + timedelta(seconds=30),
            end=START + timedelta(minutes=1),
            interval="1m",
        )


def test_universe_uses_only_latest_snapshot_available_at_each_time() -> None:
    contracts = _contracts(
        [
            {"symbol": "BTCUSDT", "snapshot_time": START},
            {
                "symbol": "BTCUSDT",
                "snapshot_time": START + timedelta(minutes=3),
                "status": "BREAK",
            },
        ]
    )
    result = _build(
        _bars(),
        contracts,
        config=_config(),
        end=START + timedelta(minutes=4),
    )
    rows = result.frame.collect().to_dicts()
    assert rows[0]["timestamp"] == START + timedelta(minutes=2)
    assert rows[0]["reason_code"] == UniverseReason.ELIGIBLE.value
    assert rows[0]["history_bars"] == 2
    assert rows[1]["timestamp"] == START + timedelta(minutes=3)
    assert rows[1]["reason_code"] == UniverseReason.NOT_TRADING.value


def test_universe_reports_market_listing_and_delivery_reasons() -> None:
    bars = pl.concat(
        [
            _bars(symbol="FUTUREUSDT"),
            _bars(symbol="WRONGUSDC"),
            _bars(symbol="OLDUSDT"),
            _bars(symbol="NOSNAPSHOT"),
        ]
    )
    contracts = _contracts(
        [
            {
                "symbol": "FUTUREUSDT",
                "snapshot_time": START,
                "onboard_time": START + timedelta(days=1),
            },
            {
                "symbol": "WRONGUSDC",
                "snapshot_time": START,
                "quote_asset": "USDC",
            },
            {
                "symbol": "OLDUSDT",
                "snapshot_time": START,
                "delivery_time": START + timedelta(minutes=1),
            },
        ]
    )
    rows = _build(bars, contracts, config=_config()).frame.collect()
    reasons = dict(zip(rows["symbol"].to_list(), rows["reason_code"].to_list()))
    assert reasons == {
        "FUTUREUSDT": UniverseReason.NOT_LISTED.value,
        "NOSNAPSHOT": UniverseReason.NO_CONTRACT_SNAPSHOT.value,
        "OLDUSDT": UniverseReason.DELISTED.value,
        "WRONGUSDC": UniverseReason.WRONG_QUOTE_ASSET.value,
    }


def test_universe_reports_history_missing_liquidity_and_explicit_exclusion() -> None:
    bars = pl.concat(
        [
            _bars(symbol="SHORT", minutes=(1,)),
            _bars(symbol="MISSING", complete=(True, False, True, True, True)),
            _bars(symbol="ILLIQUID", quote_volume=1.0),
            _bars(symbol="EXCLUDED"),
        ]
    )
    contracts = _contracts(
        [
            {"symbol": symbol, "snapshot_time": START}
            for symbol in ("SHORT", "MISSING", "ILLIQUID", "EXCLUDED")
        ]
    )
    config = _config(exclude_symbols=["EXCLUDED"])
    rows = _build(
        bars,
        contracts,
        config=config,
        start=START + timedelta(minutes=3),
        end=START + timedelta(minutes=4),
    ).frame.collect()
    reasons = dict(zip(rows["symbol"].to_list(), rows["reason_code"].to_list()))
    assert reasons["EXCLUDED"] == UniverseReason.EXPLICITLY_EXCLUDED.value
    assert reasons["SHORT"] == UniverseReason.INSUFFICIENT_HISTORY.value
    assert reasons["MISSING"] == UniverseReason.MISSING_DATA.value
    assert reasons["ILLIQUID"] == UniverseReason.ILLIQUID.value


def test_missing_window_advances_when_market_data_stops() -> None:
    contracts = _contracts([{"symbol": "BTCUSDT", "snapshot_time": START}])
    config = _config(
        min_history_bars=1,
        rolling_quote_volume={"window": "2m", "minimum": None},
    )
    row = _build(
        _bars(minutes=(0,)),
        contracts,
        config=config,
        start=START + timedelta(minutes=3),
        end=START + timedelta(minutes=4),
    ).frame.collect().to_dicts()[0]
    assert row["history_bars"] == 1
    assert row["missing_ratio"] == 1.0
    assert row["reason_code"] == UniverseReason.MISSING_DATA.value


def test_universe_version_is_deterministic_and_rejects_latest() -> None:
    contracts = _contracts([{"symbol": "BTCUSDT", "snapshot_time": START}])
    first = _build(_bars(), contracts, config=_config())
    second = _build(_bars(), contracts, config=_config())
    assert first.universe_version == second.universe_version
    assert isinstance(first.frame, pl.LazyFrame)
    with pytest.raises(UniverseBuildError, match="explicit"):
        build_point_in_time_universe(
            _bars(),
            contracts,
            build_schedule(
                start=START + timedelta(minutes=2),
                end=START + timedelta(minutes=3),
                interval="1m",
            ),
            config=_config(),
            base_interval="1m",
            bars_dataset_version="latest",
            contracts_dataset_version=CONTRACTS_VERSION,
        )


def test_a06_cli_commands_are_exposed_with_bounded_preview_options() -> None:
    runner = CliRunner()
    resample_help = runner.invoke(app, ["data", "resample-preview", "--help"])
    universe_help = runner.invoke(app, ["universe", "preview", "--help"])
    assert resample_help.exit_code == 0, resample_help.output
    assert universe_help.exit_code == 0, universe_help.output

    root = get_command(app)
    resample = root.commands["data"].commands["resample-preview"]
    universe = root.commands["universe"].commands["preview"]
    resample_options = {
        option
        for parameter in resample.params
        for option in getattr(parameter, "opts", ())
    }
    universe_options = {
        option
        for parameter in universe.params
        for option in getattr(parameter, "opts", ())
    }
    assert {
        "--source-interval",
        "--target-interval",
        "--limit",
    } <= resample_options
    assert {"--history-start", "--limit"} <= universe_options
