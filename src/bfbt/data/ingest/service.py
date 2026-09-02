"""Ingestion orchestration that keeps network and Catalog writes separated."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.manifests import RawObjectManifest, load_manifest
from bfbt.data.sources.base import ArchiveDiscoveryRequest, FetchResult
from bfbt.data.sources.binance_archive import BinanceArchiveSource


class ArchiveIngestService:
    """Discover, concurrently fetch, then sequentially register raw archives."""

    def __init__(self, source: BinanceArchiveSource) -> None:
        self.source = source

    def sync(
        self,
        request: ArchiveDiscoveryRequest,
        *,
        raw_root: Path,
        manifest_root: Path,
        catalog: DuckDBCatalog | None = None,
        max_workers: int = 4,
    ) -> tuple[FetchResult, ...]:
        if not 1 <= max_workers <= 64:
            raise ValueError("max_workers must be between 1 and 64")
        objects = self.source.discover(request)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(
                    self.source.fetch,
                    remote,
                    raw_root=raw_root,
                    manifest_root=manifest_root,
                )
                for remote in objects
            ]
            results = tuple(future.result() for future in futures)
        if catalog is None:
            return results
        registered: list[FetchResult] = []
        for result in results:
            manifest = load_manifest(Path(result.manifest_path), "raw")
            if not isinstance(manifest, RawObjectManifest):
                raise TypeError(f"unexpected manifest type: {result.manifest_path}")
            inserted = catalog.register_raw(manifest).inserted
            registered.append(result.model_copy(update={"catalog_inserted": inserted}))
        return tuple(registered)
