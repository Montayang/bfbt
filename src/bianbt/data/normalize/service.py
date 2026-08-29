"""Application service for Raw normalization and quality-gated publication."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.manifests import RawObjectManifest, load_manifest
from bianbt.data.normalize.core import (
    NormalizationError,
    NormalizationRelease,
    normalize_bars,
    normalize_contracts,
    normalize_funding,
)
from bianbt.data.publisher import ParquetPublisher, PublicationResult
from bianbt.data.validation.reports import QualityPolicy


class NormalizationService:
    def __init__(self, publisher: ParquetPublisher | None = None) -> None:
        self.publisher = publisher or ParquetPublisher()

    def run(
        self,
        dataset_name: Literal["bars", "mark_bars", "funding", "contracts"],
        raw_manifest_paths: tuple[Path, ...],
        *,
        raw_root: Path,
        normalized_root: Path,
        partition_manifest_root: Path,
        quality_root: Path,
        catalog: DuckDBCatalog | None = None,
        policy: QualityPolicy | None = None,
        compression: str = "zstd",
        row_group_rows: int = 262_144,
        release: NormalizationRelease | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> PublicationResult:
        if not raw_manifest_paths:
            raise NormalizationError("at least one Raw manifest path is required")
        manifests: list[RawObjectManifest] = []
        for path in raw_manifest_paths:
            manifest = load_manifest(path, "raw")
            if not isinstance(manifest, RawObjectManifest):
                raise NormalizationError(f"not a Raw object manifest: {path}")
            manifests.append(manifest)
        if dataset_name in {"bars", "mark_bars"}:
            batch = normalize_bars(
                manifests,
                raw_root=raw_root,
                dataset_name=dataset_name,
                release=release,
            )
        elif dataset_name == "funding":
            batch = normalize_funding(manifests, raw_root=raw_root, release=release)
        else:
            batch = normalize_contracts(manifests, raw_root=raw_root, release=release)
        if catalog is not None:
            for manifest in batch.source_manifests:
                catalog.register_raw(manifest)
        return self.publisher.publish(
            batch,
            normalized_root=normalized_root,
            partition_manifest_root=partition_manifest_root,
            quality_root=quality_root,
            policy=policy,
            compression=compression,
            row_group_rows=row_group_rows,
            catalog=catalog,
            now=now,
        )
