"""Prepare one isolated A05 Raw archive and initialized Catalog for manual CLI use."""

from __future__ import annotations

import argparse
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import sha256_bytes
from bfbt.data.manifests import RawObjectManifest, manifest_json

BACKTEST_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir", type=Path)
    args = parser.parse_args()
    workdir = args.workdir.resolve()
    relative = (
        "binance/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2024-01.zip"
    )
    archive_path = workdir / "raw" / relative
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo(
        "BTCUSDT-1m-2024-01.csv",
        date_time=(2024, 1, 1, 0, 0, 0),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    fixture = (
        BACKTEST_ROOT
        / "tests"
        / "fixtures"
        / "ingest"
        / "acceptance_04"
        / "archive_bars.csv"
    ).read_bytes()
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(info, fixture)
    payload = archive_path.read_bytes()
    digest = sha256_bytes(payload)
    manifest = RawObjectManifest(
        object_id="archive-bars-BTCUSDT-1m-monthly-2024-01",
        dataset_name="bars",
        source="binance_public_archive",
        source_uri=(
            "https://data.binance.vision/data/futures/um/monthly/klines/"
            "BTCUSDT/1m/BTCUSDT-1m-2024-01.zip"
        ),
        symbol="BTCUSDT",
        interval="1m",
        available_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
        available_to=datetime(2024, 2, 1, tzinfo=timezone.utc),
        retrieved_at=datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc),
        byte_size=len(payload),
        checksum_sha256=digest,
        upstream_checksum_sha256=digest,
        media_type="application/zip",
        compression="zip",
        http_status=200,
    )
    manifest_path = workdir / "raw-manifests" / f"{manifest.object_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest_json(manifest), encoding="utf-8")
    database = workdir / "catalog.duckdb"
    DuckDBCatalog(database).initialize()
    print(f"workdir={workdir}")
    print(f"raw_manifest={manifest_path}")
    print(f"database={database}")


if __name__ == "__main__":
    main()
