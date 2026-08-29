"""Lazy, version-pinned Polars scans over published normalized Parquet parts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable

import polars as pl

from bianbt.config.common import as_utc
from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.hashing import sha256_file
from bianbt.data.manifests import DatasetName, PartitionManifest
from bianbt.data.schemas import get_schema_definition


class DataStoreError(ValueError):
    """A version-pinned normalized data scan cannot be constructed safely."""


def _safe_child(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise DataStoreError("partition path escapes normalized root")
    return candidate


def _range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    checked_start = as_utc(start)
    checked_end = as_utc(end)
    assert checked_start is not None and checked_end is not None
    if checked_end <= checked_start:
        raise DataStoreError("end must be greater than start")
    return checked_start, checked_end


class ParquetDataStore:
    def __init__(
        self,
        *,
        normalized_root: Path,
        catalog: DuckDBCatalog,
        verify_hashes: bool = False,
    ) -> None:
        self.normalized_root = normalized_root
        self.catalog = catalog
        self.verify_hashes = verify_hashes

    def _paths(
        self,
        manifests: Iterable[PartitionManifest],
    ) -> list[str]:
        paths: list[str] = []
        for manifest in manifests:
            path = _safe_child(self.normalized_root, manifest.partition_path)
            if not path.is_file():
                raise DataStoreError(f"partition file is missing: {path}")
            if self.verify_hashes and sha256_file(path) != manifest.content_sha256:
                raise DataStoreError(f"partition checksum mismatch: {path}")
            paths.append(str(path))
        if not paths:
            raise DataStoreError("no partitions overlap the requested constraints")
        return paths

    def _scan(
        self,
        *,
        dataset_name: DatasetName,
        dataset_version: str,
        time_column: str,
        start: datetime,
        end: datetime,
        interval: str | None,
        columns: tuple[str, ...] | None,
        symbols: tuple[str, ...] | None,
    ) -> pl.LazyFrame:
        start, end = _range(start, end)
        definition = get_schema_definition(dataset_name, "v1")
        if columns is not None:
            if not columns or len(columns) != len(set(columns)):
                raise DataStoreError("columns must be non-empty and unique")
            unknown = set(columns) - set(definition.schema.names)
            if unknown:
                raise DataStoreError(f"unknown {dataset_name} columns: {sorted(unknown)}")
        manifests = self.catalog.resolve_partitions(dataset_name, dataset_version)
        selected = tuple(
            item
            for item in manifests
            if (interval is None or item.partition_values.get("interval") == interval)
            and item.min_time is not None
            and item.max_time is not None
            and item.max_time >= start
            and item.min_time < end
        )
        frame = pl.scan_parquet(
            self._paths(selected),
            hive_partitioning=False,
            glob=False,
        ).filter(
            (pl.col(time_column) >= pl.lit(start))
            & (pl.col(time_column) < pl.lit(end))
        )
        if interval is not None:
            frame = frame.filter(pl.col("interval") == interval)
        if symbols is not None:
            normalized = tuple(item.upper() for item in symbols)
            if not normalized:
                raise DataStoreError("symbols must not be empty")
            frame = frame.filter(pl.col("symbol").is_in(normalized))
        if columns is not None:
            frame = frame.select(columns)
        return frame

    def scan_bars(
        self,
        *,
        dataset_version: str,
        start: datetime,
        end: datetime,
        interval: str,
        columns: tuple[str, ...] | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> pl.LazyFrame:
        return self._scan(
            dataset_name="bars",
            dataset_version=dataset_version,
            time_column="open_time",
            start=start,
            end=end,
            interval=interval,
            columns=columns,
            symbols=symbols,
        )

    def scan_mark_bars(
        self,
        *,
        dataset_version: str,
        start: datetime,
        end: datetime,
        interval: str,
        columns: tuple[str, ...] | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> pl.LazyFrame:
        return self._scan(
            dataset_name="mark_bars",
            dataset_version=dataset_version,
            time_column="open_time",
            start=start,
            end=end,
            interval=interval,
            columns=columns,
            symbols=symbols,
        )

    def scan_funding(
        self,
        *,
        dataset_version: str,
        start: datetime,
        end: datetime,
        columns: tuple[str, ...] | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> pl.LazyFrame:
        return self._scan(
            dataset_name="funding",
            dataset_version=dataset_version,
            time_column="funding_time",
            start=start,
            end=end,
            interval=None,
            columns=columns,
            symbols=symbols,
        )

    def scan_contracts(
        self,
        *,
        dataset_version: str,
        start: datetime,
        end: datetime,
        columns: tuple[str, ...] | None = None,
        symbols: tuple[str, ...] | None = None,
    ) -> pl.LazyFrame:
        return self._scan(
            dataset_name="contracts",
            dataset_version=dataset_version,
            time_column="snapshot_time",
            start=start,
            end=end,
            interval=None,
            columns=columns,
            symbols=symbols,
        )
