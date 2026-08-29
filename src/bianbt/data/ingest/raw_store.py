"""Atomic publication of immutable public REST responses and manifests."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.hashing import sha256_bytes, sha256_file
from bianbt.data.manifests import RawObjectManifest, load_manifest, manifest_json
from bianbt.data.sources.base import (
    FetchResult,
    FetchStatus,
    RawObjectConflictError,
    RestPage,
)


def _safe_child(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("raw path escapes its configured root")
    return candidate


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


def _rest_kind(page: RestPage) -> str:
    return {
        "/fapi/v1/klines": "klines",
        "/fapi/v1/markPriceKlines": "markPriceKlines",
        "/fapi/v1/fundingRate": "fundingRate",
        "/fapi/v1/exchangeInfo": "exchangeInfo",
        "/fapi/v1/fundingInfo": "fundingInfo",
    }.get(page.endpoint, page.endpoint.strip("/").replace("/", "-"))


def _identity(page: RestPage, digest: str) -> str:
    kind = _rest_kind(page)
    parts = ["rest", page.dataset_name, kind]
    if page.symbol is not None:
        parts.append(page.symbol)
    if page.interval is not None:
        parts.append(page.interval)
    if page.endpoint in {"/fapi/v1/exchangeInfo", "/fapi/v1/fundingInfo"}:
        observed = page.retrieved_at.strftime("%Y%m%dT%H%M%S.%fZ")
        parts.append(observed)
    request_fingerprint = sha256_bytes(page.source_uri.encode("utf-8"))[:16]
    parts.extend((request_fingerprint, digest))
    return "-".join(parts)


def _relative_path(page: RestPage, object_id: str) -> str:
    kind = _rest_kind(page)
    parts = ["binance", "futures", "um", "rest", kind]
    if page.symbol is not None:
        parts.append(page.symbol)
    if page.interval is not None:
        parts.append(page.interval)
    parts.append(f"{object_id}.json")
    return "/".join(parts)


class RawRestStore:
    """Persist exact REST response bytes before any normalization."""

    def publish(
        self,
        page: RestPage,
        *,
        raw_root: Path,
        manifest_root: Path,
        catalog: DuckDBCatalog | None = None,
    ) -> FetchResult:
        if not page.response_body:
            raise ValueError("REST response body must not be empty")
        digest = sha256_bytes(page.response_body)
        object_id = _identity(page, digest)
        target = _safe_child(raw_root, _relative_path(page, object_id))
        manifest_path = _safe_child(manifest_root, f"{object_id}.json")
        status = FetchStatus.DOWNLOADED

        if target.is_file():
            if sha256_file(target) != digest:
                raise RawObjectConflictError(
                    f"immutable REST raw object has changed: {target}"
                )
            status = FetchStatus.SKIPPED
        else:
            _write_atomic(target, page.response_body)

        source = (
            "binance_exchange_info"
            if page.endpoint == "/fapi/v1/exchangeInfo"
            else "binance_rest"
        )
        manifest = RawObjectManifest(
            object_id=object_id,
            dataset_name=page.dataset_name,
            source=source,
            source_uri=page.source_uri,
            symbol=page.symbol,
            interval=page.interval,
            available_from=page.available_from,
            available_to=page.available_to,
            retrieved_at=page.retrieved_at,
            byte_size=len(page.response_body),
            checksum_sha256=digest,
            upstream_checksum_sha256=None,
            media_type="application/json",
            compression="none",
            http_status=page.http_status,
        )
        if manifest_path.is_file():
            existing = load_manifest(manifest_path, "raw")
            if not isinstance(existing, RawObjectManifest) or any(
                (
                    existing.object_id != manifest.object_id,
                    existing.dataset_name != manifest.dataset_name,
                    existing.source != manifest.source,
                    existing.source_uri != manifest.source_uri,
                    existing.symbol != manifest.symbol,
                    existing.interval != manifest.interval,
                    existing.available_from != manifest.available_from,
                    existing.available_to != manifest.available_to,
                    existing.byte_size != manifest.byte_size,
                    existing.checksum_sha256 != manifest.checksum_sha256,
                )
            ):
                raise RawObjectConflictError(
                    f"REST manifest conflicts with immutable object: {manifest_path}"
                )
            manifest = existing
        else:
            _write_atomic(manifest_path, manifest_json(manifest).encode("utf-8"))

        registered = catalog.register_raw(manifest).inserted if catalog else None
        return FetchResult(
            status=status,
            object_id=object_id,
            path=str(target),
            manifest_path=str(manifest_path),
            http_status=page.http_status,
            byte_size=len(page.response_body),
            checksum_sha256=digest,
            upstream_checksum_sha256=None,
            retrieved_at=manifest.retrieved_at,
            catalog_inserted=registered,
        )

    def publish_all(
        self,
        pages: Iterable[RestPage],
        *,
        raw_root: Path,
        manifest_root: Path,
        catalog: DuckDBCatalog | None = None,
    ) -> tuple[FetchResult, ...]:
        return tuple(
            self.publish(
                page,
                raw_root=raw_root,
                manifest_root=manifest_root,
                catalog=catalog,
            )
            for page in pages
        )
