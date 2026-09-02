"""User-run offline acceptance suite for A04; Codex does not execute it."""

from __future__ import annotations

import hashlib
import io
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bfbt.cli import app
from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.ingest.raw_store import RawRestStore
from bfbt.data.ingest.service import ArchiveIngestService
from bfbt.data.manifests import RawObjectManifest, load_manifest
from bfbt.data.sources.base import (
    ArchiveDiscoveryRequest,
    ChecksumError,
    FetchStatus,
    RawObjectConflictError,
    SourceError,
    SourceProtocolError,
)
from bfbt.data.sources.binance_archive import (
    BinanceArchiveSource,
    archive_candidates,
    local_archive_coverage,
    parse_checksum,
)
from bfbt.data.sources.binance_rest import BinanceRestSource
from bfbt.data.sources.http import PublicHttpClient, RetryPolicy

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = BACKTEST_ROOT / "tests" / "fixtures" / "ingest" / "acceptance_04"
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _archive_request(**updates: object) -> ArchiveDiscoveryRequest:
    payload: dict[str, object] = {
        "dataset_name": "bars",
        "symbol": "btcusdt",
        "interval": "1m",
        "frequency": "monthly",
        "start": "2024-01-15T00:00:00Z",
        "end": "2024-03-01T00:00:00Z",
    }
    payload.update(updates)
    return ArchiveDiscoveryRequest.model_validate(payload)


def _zip_bytes(filename: str = "BTCUSDT-1m-2024-01.csv") -> bytes:
    output = io.BytesIO()
    info = zipfile.ZipInfo(filename, date_time=(2024, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(info, (FIXTURES / "archive_bars.csv").read_bytes())
    return output.getvalue()


def _http(handler, *, retries: int = 0, sleeper=lambda _: None) -> PublicHttpClient:
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        headers={"User-Agent": "a04-test"},
    )
    return PublicHttpClient(
        client=client,
        retry_policy=RetryPolicy(max_retries=retries),
        sleeper=sleeper,
    )


def _archive_handler(zip_payload: bytes, calls: list[str]):
    digest = hashlib.sha256(zip_payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url}")
        filename = Path(request.url.path.removesuffix(".CHECKSUM")).name
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, content=f"{digest}  {filename}\n".encode())
        return httpx.Response(
            200,
            stream=httpx.ByteStream(zip_payload),
            headers={"ETag": '"fixture"'},
        )

    return handler


def test_archive_candidates_use_exact_paths_and_complete_period_coverage() -> None:
    monthly = archive_candidates(_archive_request())
    assert len(monthly) == 2
    assert monthly[0].symbol == "BTCUSDT"
    assert monthly[0].available_from.isoformat() == "2024-01-01T00:00:00+00:00"
    assert monthly[0].available_to.isoformat() == "2024-02-01T00:00:00+00:00"
    assert monthly[0].url.endswith(
        "/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-01.zip"
    )
    daily = archive_candidates(
        _archive_request(
            frequency="daily",
            start="2024-01-31T12:00:00Z",
            end="2024-02-02T00:00:00Z",
        )
    )
    assert [item.period for item in daily] == ["2024-01-31", "2024-02-01"]

    with pytest.raises(ValidationError, match="monthly-only"):
        _archive_request(
            dataset_name="funding",
            interval=None,
            frequency="daily",
        )
    with pytest.raises(ValidationError, match="not supported"):
        _archive_request(interval="7m")


def test_archive_accepts_real_unicode_binance_symbol_and_checksum() -> None:
    symbol = "币安人生USDT"
    candidate = archive_candidates(_archive_request(symbol=symbol))[0]
    assert candidate.symbol == symbol
    assert f"/{symbol}/1m/{symbol}-1m-2024-01.zip" in candidate.url
    filename = f"{symbol}-1m-2024-01.zip"
    digest = "a" * 64
    payload = f"{digest}  {filename}\n".encode()
    assert parse_checksum(payload, filename) == digest


def test_archive_discovery_uses_head_and_requires_zip_checksum_pair() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if "2024-02" in request.url.path and request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(404)
        return httpx.Response(200)

    source = BinanceArchiveSource(_http(handler))
    discovered = source.discover(_archive_request())
    assert [item.period for item in discovered] == ["2024-01"]
    assert set(calls) == {"HEAD"}

    def orphan_checksum(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.path.endswith(".CHECKSUM") else 404)

    with pytest.raises(SourceProtocolError, match="archive is missing"):
        BinanceArchiveSource(_http(orphan_checksum)).discover(
            _archive_request(end="2024-02-01T00:00:00Z")
        )


def test_checksum_parser_is_strict_about_hash_and_filename() -> None:
    digest = "a" * 64
    assert parse_checksum(f"{digest}  file.zip\n".encode(), "file.zip") == digest
    with pytest.raises(ChecksumError, match="expected"):
        parse_checksum(f"{digest}  other.zip\n".encode(), "file.zip")
    with pytest.raises(ChecksumError, match="exactly one"):
        parse_checksum(f"{digest}  file.zip\n{digest}  file.zip\n".encode(), "file.zip")


def test_archive_fetch_is_atomic_cataloged_and_idempotent(tmp_path: Path) -> None:
    payload = _zip_bytes()
    calls: list[str] = []
    source = BinanceArchiveSource(_http(_archive_handler(payload, calls)))
    remote = archive_candidates(
        _archive_request(end="2024-02-01T00:00:00Z")
    )[0]
    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()

    first = source.fetch(
        remote,
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
        now=lambda: NOW,
    )
    assert first.status == FetchStatus.DOWNLOADED
    assert first.catalog_inserted is True
    assert Path(first.path).read_bytes() == payload
    assert Path(f"{first.path}.CHECKSUM").is_file()
    manifest = load_manifest(Path(first.manifest_path), "raw")
    assert isinstance(manifest, RawObjectManifest)
    assert manifest.source == "binance_public_archive"
    assert manifest.upstream_checksum_sha256 == first.checksum_sha256

    second = source.fetch(
        remote,
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
        now=lambda: NOW + timedelta(days=1),
    )
    assert second.status == FetchStatus.SKIPPED
    assert second.catalog_inserted is False
    assert second.retrieved_at == NOW
    assert sum("GET" in item and not item.endswith("CHECKSUM") for item in calls) == 1
    coverage = local_archive_coverage(
        _archive_request(end="2024-02-01T00:00:00Z"),
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
    )
    assert [item.status.value for item in coverage] == ["verified"]


def test_checksum_failure_keeps_part_and_never_publishes_manifest(tmp_path: Path) -> None:
    payload = _zip_bytes()
    remote = archive_candidates(
        _archive_request(end="2024-02-01T00:00:00Z")
    )[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            filename = Path(request.url.path.removesuffix(".CHECKSUM")).name
            return httpx.Response(200, content=f"{'0' * 64}  {filename}\n".encode())
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    with pytest.raises(ChecksumError, match="mismatch"):
        BinanceArchiveSource(_http(handler)).fetch(
            remote,
            raw_root=tmp_path / "raw",
            manifest_root=tmp_path / "manifests",
            now=lambda: NOW,
        )
    assert tuple((tmp_path / "raw").rglob("*.part"))
    assert not tuple((tmp_path / "manifests").glob("*.json"))


def test_upstream_revision_does_not_overwrite_immutable_raw_object(tmp_path: Path) -> None:
    first_payload = _zip_bytes()
    second_payload = _zip_bytes("revised.csv")
    remote = archive_candidates(
        _archive_request(end="2024-02-01T00:00:00Z")
    )[0]
    source = BinanceArchiveSource(_http(_archive_handler(first_payload, [])))
    result = source.fetch(
        remote,
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        now=lambda: NOW,
    )
    revised = BinanceArchiveSource(_http(_archive_handler(second_payload, [])))
    with pytest.raises(RawObjectConflictError, match="differs from upstream"):
        revised.fetch(
            remote,
            raw_root=tmp_path / "raw",
            manifest_root=tmp_path / "manifests",
            now=lambda: NOW + timedelta(days=1),
        )
    assert Path(result.path).read_bytes() == first_payload

    # An orphaned old manifest must also block a revised formal object.
    Path(result.path).unlink()
    with pytest.raises(RawObjectConflictError, match="existing manifest conflicts"):
        revised.fetch(
            remote,
            raw_root=tmp_path / "raw",
            manifest_root=tmp_path / "manifests",
            now=lambda: NOW + timedelta(days=2),
        )
    assert not Path(result.path).exists()
    assert tuple((tmp_path / "raw").rglob("*.part"))


class _BrokenStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"partial"
        raise httpx.ReadError("fixture interruption")


def test_interrupted_download_is_not_registered_or_published(tmp_path: Path) -> None:
    payload = _zip_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    remote = archive_candidates(
        _archive_request(end="2024-02-01T00:00:00Z")
    )[0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            filename = Path(request.url.path.removesuffix(".CHECKSUM")).name
            return httpx.Response(200, content=f"{digest}  {filename}\n".encode())
        return httpx.Response(200, stream=_BrokenStream())

    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()
    with pytest.raises(SourceError, match="download failed"):
        BinanceArchiveSource(_http(handler)).fetch(
            remote,
            raw_root=tmp_path / "raw",
            manifest_root=tmp_path / "manifests",
            catalog=catalog,
            now=lambda: NOW,
        )
    assert tuple((tmp_path / "raw").rglob("*.part"))
    assert not tuple((tmp_path / "raw").rglob("*.zip"))
    assert dict((item.table, item.rows) for item in catalog.info().counts)["raw_objects"] == 0


def test_http_retries_429_and_rejects_authentication_headers() -> None:
    attempts = 0
    delays: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"})
        return httpx.Response(200, content=b"ok")

    response = _http(handler, retries=1, sleeper=delays.append).request(
        "GET", "https://fapi.binance.com/fapi/v1/time"
    )
    assert response.content == b"ok"
    assert attempts == 2
    assert delays == [0.5]

    authenticated = httpx.Client(headers={"X-MBX-APIKEY": "must-not-be-used"})
    with pytest.raises(ValueError, match="authentication headers"):
        PublicHttpClient(client=authenticated)
    authenticated.close()


def test_kline_rest_pagination_uses_left_closed_right_open_range() -> None:
    bodies = [
        (FIXTURES / "klines_page_1.json").read_bytes(),
        (FIXTURES / "klines_page_2.json").read_bytes(),
    ]
    starts: list[int] = []
    ends: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(int(request.url.params["startTime"]))
        ends.append(int(request.url.params["endTime"]))
        return httpx.Response(200, content=bodies[len(starts) - 1])

    source = BinanceRestSource(_http(handler), now=lambda: NOW)
    pages = tuple(
        source.kline_pages(
            dataset_name="bars",
            symbol="btcusdt",
            interval="1m",
            start=START,
            end=START + timedelta(minutes=3),
            limit=2,
        )
    )
    assert [len(page.records) for page in pages] == [2, 1]
    assert starts == [1704067200000, 1704067320000]
    assert ends == [1704067379999, 1704067379999]
    assert pages[-1].available_to == START + timedelta(minutes=3)

    duplicate = (FIXTURES / "klines_page_1.json").read_bytes().replace(
        b"1704067260000", b"1704067200000"
    )
    with pytest.raises(SourceProtocolError, match="unique and increasing"):
        tuple(
            BinanceRestSource(_http(lambda _: httpx.Response(200, content=duplicate))).kline_pages(
                dataset_name="bars",
                symbol="BTCUSDT",
                interval="1m",
                start=START,
                end=START + timedelta(minutes=3),
                limit=2,
            )
        )


def test_funding_rest_pagination_advances_one_millisecond() -> None:
    bodies = [
        (FIXTURES / "funding_page_1.json").read_bytes(),
        (FIXTURES / "funding_page_2.json").read_bytes(),
    ]
    starts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        starts.append(int(request.url.params["startTime"]))
        return httpx.Response(200, content=bodies[len(starts) - 1])

    pages = tuple(
        BinanceRestSource(_http(handler), now=lambda: NOW).funding_pages(
            symbol="BTCUSDT",
            start=START,
            end=START + timedelta(days=1),
            limit=2,
        )
    )
    assert [len(page.records) for page in pages] == [2, 1]
    assert starts == [1704067200000, 1704096000001]
    with pytest.raises(ValueError, match="between 1 and 1000"):
        tuple(
            BinanceRestSource(_http(handler)).funding_pages(
                symbol="BTCUSDT",
                start=START,
                end=START + timedelta(days=1),
                limit=1001,
            )
        )


def test_rest_raw_store_preserves_exact_bytes_and_is_idempotent(tmp_path: Path) -> None:
    body = (FIXTURES / "klines_page_2.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    source = BinanceRestSource(_http(handler), now=lambda: NOW)
    page = tuple(
        source.kline_pages(
            dataset_name="bars",
            symbol="BTCUSDT",
            interval="1m",
            start=START + timedelta(minutes=2),
            end=START + timedelta(minutes=3),
        )
    )[0]
    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()
    store = RawRestStore()
    first = store.publish(
        page,
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
    )
    later = page.model_copy(update={"retrieved_at": NOW + timedelta(days=1)})
    second = store.publish(
        later,
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
    )
    assert Path(first.path).read_bytes() == body
    assert first.status == FetchStatus.DOWNLOADED
    assert second.status == FetchStatus.SKIPPED
    assert second.retrieved_at == NOW
    assert second.catalog_inserted is False


def test_metadata_snapshots_are_public_versioned_and_cataloged(tmp_path: Path) -> None:
    exchange = (FIXTURES / "exchange_info.json").read_bytes()
    funding = (FIXTURES / "funding_info.json").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        body = exchange if request.url.path.endswith("exchangeInfo") else funding
        return httpx.Response(200, content=body)

    source = BinanceRestSource(_http(handler), now=lambda: NOW)
    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()
    store = RawRestStore()
    exchange_result = store.publish(
        source.exchange_info(),
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
    )
    funding_result = store.publish(
        source.funding_info(),
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
    )
    exchange_manifest = load_manifest(Path(exchange_result.manifest_path), "raw")
    assert isinstance(exchange_manifest, RawObjectManifest)
    assert exchange_manifest.source == "binance_exchange_info"
    assert exchange_manifest.dataset_name == "contracts"
    assert Path(exchange_result.path).read_bytes() == exchange
    assert Path(funding_result.path).read_bytes() == funding
    assert dict((item.table, item.rows) for item in catalog.info().counts)["raw_objects"] == 2

    with pytest.raises(ValidationError, match="API keys or signatures"):
        RawObjectManifest.model_validate(
            {
                **exchange_manifest.model_dump(mode="json"),
                "source_uri": "https://fapi.binance.com/fapi/v1/exchangeInfo?api_key=secret",
            }
        )


def test_archive_ingest_service_registers_only_after_all_downloads(tmp_path: Path) -> None:
    payload = _zip_bytes()
    calls: list[str] = []
    lock = threading.Lock()
    base_handler = _archive_handler(payload, calls)

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            return base_handler(request)

    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()
    results = ArchiveIngestService(
        BinanceArchiveSource(_http(handler))
    ).sync(
        _archive_request(),
        raw_root=tmp_path / "raw",
        manifest_root=tmp_path / "manifests",
        catalog=catalog,
        max_workers=2,
    )
    assert len(results) == 2
    assert all(item.catalog_inserted is True for item in results)
    assert dict((item.table, item.rows) for item in catalog.info().counts)["raw_objects"] == 2

    failed_catalog = DuckDBCatalog(tmp_path / "failed-catalog.duckdb")
    failed_catalog.initialize()
    digest = hashlib.sha256(payload).hexdigest()

    def one_bad_checksum(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)
        filename = Path(request.url.path.removesuffix(".CHECKSUM")).name
        if request.url.path.endswith(".CHECKSUM"):
            selected = "0" * 64 if "2024-02" in request.url.path else digest
            return httpx.Response(200, content=f"{selected}  {filename}\n".encode())
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    with pytest.raises(ChecksumError, match="mismatch"):
        ArchiveIngestService(BinanceArchiveSource(_http(one_bad_checksum))).sync(
            _archive_request(),
            raw_root=tmp_path / "failed-raw",
            manifest_root=tmp_path / "failed-manifests",
            catalog=failed_catalog,
            max_workers=2,
        )
    assert dict((item.table, item.rows) for item in failed_catalog.info().counts)["raw_objects"] == 0


def test_data_cli_archive_plan_is_offline_and_validated(tmp_path: Path) -> None:
    runner = CliRunner()
    planned = runner.invoke(
        app,
        [
            "data",
            "archive-plan",
            "bars",
            "btcusdt",
            "2024-01-15T00:00:00Z",
            "2024-03-01T00:00:00Z",
            "--interval",
            "1m",
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert "candidates=2" in planned.output
    assert "BTCUSDT-1m-2024-01.zip" in planned.output

    coverage = runner.invoke(
        app,
        [
            "data",
            "archive-coverage",
            "bars",
            "BTCUSDT",
            "2024-01-01T00:00:00Z",
            "2024-02-01T00:00:00Z",
            "--interval",
            "1m",
            "--raw-root",
            str(tmp_path / "raw"),
            "--manifest-root",
            str(tmp_path / "manifests"),
        ],
    )
    assert coverage.exit_code == 0, coverage.output
    assert "2024-01 status=missing" in coverage.output

    invalid = runner.invoke(
        app,
        [
            "data",
            "archive-plan",
            "funding",
            "BTCUSDT",
            "2024-01-01T00:00:00Z",
            "2024-02-01T00:00:00Z",
            "--frequency",
            "daily",
        ],
    )
    assert invalid.exit_code == 2
    assert "monthly-only" in invalid.output
