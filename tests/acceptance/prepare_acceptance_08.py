"""Prepare isolated normalized multi-symbol A08 execution fixtures."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.ingest.raw_store import RawRestStore
from bianbt.data.normalize.service import NormalizationService
from bianbt.data.sources.base import RestPage

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
SERIES = {
    "AUSDT": tuple(100 + minute for minute in range(10)),
    "BUSDT": tuple(100 + 2 * minute for minute in range(10)),
    "CUSDT": tuple(100 - minute for minute in range(10)),
}


def _body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _klines(prices: tuple[int, ...]) -> list[list[object]]:
    rows = []
    for minute, price in enumerate(prices):
        open_time = START + timedelta(minutes=minute)
        open_ms = int(open_time.timestamp() * 1_000)
        rows.append(
            [
                open_ms,
                str(price),
                str(price + 1),
                str(price - 1),
                str(price),
                "2",
                open_ms + 59_999,
                "10",
                3,
                "1",
                "2.5",
                "0",
            ]
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    roots = {
        "raw_root": workdir / "raw",
        "normalized_root": workdir / "normalized",
        "partition_manifest_root": workdir / "partition-manifests",
        "quality_root": workdir / "quality",
    }
    manifest_root = workdir / "raw-manifests"
    database = workdir / "catalog.duckdb"
    catalog = DuckDBCatalog(database)
    catalog.initialize()
    bar_manifests = []
    for symbol, prices in SERIES.items():
        payload = _klines(prices)
        page = RestPage(
            dataset_name="bars",
            endpoint="/fapi/v1/klines",
            source_uri=(
                "https://fapi.binance.com/fapi/v1/klines"
                f"?symbol={symbol}&interval=1m"
            ),
            symbol=symbol,
            interval="1m",
            available_from=START,
            available_to=START + timedelta(minutes=10),
            retrieved_at=START + timedelta(days=1),
            page_number=1,
            records=tuple(payload),
            response_body=_body(payload),
            http_status=200,
        )
        published = RawRestStore().publish(
            page,
            raw_root=roots["raw_root"],
            manifest_root=manifest_root,
        )
        bar_manifests.append(Path(published.manifest_path))
    bars_result = NormalizationService().run(
        "bars",
        tuple(bar_manifests),
        **roots,
        catalog=catalog,
    )

    mark_manifests = []
    for symbol, prices in SERIES.items():
        payload = _klines(prices)
        page = RestPage(
            dataset_name="mark_bars",
            endpoint="/fapi/v1/markPriceKlines",
            source_uri="https://fapi.binance.com/fapi/v1/markPriceKlines",
            symbol=symbol,
            interval="1m",
            available_from=START,
            available_to=START + timedelta(minutes=10),
            retrieved_at=START + timedelta(days=1),
            page_number=1,
            records=tuple(payload),
            response_body=_body(payload),
            http_status=200,
        )
        published = RawRestStore().publish(
            page, raw_root=roots["raw_root"], manifest_root=manifest_root
        )
        mark_manifests.append(Path(published.manifest_path))
    mark_result = NormalizationService().run(
        "mark_bars", tuple(mark_manifests), **roots, catalog=catalog
    )

    funding_manifests = []
    funding_time = START + timedelta(minutes=6)
    for symbol in SERIES:
        payload = [
            {
                "symbol": symbol,
                "fundingTime": int(funding_time.timestamp() * 1_000),
                "fundingRate": "0.0001",
                "markPrice": str(SERIES[symbol][6]),
            }
        ]
        page = RestPage(
            dataset_name="funding",
            endpoint="/fapi/v1/fundingRate",
            source_uri="https://fapi.binance.com/fapi/v1/fundingRate",
            symbol=symbol,
            interval=None,
            available_from=START,
            available_to=START + timedelta(minutes=10),
            retrieved_at=START + timedelta(days=1),
            page_number=1,
            records=tuple(payload),
            response_body=_body(payload),
            http_status=200,
        )
        published = RawRestStore().publish(
            page, raw_root=roots["raw_root"], manifest_root=manifest_root
        )
        funding_manifests.append(Path(published.manifest_path))
    funding_result = NormalizationService().run(
        "funding", tuple(funding_manifests), **roots, catalog=catalog
    )

    exchange_info = {
        "symbols": [
            {
                "symbol": symbol,
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": symbol.removesuffix("USDT"),
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "onboardDate": int(
                    (START - timedelta(days=60)).timestamp() * 1_000
                ),
                "deliveryDate": int(
                    (START + timedelta(days=3650)).timestamp() * 1_000
                ),
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }
            for symbol in SERIES
        ]
    }
    contracts_page = RestPage(
        dataset_name="contracts",
        endpoint="/fapi/v1/exchangeInfo",
        source_uri="https://fapi.binance.com/fapi/v1/exchangeInfo",
        symbol=None,
        interval=None,
        available_from=START,
        available_to=None,
        retrieved_at=START,
        page_number=1,
        records=(exchange_info,),
        response_body=_body(exchange_info),
        http_status=200,
    )
    contracts_raw = RawRestStore().publish(
        contracts_page,
        raw_root=roots["raw_root"],
        manifest_root=manifest_root,
    )
    contracts_result = NormalizationService().run(
        "contracts",
        (Path(contracts_raw.manifest_path),),
        **roots,
        catalog=catalog,
    )

    universe_config = {
        "schedule": {"interval": "1m"},
        "filters": {
            "trading_status_only": True,
            "min_listing_age_days": 0,
            "min_history_bars": 3,
            "rolling_quote_volume": {"window": "3m", "minimum": 0},
            "max_missing_ratio": {"window": "3m", "maximum": 0},
            "exclude_symbols": [],
        },
    }
    factor_config = {
        "factors": [
            {
                "name": "momentum",
                "version": "v1",
                "parameters": {"lookback": "2m"},
                "compute_interval": "1m",
                "preprocess": [{"name": "rank"}],
            }
        ],
        "labels": [
            {
                "name": "forward_return_2m",
                "signal_delay_bars": 1,
                "horizon": "2m",
                "entry_field": "open",
                "exit_field": "open",
            }
        ],
        "cache": {"enabled": True},
    }
    backtest_config = {
        "schedule": {
            "factor_interval": "1m",
            "rebalance_interval": "1m",
            "signal_delay_bars": 1,
        },
        "portfolio": {
            "construction": "long_short_count",
            "long_count": 1,
            "short_count": 1,
            "weighting": "equal",
            "gross_exposure": 1.0,
            "net_exposure": 0.0,
            "max_symbol_weight": None,
            "max_turnover": None,
        },
        "execution": {
            "fill_price": "next_bar_open",
            "partial_fill": False,
            "fee": {"model": "fixed_bps", "taker_bps": 1.0},
            "slippage": {"model": "fixed_bps", "bps": 2.0},
            "funding": {"enabled": True, "missing_policy": "error"},
        },
        "valuation": {"price": "mark_close"},
        "risk": {"leverage": 1.0, "enforce_liquidation": False},
    }
    universe_path = workdir / "universe.json"
    factor_path = workdir / "factor.json"
    backtest_path = workdir / "backtest.json"
    universe_path.write_text(
        json.dumps(universe_config, indent=2), encoding="utf-8"
    )
    factor_path.write_text(json.dumps(factor_config, indent=2), encoding="utf-8")
    backtest_path.write_text(
        json.dumps(backtest_config, indent=2), encoding="utf-8"
    )
    print(f"workdir={workdir}")
    print(f"bars_version={bars_result.partition_manifest.dataset_version}")
    print(f"mark_version={mark_result.partition_manifest.dataset_version}")
    print(f"funding_version={funding_result.partition_manifest.dataset_version}")
    print(f"contracts_version={contracts_result.partition_manifest.dataset_version}")
    print(f"normalized_root={roots['normalized_root']}")
    print(f"database={database}")
    print(f"universe_config={universe_path}")
    print(f"factor_config={factor_path}")
    print(f"backtest_config={backtest_path}")


if __name__ == "__main__":
    main()
