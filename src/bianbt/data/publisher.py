"""Quality-gated, atomic publication of immutable normalized Parquet parts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.hashing import sha256_file
from bianbt.data.manifests import PartitionManifest, load_manifest, manifest_json
from bianbt.data.normalize.core import NormalizedBatch
from bianbt.data.schemas import get_schema_definition
from bianbt.data.validation.reports import (
    QualityError,
    QualityPolicy,
    QualityReport,
    evaluate_quality,
)


class PublicationConflictError(ValueError):
    """An immutable normalized artifact conflicts with an existing artifact."""


@dataclass(frozen=True)
class PublicationResult:
    partition_manifest: PartitionManifest
    quality_report: QualityReport
    parquet_path: Path
    partition_manifest_path: Path
    quality_report_path: Path
    published: bool
    catalog_inserted: bool | None


def _safe_child(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("publication path escapes its configured root")
    return candidate


def _atomic_bytes(path: Path, payload: bytes) -> None:
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


def _load_quality(path: Path) -> QualityReport:
    try:
        return QualityReport.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise PublicationConflictError(f"invalid existing quality report: {path}") from exc


def _quality_bytes(report: QualityReport) -> bytes:
    return (
        json.dumps(
            report.model_dump(mode="json", exclude_none=False),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


_QUALITY_IDENTITY_FIELDS = (
    "report_id",
    "dataset_name",
    "dataset_version",
    "status",
    "row_count",
    "symbols_count",
    "duplicate_keys",
    "null_required",
    "invalid_numeric",
    "bad_ohlc",
    "non_positive_prices",
    "negative_values",
    "inconsistent_intervals",
    "missing_bars",
    "missing_ratio",
    "source_object_ids",
    "errors",
)

_PARTITION_IDENTITY_FIELDS = (
    "partition_id",
    "dataset_name",
    "schema_version",
    "schema_fingerprint",
    "dataset_version",
    "partition_path",
    "partition_values",
    "row_count",
    "min_time",
    "max_time",
    "symbols_count",
    "content_sha256",
    "source_object_ids",
    "quality_report_id",
)


class ParquetPublisher:
    def publish(
        self,
        batch: NormalizedBatch,
        *,
        normalized_root: Path,
        partition_manifest_root: Path,
        quality_root: Path,
        policy: QualityPolicy | None = None,
        compression: str = "zstd",
        row_group_rows: int = 262_144,
        catalog: DuckDBCatalog | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> PublicationResult:
        if row_group_rows < 1:
            raise ValueError("row_group_rows must be positive")
        policy = policy or QualityPolicy()
        report = evaluate_quality(batch, policy=policy, evaluated_at=now())
        report_path = _safe_child(quality_root, f"{report.report_id}.json")
        if report_path.is_file():
            existing_report = _load_quality(report_path)
            if any(
                getattr(existing_report, key) != getattr(report, key)
                for key in _QUALITY_IDENTITY_FIELDS
            ):
                raise PublicationConflictError(
                    f"quality report ID conflicts with existing content: {report_path}"
                )
            report = existing_report
        else:
            _atomic_bytes(report_path, _quality_bytes(report))
        if report.status != "pass":
            raise QualityError(
                f"quality report {report.report_id} failed: {', '.join(report.errors)}"
            )

        values = batch.partition_values
        path_parts = [
            batch.dataset_name,
            f"schema={batch.schema_version}",
            f"dataset_version={batch.dataset_version}",
        ]
        if "interval" in values:
            path_parts.append(f"interval={values['interval']}")
        path_parts.extend((f"year={values['year']}", f"month={values['month']}"))
        part_key = report.report_id.rsplit("-", 1)[-1][:20]
        relative = "/".join(path_parts + [f"part-{part_key}.parquet"])
        target = _safe_child(normalized_root, relative)
        partition_id = (
            f"{batch.dataset_name}-{batch.dataset_version}-"
            f"{values.get('interval', 'na')}-{values['year']}-{values['month']}-"
            f"{part_key}"
        )
        manifest_path = _safe_child(partition_manifest_root, f"{partition_id}.json")
        temporary = target.with_name(f".{target.name}.tmp")
        created = False
        if target.is_file():
            if not manifest_path.is_file():
                raise PublicationConflictError(
                    f"normalized target exists without its manifest: {target}"
                )
            content_hash = sha256_file(target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                pq.write_table(
                    batch.table,
                    temporary,
                    compression=compression,
                    row_group_size=row_group_rows,
                    version="2.6",
                    write_statistics=True,
                )
                content_hash = sha256_file(temporary)
                os.replace(temporary, target)
                created = True
            except Exception:
                if temporary.is_file():
                    temporary.unlink()
                raise

        definition = get_schema_definition(batch.dataset_name, batch.schema_version)
        time_values = batch.table.column(batch.time_column).to_pylist()
        candidate = PartitionManifest(
            partition_id=partition_id,
            dataset_name=batch.dataset_name,
            schema_version=batch.schema_version,
            schema_fingerprint=definition.fingerprint,
            dataset_version=batch.dataset_version,
            partition_path=relative,
            partition_values=values,
            row_count=batch.table.num_rows,
            min_time=min(time_values),
            max_time=max(time_values),
            symbols_count=len(set(batch.table.column("symbol").to_pylist())),
            content_sha256=content_hash,
            source_object_ids=tuple(item.object_id for item in batch.source_manifests),
            quality_report_id=report.report_id,
            published_at=now(),
        )
        if manifest_path.is_file():
            existing = load_manifest(manifest_path, "partition")
            if not isinstance(existing, PartitionManifest):
                raise PublicationConflictError(f"wrong manifest type: {manifest_path}")
            if any(
                getattr(existing, key) != getattr(candidate, key)
                for key in _PARTITION_IDENTITY_FIELDS
            ):
                raise PublicationConflictError(
                    f"partition manifest conflicts with immutable output: {manifest_path}"
                )
            manifest = existing
        else:
            manifest = candidate
            _atomic_bytes(manifest_path, manifest_json(manifest).encode("utf-8"))
        inserted = catalog.register_partition(manifest).inserted if catalog else None
        return PublicationResult(
            partition_manifest=manifest,
            quality_report=report,
            parquet_path=target,
            partition_manifest_path=manifest_path,
            quality_report_path=report_path,
            published=created,
            catalog_inserted=inserted,
        )
