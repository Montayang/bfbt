"""User-run offline acceptance suite for A05; Codex does not execute it."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pyarrow.parquet as pq
import pytest
from typer.testing import CliRunner

from bianbt.cli import app
from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.hashing import sha256_bytes
from bianbt.data.ingest.raw_store import RawRestStore
from bianbt.data.manifests import RawObjectManifest, manifest_json
from bianbt.data.normalize.core import (
    NormalizationError,
    build_normalization_release,
    normalize_bars,
    normalize_contracts,
    normalize_funding,
    raw_object_path,
)
from bianbt.data.normalize.service import NormalizationService
from bianbt.data.publisher import ParquetPublisher, PublicationConflictError
from bianbt.data.schemas import validate_arrow_schema
from bianbt.data.sources.base import RestPage
from bianbt.data.storage import DataStoreError, ParquetDataStore
from bianbt.data.validation.reports import QualityError, QualityPolicy, evaluate_quality

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
A04_FIXTURES = BACKTEST_ROOT / "tests" / "fixtures" / "ingest" / "acceptance_04"
FIXTURES = BACKTEST_ROOT / "tests" / "fixtures" / "normalize" / "acceptance_05"
NOW = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _roots(tmp_path: Path) -> dict[str, Path]:
    return {
        "raw_root": tmp_path / "raw",
        "normalized_root": tmp_path / "normalized",
        "partition_manifest_root": tmp_path / "partition-manifests",
        "quality_root": tmp_path / "quality",
    }


def _publisher_roots(roots: dict[str, Path]) -> dict[str, Path]:
    return {key: value for key, value in roots.items() if key != "raw_root"}


def _catalog(tmp_path: Path) -> DuckDBCatalog:
    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()
    return catalog


def _archive_bars(tmp_path: Path) -> tuple[RawObjectManifest, Path]:
    relative = (
        "binance/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2024-01.zip"
    )
    target = tmp_path / "raw" / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "BTCUSDT-1m-2024-01.csv",
            (A04_FIXTURES / "archive_bars.csv").read_bytes(),
        )
    payload = target.read_bytes()
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
        available_from=START,
        available_to=datetime(2024, 2, 1, tzinfo=timezone.utc),
        retrieved_at=NOW,
        byte_size=len(payload),
        checksum_sha256=sha256_bytes(payload),
        upstream_checksum_sha256=sha256_bytes(payload),
        media_type="application/zip",
        compression="zip",
        http_status=200,
    )
    manifest_path = tmp_path / "raw-manifests" / f"{manifest.object_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(manifest_json(manifest))
    return manifest, manifest_path


def _rest_raw(
    tmp_path: Path,
    fixture: str,
    *,
    dataset_name: str,
    endpoint: str,
    symbol: str | None,
    interval: str | None,
    available_from: datetime | None = START,
    available_to: datetime | None = START + timedelta(minutes=2),
    retrieved_at: datetime = NOW,
) -> tuple[RawObjectManifest, Path]:
    body = (FIXTURES / fixture).read_bytes() if (FIXTURES / fixture).is_file() else (
        A04_FIXTURES / fixture
    ).read_bytes()
    payload = json.loads(body)
    page = RestPage(
        dataset_name=dataset_name,
        endpoint=endpoint,
        source_uri=f"https://fapi.binance.com{endpoint}",
        symbol=symbol,
        interval=interval,
        available_from=available_from,
        available_to=available_to,
        retrieved_at=retrieved_at,
        page_number=1,
        records=tuple(payload) if isinstance(payload, list) else (payload,),
        response_body=body,
        http_status=200,
    )
    result = RawRestStore().publish(
        page,
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "raw-manifests",
    )
    return (
        RawObjectManifest.model_validate_json(Path(result.manifest_path).read_bytes()),
        Path(result.manifest_path),
    )


def test_archive_bars_normalization_is_schema_exact_and_deterministic(tmp_path: Path) -> None:
    manifest, _ = _archive_bars(tmp_path)
    first = normalize_bars((manifest,), raw_root=tmp_path / "raw")
    second = normalize_bars((manifest,), raw_root=tmp_path / "raw")

    assert first.dataset_version == second.dataset_version
    assert first.table.equals(second.table)
    assert first.partition_values == {"interval": "1m", "year": "2024", "month": "01"}
    validate_arrow_schema(first.table.schema, dataset="bars", version="v1")
    row = first.table.to_pylist()[0]
    assert row["open_time"] == START
    assert row["close_time"] == START + timedelta(minutes=1)
    assert row["source"] == "binance_public_archive"
    assert row["dataset_version"] == first.dataset_version


def test_release_binds_multiple_bounded_batches_to_one_version(
    tmp_path: Path,
) -> None:
    first, _ = _archive_bars(tmp_path)
    second_path = (
        tmp_path / "raw/binance/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2024-02.zip"
    )
    second_path.parent.mkdir(parents=True, exist_ok=True)
    second_path.write_bytes(
        raw_object_path(first, tmp_path / "raw").read_bytes()
    )
    second = first.model_copy(
        update={
            "object_id": "archive-bars-BTCUSDT-1m-monthly-2024-02",
            "source_uri": str(first.source_uri).replace("2024-01", "2024-02"),
            "available_from": datetime(2024, 2, 1, tzinfo=timezone.utc),
            "available_to": datetime(2024, 3, 1, tzinfo=timezone.utc),
        }
    )
    release = build_normalization_release("bars", (first, second))
    january = normalize_bars((first,), raw_root=tmp_path / "raw", release=release)
    february = normalize_bars(
        (second,), raw_root=tmp_path / "raw", release=release
    )
    assert (
        january.dataset_version
        == february.dataset_version
        == release.dataset_version
    )
    assert set(january.table["dataset_version"].to_pylist()) == {
        release.dataset_version
    }
    outsider = first.model_copy(update={"object_id": "outside-release"})
    with pytest.raises(NormalizationError, match="outside release"):
        normalize_bars(
            (outsider,), raw_root=tmp_path / "raw", release=release
        )


def test_raw_resolution_rejects_checksum_changes_before_parsing(tmp_path: Path) -> None:
    manifest, _ = _archive_bars(tmp_path)
    path = raw_object_path(manifest, tmp_path / "raw")
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(NormalizationError, match="size differs"):
        normalize_bars((manifest,), raw_root=tmp_path / "raw")


def test_quality_gate_blocks_bad_rows_but_persists_report(tmp_path: Path) -> None:
    manifest, _ = _rest_raw(
        tmp_path,
        "bad_bars.json",
        dataset_name="bars",
        endpoint="/fapi/v1/klines",
        symbol="BTCUSDT",
        interval="1m",
    )
    batch = normalize_bars((manifest,), raw_root=tmp_path / "raw")
    report = evaluate_quality(batch, policy=QualityPolicy(), evaluated_at=NOW)
    assert report.status == "fail"
    assert report.duplicate_keys == 1
    assert report.bad_ohlc == 1
    assert report.negative_values >= 1

    roots = _roots(tmp_path)
    with pytest.raises(QualityError, match="quality report"):
        ParquetPublisher().publish(batch, **_publisher_roots(roots), now=lambda: NOW)
    assert not tuple(roots["normalized_root"].rglob("*.parquet"))
    assert not tuple(roots["partition_manifest_root"].glob("*.json"))
    assert len(tuple(roots["quality_root"].glob("*.json"))) == 1


def test_publish_is_atomic_cataloged_and_idempotent(tmp_path: Path) -> None:
    _, manifest_path = _archive_bars(tmp_path)
    roots = _roots(tmp_path)
    catalog = _catalog(tmp_path)
    service = NormalizationService()
    first = service.run(
        "bars",
        (manifest_path,),
        **roots,
        catalog=catalog,
        now=lambda: NOW,
    )
    second = service.run(
        "bars",
        (manifest_path,),
        **roots,
        catalog=catalog,
        now=lambda: NOW + timedelta(days=1),
    )
    assert first.published is True
    assert second.published is False
    assert first.catalog_inserted is True
    assert second.catalog_inserted is False
    assert first.partition_manifest == second.partition_manifest
    assert first.parquet_path == second.parquet_path
    assert first.parquet_path.is_file()
    table = pq.ParquetFile(first.parquet_path).read()
    validate_arrow_schema(table.schema, dataset="bars", version="v1")
    summary = catalog.coverage("bars", first.partition_manifest.dataset_version)
    assert summary.row_count == 1
    assert summary.partition_count == 1


def test_mark_funding_and_contract_normalizers_preserve_distinct_semantics(tmp_path: Path) -> None:
    mark, _ = _rest_raw(
        tmp_path,
        "mark_klines.json",
        dataset_name="mark_bars",
        endpoint="/fapi/v1/markPriceKlines",
        symbol="BTCUSDT",
        interval="1m",
    )
    mark_batch = normalize_bars(
        (mark,), raw_root=tmp_path / "raw", dataset_name="mark_bars"
    )
    assert "volume" not in mark_batch.table.column_names
    assert mark_batch.table.num_rows == 2

    funding_rate, _ = _rest_raw(
        tmp_path,
        "funding_rate.json",
        dataset_name="funding",
        endpoint="/fapi/v1/fundingRate",
        symbol="BTCUSDT",
        interval=None,
        available_to=START + timedelta(hours=16),
    )
    funding_info, _ = _rest_raw(
        tmp_path,
        "funding_info.json",
        dataset_name="funding",
        endpoint="/fapi/v1/fundingInfo",
        symbol=None,
        interval=None,
        available_from=NOW,
        available_to=None,
    )
    funding_batch = normalize_funding(
        (funding_rate, funding_info), raw_root=tmp_path / "raw"
    )
    assert funding_batch.table.column("funding_interval_hours").to_pylist() == [8.0, 8.0]

    contracts, _ = _rest_raw(
        tmp_path,
        "exchange_info.json",
        dataset_name="contracts",
        endpoint="/fapi/v1/exchangeInfo",
        symbol=None,
        interval=None,
        available_from=NOW,
        available_to=None,
    )
    contract_batch = normalize_contracts((contracts,), raw_root=tmp_path / "raw")
    contract_rows = contract_batch.table.to_pylist()
    assert [item["symbol"] for item in contract_rows] == ["BTCUSDT", "ETHUSDT"]
    assert contract_rows[0]["price_tick"] == 0.1
    assert contract_rows[0]["quantity_step"] == 0.001


def test_lazy_datastore_filters_time_symbols_and_projection(tmp_path: Path) -> None:
    manifest, manifest_path = _rest_raw(
        tmp_path,
        "klines_page_1.json",
        dataset_name="bars",
        endpoint="/fapi/v1/klines",
        symbol="BTCUSDT",
        interval="1m",
    )
    roots = _roots(tmp_path)
    catalog = _catalog(tmp_path)
    result = NormalizationService().run(
        "bars", (manifest_path,), **roots, catalog=catalog, now=lambda: NOW
    )
    store = ParquetDataStore(
        normalized_root=roots["normalized_root"], catalog=catalog, verify_hashes=True
    )
    lazy = store.scan_bars(
        dataset_version=result.partition_manifest.dataset_version,
        start=START + timedelta(minutes=1),
        end=START + timedelta(minutes=2),
        interval="1m",
        columns=("open_time", "symbol", "close"),
        symbols=("btcusdt",),
    )
    assert isinstance(lazy, pl.LazyFrame)
    frame = lazy.collect()
    assert frame.columns == ["open_time", "symbol", "close"]
    assert frame.height == 1
    assert frame["close"].to_list() == [42150.0]

    result.parquet_path.write_bytes(result.parquet_path.read_bytes() + b"tamper")
    with pytest.raises(DataStoreError, match="checksum mismatch"):
        store.scan_bars(
            dataset_version=result.partition_manifest.dataset_version,
            start=START,
            end=START + timedelta(minutes=2),
            interval="1m",
        )


def test_publisher_rejects_unmanifested_existing_target(tmp_path: Path) -> None:
    manifest, _ = _archive_bars(tmp_path)
    batch = normalize_bars((manifest,), raw_root=tmp_path / "raw")
    roots = _roots(tmp_path)
    first = ParquetPublisher().publish(batch, **_publisher_roots(roots), now=lambda: NOW)
    first.partition_manifest_path.unlink()
    with pytest.raises(PublicationConflictError, match="without its manifest"):
        ParquetPublisher().publish(batch, **_publisher_roots(roots), now=lambda: NOW)


def test_cli_normalize_and_bounded_lazy_scan(tmp_path: Path) -> None:
    _, manifest_path = _archive_bars(tmp_path)
    roots = _roots(tmp_path)
    database = tmp_path / "catalog.duckdb"
    DuckDBCatalog(database).initialize()
    runner = CliRunner()
    normalized = runner.invoke(
        app,
        [
            "data",
            "normalize",
            "bars",
            str(manifest_path),
            "--raw-root",
            str(roots["raw_root"]),
            "--normalized-root",
            str(roots["normalized_root"]),
            "--partition-manifest-root",
            str(roots["partition_manifest_root"]),
            "--quality-root",
            str(roots["quality_root"]),
            "--database",
            str(database),
        ],
    )
    assert normalized.exit_code == 0, normalized.output
    assert "publication=published" in normalized.output
    assert "quality=pass" in normalized.output
    version = next(
        line.split("=", 1)[1]
        for line in normalized.output.splitlines()
        if line.startswith("dataset_version=")
    )
    scanned = runner.invoke(
        app,
        [
            "data",
            "normalized-scan",
            "bars",
            version,
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:01:00Z",
            "--interval",
            "1m",
            "--columns",
            "open_time,symbol,close",
            "--normalized-root",
            str(roots["normalized_root"]),
            "--database",
            str(database),
            "--verify-hashes",
        ],
    )
    assert scanned.exit_code == 0, scanned.output
    assert "rows=1" in scanned.output
    assert "BTCUSDT" in scanned.output

    latest = runner.invoke(
        app,
        [
            "data",
            "normalized-scan",
            "bars",
            "latest",
            "2024-01-01T00:00:00Z",
            "2024-01-01T00:01:00Z",
            "--interval",
            "1m",
            "--normalized-root",
            str(roots["normalized_root"]),
            "--database",
            str(database),
        ],
    )
    assert latest.exit_code == 2
    assert "explicit non-'latest'" in latest.output
