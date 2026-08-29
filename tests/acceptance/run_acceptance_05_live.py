"""Live A05 smoke test against Binance public USD-M market data."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.ingest.raw_store import RawRestStore
from bianbt.data.normalize.service import NormalizationService
from bianbt.data.sources.binance_rest import BinanceRestSource
from bianbt.data.sources.http import PublicHttpClient
from bianbt.data.storage import ParquetDataStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    workdir = args.workdir.resolve()

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(
        minutes=1
    )
    start = end - timedelta(minutes=3)
    raw_root = workdir / "raw"
    raw_manifests = workdir / "raw-manifests"
    normalized_root = workdir / "normalized"
    partition_manifests = workdir / "partition-manifests"
    quality_root = workdir / "quality"
    database = workdir / "catalog.duckdb"
    catalog = DuckDBCatalog(database)
    catalog.initialize()

    with PublicHttpClient() as http:
        pages = BinanceRestSource(http).kline_pages(
            dataset_name="bars",
            symbol="BTCUSDT",
            interval="1m",
            start=start,
            end=end,
            limit=2,
        )
        fetched = RawRestStore().publish_all(
            pages,
            raw_root=raw_root,
            manifest_root=raw_manifests,
            catalog=catalog,
        )
    if not fetched:
        raise AssertionError("Binance returned no closed BTCUSDT bars")

    published = NormalizationService().run(
        "bars",
        tuple(Path(item.manifest_path) for item in fetched),
        raw_root=raw_root,
        normalized_root=normalized_root,
        partition_manifest_root=partition_manifests,
        quality_root=quality_root,
        catalog=catalog,
    )
    version = published.partition_manifest.dataset_version
    frame = ParquetDataStore(
        normalized_root=normalized_root,
        catalog=catalog,
        verify_hashes=True,
    ).scan_bars(
        dataset_version=version,
        start=start,
        end=end,
        interval="1m",
        columns=("open_time", "close_time", "symbol", "close", "is_complete"),
        symbols=("BTCUSDT",),
    ).collect()

    if frame.height != 3:
        raise AssertionError(f"expected 3 real bars, received {frame.height}")
    if frame["symbol"].unique().to_list() != ["BTCUSDT"]:
        raise AssertionError("live scan returned an unexpected symbol")
    if not all(frame["is_complete"].to_list()):
        raise AssertionError("live scan returned an incomplete bar")
    if not all(value > 0 for value in frame["close"].to_list()):
        raise AssertionError("live scan returned a non-positive close")

    print("live_acceptance=passed")
    print(f"workdir={workdir}")
    print(f"range=[{start.isoformat()}, {end.isoformat()})")
    print(f"raw_objects={len(fetched)}")
    print(f"dataset_version={version}")
    print(f"partition_id={published.partition_manifest.partition_id}")
    print(f"quality={published.quality_report.status}")
    print(f"rows={frame.height}")
    print(frame)


if __name__ == "__main__":
    main()
