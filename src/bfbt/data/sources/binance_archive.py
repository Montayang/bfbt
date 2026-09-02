"""Binance USD-M public archive discovery and checksum-verified fetching."""

from __future__ import annotations

import os
import re
import zipfile
from dataclasses import dataclass
from enum import Enum
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import sha256_file
from bfbt.data.manifests import (
    RawObjectManifest,
    load_manifest,
    manifest_json,
)
from bfbt.data.sources.base import (
    ArchiveDiscoveryRequest,
    ChecksumError,
    FetchResult,
    FetchStatus,
    RawObjectConflictError,
    RemoteArchiveObject,
    SourceProtocolError,
)
from bfbt.data.sources.http import PublicHttpClient

_BASE_URL = "https://data.binance.vision/data/futures/um"
_DATASET_PATH = {
    "bars": "klines",
    "mark_bars": "markPriceKlines",
    "funding": "fundingRate",
}
_CHECKSUM_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64})[ \t]+[*]?(?P<name>[^/\\]+)$")


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return datetime(value.year + 1, 1, 1, tzinfo=timezone.utc)
    return datetime(value.year, value.month + 1, 1, tzinfo=timezone.utc)


def _periods(request: ArchiveDiscoveryRequest) -> Iterator[tuple[str, datetime, datetime]]:
    if request.frequency == "monthly":
        cursor = datetime(request.start.year, request.start.month, 1, tzinfo=timezone.utc)
        while cursor < request.end:
            following = _next_month(cursor)
            yield cursor.strftime("%Y-%m"), cursor, following
            cursor = following
        return
    cursor = datetime(
        request.start.year,
        request.start.month,
        request.start.day,
        tzinfo=timezone.utc,
    )
    while cursor < request.end:
        following = cursor + timedelta(days=1)
        yield cursor.strftime("%Y-%m-%d"), cursor, following
        cursor = following


def parse_checksum(payload: bytes, expected_filename: str) -> str:
    """Parse the official sha256sum-compatible one-line checksum format."""

    try:
        text = payload.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ChecksumError("checksum file must be UTF-8") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ChecksumError("checksum file must contain exactly one non-empty line")
    match = _CHECKSUM_LINE.fullmatch(lines[0])
    if match is None:
        raise ChecksumError("checksum line must use sha256sum format")
    if match.group("name") != expected_filename:
        raise ChecksumError(
            f"checksum names {match.group('name')!r}, expected {expected_filename!r}"
        )
    return match.group("digest").lower()


def _safe_child(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("raw path escapes its configured root")
    return candidate


def archive_candidates(
    request: ArchiveDiscoveryRequest,
) -> tuple[RemoteArchiveObject, ...]:
    """Generate deterministic candidates without making an HTTP request."""

    kind = _DATASET_PATH[request.dataset_name]
    results: list[RemoteArchiveObject] = []
    for period, period_start, period_end in _periods(request):
        if request.interval is None:
            filename = f"{request.symbol}-{kind}-{period}.zip"
            suffix = f"{kind}/{request.symbol}/{filename}"
        else:
            filename = f"{request.symbol}-{request.interval}-{period}.zip"
            suffix = f"{kind}/{request.symbol}/{request.interval}/{filename}"
        archive_path = f"{request.frequency}/{suffix}"
        url = f"{_BASE_URL}/{archive_path}"
        results.append(
            RemoteArchiveObject(
                dataset_name=request.dataset_name,
                symbol=request.symbol,
                interval=request.interval,
                frequency=request.frequency,
                period=period,
                available_from=period_start,
                available_to=period_end,
                url=url,
                checksum_url=f"{url}.CHECKSUM",
                relative_path=f"binance/futures/um/{archive_path}",
            )
        )
    return tuple(results)


def _object_id(remote: RemoteArchiveObject) -> str:
    pieces = ["archive", remote.dataset_name, remote.symbol]
    if remote.interval is not None:
        pieces.append(remote.interval)
    pieces.extend((remote.frequency, remote.period))
    return "-".join(pieces)


class ArchiveCoverageStatus(str, Enum):
    MISSING = "missing"
    PARTIAL = "partial"
    UNMANIFESTED = "unmanifested"
    ORPHAN_MANIFEST = "orphan_manifest"
    VERIFIED = "verified"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ArchiveCoverageItem:
    remote: RemoteArchiveObject
    status: ArchiveCoverageStatus
    path: Path
    manifest_path: Path


def local_archive_coverage(
    request: ArchiveDiscoveryRequest,
    *,
    raw_root: Path,
    manifest_root: Path,
) -> tuple[ArchiveCoverageItem, ...]:
    """Report local state for every deterministic archive candidate."""

    items: list[ArchiveCoverageItem] = []
    for remote in archive_candidates(request):
        target = _safe_child(raw_root, remote.relative_path)
        manifest_path = _safe_child(manifest_root, f"{_object_id(remote)}.json")
        partial = target.with_name(f".{target.name}.part")
        if not target.is_file() and not manifest_path.is_file():
            status = (
                ArchiveCoverageStatus.PARTIAL
                if partial.is_file()
                else ArchiveCoverageStatus.MISSING
            )
        elif target.is_file() and not manifest_path.is_file():
            status = ArchiveCoverageStatus.UNMANIFESTED
        elif not target.is_file():
            status = ArchiveCoverageStatus.ORPHAN_MANIFEST
        else:
            try:
                manifest = load_manifest(manifest_path, "raw")
                if not isinstance(manifest, RawObjectManifest):
                    raise ValueError("wrong manifest type")
                _validate_zip(target)
                actual = sha256_file(target)
                valid = (
                    manifest.object_id == _object_id(remote)
                    and manifest.source_uri == remote.url
                    and manifest.checksum_sha256 == actual
                    and manifest.byte_size == target.stat().st_size
                )
                status = (
                    ArchiveCoverageStatus.VERIFIED
                    if valid
                    else ArchiveCoverageStatus.CONFLICT
                )
            except (OSError, ValueError, ChecksumError):
                status = ArchiveCoverageStatus.CONFLICT
        items.append(ArchiveCoverageItem(remote, status, target, manifest_path))
    return tuple(items)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if temporary.is_file():
            temporary.unlink()
        raise


def _validate_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if not files:
                raise ChecksumError("archive ZIP contains no files")
            bad = archive.testzip()
            if bad is not None:
                raise ChecksumError(f"archive ZIP member failed CRC: {bad}")
    except zipfile.BadZipFile as exc:
        raise ChecksumError("download is not a valid ZIP archive") from exc


class BinanceArchiveSource:
    """Discover and fetch checksum-backed USD-M public archive objects."""

    def __init__(self, http: PublicHttpClient) -> None:
        self.http = http

    def candidates(
        self, request: ArchiveDiscoveryRequest
    ) -> tuple[RemoteArchiveObject, ...]:
        """Generate deterministic candidates without claiming remote existence."""

        return archive_candidates(request)

    def discover(
        self, request: ArchiveDiscoveryRequest
    ) -> tuple[RemoteArchiveObject, ...]:
        """Probe both ZIP and CHECKSUM without downloading their bodies."""

        available: list[RemoteArchiveObject] = []
        for candidate in self.candidates(request):
            checksum = self.http.request(
                "HEAD", candidate.checksum_url, allow_not_found=True
            )
            if checksum.status_code == 404:
                continue
            archive = self.http.request("HEAD", candidate.url, allow_not_found=True)
            if archive.status_code == 404:
                raise SourceProtocolError(
                    f"checksum exists but archive is missing: {candidate.url}"
                )
            available.append(candidate)
        return tuple(available)

    def fetch(
        self,
        remote: RemoteArchiveObject,
        *,
        raw_root: Path,
        manifest_root: Path,
        catalog: DuckDBCatalog | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> FetchResult:
        checksum_response = self.http.request("GET", remote.checksum_url)
        expected = parse_checksum(checksum_response.content, Path(remote.url).name)
        target = _safe_child(raw_root, remote.relative_path)
        checksum_path = target.with_name(f"{target.name}.CHECKSUM")
        object_id = _object_id(remote)
        manifest_path = _safe_child(manifest_root, f"{object_id}.json")

        existing_manifest: RawObjectManifest | None = None
        if manifest_path.is_file():
            loaded = load_manifest(manifest_path, "raw")
            if not isinstance(loaded, RawObjectManifest):
                raise RawObjectConflictError(f"unexpected manifest type: {manifest_path}")
            existing_manifest = loaded

        if target.is_file():
            actual = sha256_file(target)
            if actual != expected:
                raise RawObjectConflictError(
                    f"immutable raw object differs from upstream checksum: {target}"
                )
            _validate_zip(target)
            if existing_manifest is None:
                manifest = self._manifest(remote, object_id, target, actual, 200, now())
                _write_atomic(manifest_path, manifest_json(manifest).encode("utf-8"))
            else:
                manifest = existing_manifest
                self._assert_existing_manifest(manifest, remote, expected, target)
            _write_atomic(checksum_path, checksum_response.content)
            registered = catalog.register_raw(manifest).inserted if catalog else None
            return FetchResult(
                status=FetchStatus.SKIPPED,
                object_id=object_id,
                path=str(target),
                manifest_path=str(manifest_path),
                http_status=manifest.http_status,
                byte_size=target.stat().st_size,
                checksum_sha256=actual,
                upstream_checksum_sha256=expected,
                retrieved_at=manifest.retrieved_at,
                catalog_inserted=registered,
            )

        temporary = target.with_name(f".{target.name}.part")
        status, etag = self.http.download(remote.url, temporary)
        actual = sha256_file(temporary)
        if actual != expected:
            raise ChecksumError(
                f"archive checksum mismatch for {remote.url}: {actual} != {expected}"
            )
        _validate_zip(temporary)
        if existing_manifest is not None:
            # Validate an orphan manifest before publishing a new formal object.
            self._assert_existing_manifest(existing_manifest, remote, expected, temporary)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, target)
        _write_atomic(checksum_path, checksum_response.content)
        if existing_manifest is not None:
            manifest = existing_manifest
        else:
            manifest = self._manifest(remote, object_id, target, actual, status, now())
            _write_atomic(manifest_path, manifest_json(manifest).encode("utf-8"))
        registered = catalog.register_raw(manifest).inserted if catalog else None
        return FetchResult(
            status=FetchStatus.DOWNLOADED,
            object_id=object_id,
            path=str(target),
            manifest_path=str(manifest_path),
            http_status=status,
            byte_size=target.stat().st_size,
            checksum_sha256=actual,
            upstream_checksum_sha256=expected,
            retrieved_at=manifest.retrieved_at,
            etag=etag,
            catalog_inserted=registered,
        )

    @staticmethod
    def _manifest(
        remote: RemoteArchiveObject,
        object_id: str,
        target: Path,
        digest: str,
        http_status: int,
        retrieved_at: datetime,
    ) -> RawObjectManifest:
        return RawObjectManifest(
            object_id=object_id,
            dataset_name=remote.dataset_name,
            source="binance_public_archive",
            source_uri=remote.url,
            symbol=remote.symbol,
            interval=remote.interval,
            available_from=remote.available_from,
            available_to=remote.available_to,
            retrieved_at=retrieved_at,
            byte_size=target.stat().st_size,
            checksum_sha256=digest,
            upstream_checksum_sha256=digest,
            media_type="application/zip",
            compression="zip",
            http_status=http_status,
        )

    @staticmethod
    def _assert_existing_manifest(
        manifest: RawObjectManifest,
        remote: RemoteArchiveObject,
        digest: str,
        target: Path,
    ) -> None:
        if (
            manifest.object_id != _object_id(remote)
            or manifest.dataset_name != remote.dataset_name
            or manifest.source != "binance_public_archive"
            or manifest.source_uri != remote.url
            or manifest.symbol != remote.symbol
            or manifest.interval != remote.interval
            or manifest.available_from != remote.available_from
            or manifest.available_to != remote.available_to
            or manifest.checksum_sha256 != digest
            or manifest.upstream_checksum_sha256 != digest
            or manifest.byte_size != target.stat().st_size
        ):
            raise RawObjectConflictError(
                f"existing manifest conflicts with immutable raw object: {target}"
            )
