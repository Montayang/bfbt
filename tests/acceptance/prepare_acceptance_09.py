"""Prepare isolated normalized multi-symbol A09 formal-run fixtures."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    manifest_json,
)
from bfbt.data.schemas import get_schema_definition
from bfbt.data.ingest.raw_store import RawRestStore
from bfbt.data.normalize.service import NormalizationService
from bfbt.data.sources.base import RestPage

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

    normalized_results = {
        "bars": bars_result,
        "mark_bars": mark_result,
        "funding": funding_result,
        "contracts": contracts_result,
    }
    references = []
    for dataset_name, result in normalized_results.items():
        partition = result.partition_manifest
        definition = get_schema_definition(dataset_name, "v1")
        references.append(
            DatasetReference(
                dataset_name=dataset_name,
                dataset_version=partition.dataset_version,
                schema_version="v1",
                schema_fingerprint=definition.fingerprint,
                available_from=START,
                available_to=START + timedelta(minutes=11),
                partition_manifest_ids=(partition.partition_id,),
                quality_report_ids=(partition.quality_report_id,),
            )
        )
    snapshot = DatasetSnapshotManifest(
        dataset_id="a09-usdm-fixture",
        dataset_version="a09-snapshot-v1",
        created_at=START + timedelta(days=2),
        datasets=tuple(references),
        source_manifest_hash=content_sha256(
            sorted(item.partition_id for item in (
                bars_result.partition_manifest,
                mark_result.partition_manifest,
                funding_result.partition_manifest,
                contracts_result.partition_manifest,
            ))
        ),
        normalizer_code_version="a05-normalizer-v1",
        normalizer_parameters_hash=content_sha256({"fixture": "a09"}),
    )
    catalog.register_dataset(snapshot)
    snapshot_path = workdir / "dataset-snapshot.json"
    snapshot_path.write_text(manifest_json(snapshot), encoding="utf-8")

    data_config = {
        "datasets": {
            "bars": {"enabled": True, "base_interval": "1m"},
            "mark_bars": {"enabled": True, "base_interval": "1m"},
            "funding": {"enabled": True},
            "contracts": {"enabled": True},
            "index_bars": {"enabled": False, "base_interval": "1m"},
        },
        "source": {"allow_authenticated_endpoints": False},
        "time": {
            "base_interval": "1m",
            "derived_intervals": [],
            "start": START.isoformat(),
            "end": (START + timedelta(minutes=10)).isoformat(),
        },
        "storage": {
            "root": str(workdir / "data"),
            "normalized": str(roots["normalized_root"]),
            "metadata": str(workdir / "metadata"),
        },
    }
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
        "run": {
            "name": "a09_formal_fixture",
            "start": (START + timedelta(minutes=4)).isoformat(),
            "end": (START + timedelta(minutes=7)).isoformat(),
            "dataset_version": snapshot.dataset_version,
            "random_seed": 42,
        },
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
        "output": {
            "root": str(workdir / "runs"),
            "save_factor_values": True,
            "save_universe": True,
            "save_positions": True,
            "save_trades": True,
            "save_costs": True,
            "render_html": True,
        },
    }
    failed_backtest_config = json.loads(json.dumps(backtest_config))
    failed_backtest_config["run"]["name"] = "a09_failed_fixture"
    failed_backtest_config["risk"]["leverage"] = 0.1

    data_path = workdir / "data.json"
    universe_path = workdir / "universe.json"
    factor_path = workdir / "factor.json"
    backtest_path = workdir / "backtest.json"
    failed_backtest_path = workdir / "backtest-failed.json"
    data_path.write_text(json.dumps(data_config, indent=2), encoding="utf-8")
    universe_path.write_text(
        json.dumps(universe_config, indent=2), encoding="utf-8"
    )
    factor_path.write_text(json.dumps(factor_config, indent=2), encoding="utf-8")
    backtest_path.write_text(
        json.dumps(backtest_config, indent=2), encoding="utf-8"
    )
    failed_backtest_path.write_text(
        json.dumps(failed_backtest_config, indent=2), encoding="utf-8"
    )
    print(f"workdir={workdir}")
    print(f"dataset_id={snapshot.dataset_id}")
    print(f"dataset_version={snapshot.dataset_version}")
    print(f"bars_version={bars_result.partition_manifest.dataset_version}")
    print(f"mark_version={mark_result.partition_manifest.dataset_version}")
    print(f"funding_version={funding_result.partition_manifest.dataset_version}")
    print(f"contracts_version={contracts_result.partition_manifest.dataset_version}")
    print(f"normalized_root={roots['normalized_root']}")
    print(f"database={database}")
    print(f"data_config={data_path}")
    print(f"universe_config={universe_path}")
    print(f"factor_config={factor_path}")
    print(f"backtest_config={backtest_path}")
    print(f"failed_backtest_config={failed_backtest_path}")
    print(f"output_root={workdir / 'runs'}")
    print(f"snapshot_manifest={snapshot_path}")


if __name__ == "__main__":
    main()
