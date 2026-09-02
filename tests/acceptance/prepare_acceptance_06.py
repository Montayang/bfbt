"""Prepare isolated normalized A06 fixtures and an initialized Catalog."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.ingest.raw_store import RawRestStore
from bfbt.data.normalize.service import NormalizationService
from bfbt.data.sources.base import RestPage

START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _body(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


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

    klines = []
    for minute in range(5):
        open_time = START + timedelta(minutes=minute)
        open_ms = int(open_time.timestamp() * 1_000)
        price = 100 + minute
        klines.append(
            [
                open_ms,
                str(price),
                str(price + 2),
                str(price - 1),
                str(price + 1),
                "2",
                open_ms + 59_999,
                "10",
                3,
                "1",
                "5",
                "0",
            ]
        )
    bars_page = RestPage(
        dataset_name="bars",
        endpoint="/fapi/v1/klines",
        source_uri="https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT",
        symbol="BTCUSDT",
        interval="1m",
        available_from=START,
        available_to=START + timedelta(minutes=5),
        retrieved_at=START + timedelta(days=1),
        page_number=1,
        records=tuple(klines),
        response_body=_body(klines),
        http_status=200,
    )
    bars_raw = RawRestStore().publish(
        bars_page,
        raw_root=roots["raw_root"],
        manifest_root=manifest_root,
    )
    bars_result = NormalizationService().run(
        "bars",
        (Path(bars_raw.manifest_path),),
        **roots,
        catalog=catalog,
    )

    exchange_info = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "contractType": "PERPETUAL",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "onboardDate": int(
                    (START - timedelta(days=60)).timestamp() * 1_000
                ),
                "deliveryDate": int(
                    (START + timedelta(days=3650)).timestamp() * 1_000
                ),
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "tickSize": "0.10",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.001",
                        "minQty": "0.001",
                    },
                    {
                        "filterType": "MIN_NOTIONAL",
                        "notional": "5",
                    },
                ],
            }
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

    config = {
        "schedule": {"interval": "1m"},
        "filters": {
            "trading_status_only": True,
            "min_listing_age_days": 0,
            "min_history_bars": 5,
            "rolling_quote_volume": {"window": "5m", "minimum": 40},
            "max_missing_ratio": {"window": "5m", "maximum": 0},
            "exclude_symbols": [],
        },
    }
    config_path = workdir / "universe.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"workdir={workdir}")
    print(f"bars_version={bars_result.partition_manifest.dataset_version}")
    print(f"contracts_version={contracts_result.partition_manifest.dataset_version}")
    print(f"normalized_root={roots['normalized_root']}")
    print(f"database={database}")
    print(f"universe_config={config_path}")


if __name__ == "__main__":
    main()
