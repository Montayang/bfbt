"""Immutable, content-verified analysis and signal snapshot artifacts."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import polars as pl
import pyarrow.parquet as pq
from pydantic import Field, field_validator, model_validator

from bfbt.config.common import StrictModel, as_utc
from bfbt.data.hashing import content_sha256, sha256_file

REUSE_ARTIFACT_VERSION = "a27-reuse-v1"


class ReuseArtifactError(RuntimeError):
    """A reusable snapshot is incomplete, incompatible, or tampered with."""


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError("snapshot file path must be safe and relative")
    return path.as_posix()


class SnapshotPart(StrictModel):
    path: str
    row_count: int = Field(ge=0)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative(value)


class AnalysisSnapshotManifest(StrictModel):
    manifest_version: Literal["analysis-snapshot/v1"] = "analysis-snapshot/v1"
    artifact_version: Literal["a27-reuse-v1"] = REUSE_ARTIFACT_VERSION
    analysis_id: str = Field(pattern=r"^analysis-[0-9a-f]{24}$")
    created_at: datetime
    dataset_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: datetime
    end: datetime
    factor_name: str = Field(min_length=1)
    factor_version: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    tables: dict[str, tuple[SnapshotPart, ...]]

    @field_validator("created_at", "start", "end")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_tables(self) -> "AnalysisSnapshotManifest":
        if self.end <= self.start:
            raise ValueError("analysis snapshot end must be after start")
        required = {"factor_values", "universe"}
        if not required <= set(self.tables):
            raise ValueError("analysis snapshot requires factor_values and universe")
        if any(not parts for parts in self.tables.values()):
            raise ValueError("analysis snapshot tables must contain parts")
        return self


class SignalSnapshotManifest(StrictModel):
    manifest_version: Literal["signal-snapshot/v1"] = "signal-snapshot/v1"
    artifact_version: Literal["a27-reuse-v1"] = REUSE_ARTIFACT_VERSION
    signal_id: str = Field(pattern=r"^signal-[0-9a-f]{24}$")
    created_at: datetime
    analysis_id: str = Field(pattern=r"^analysis-[0-9a-f]{24}$")
    analysis_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start: datetime
    end: datetime
    factor_version: str = Field(min_length=1)
    universe_version: str = Field(min_length=1)
    tables: dict[str, tuple[SnapshotPart, ...]]

    @field_validator("created_at", "start", "end")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_tables(self) -> "SignalSnapshotManifest":
        if self.end <= self.start:
            raise ValueError("signal snapshot end must be after start")
        if "selections" not in self.tables or not self.tables["selections"]:
            raise ValueError("signal snapshot requires selections")
        return self


ReuseManifest = AnalysisSnapshotManifest | SignalSnapshotManifest


def reuse_manifest_sha256(manifest: ReuseManifest) -> str:
    return content_sha256(manifest)


class ReusableSnapshotStore:
    """Publish and verify reusable tables without mutating prior snapshots."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if self.root in {Path("/"), Path.home().resolve()}:
            raise ReuseArtifactError(f"unsafe reusable snapshot root: {self.root}")
        self.root.mkdir(parents=True, exist_ok=True)

    def directory(self, kind: Literal["analysis", "signal"], identity: str) -> Path:
        if "/" in identity or ".." in identity:
            raise ReuseArtifactError("unsafe reusable snapshot identity")
        return self.root / kind / identity

    def load_analysis(self, analysis_id: str) -> AnalysisSnapshotManifest | None:
        path = self.directory("analysis", analysis_id)
        if not path.exists():
            return None
        manifest = AnalysisSnapshotManifest.model_validate_json(
            (path / "manifest.json").read_text(encoding="utf-8")
        )
        if manifest.analysis_id != analysis_id:
            raise ReuseArtifactError("analysis directory identity mismatch")
        self.verify(path, manifest)
        return manifest

    def load_signal(self, signal_id: str) -> SignalSnapshotManifest | None:
        path = self.directory("signal", signal_id)
        if not path.exists():
            return None
        manifest = SignalSnapshotManifest.model_validate_json(
            (path / "manifest.json").read_text(encoding="utf-8")
        )
        if manifest.signal_id != signal_id:
            raise ReuseArtifactError("signal directory identity mismatch")
        self.verify(path, manifest)
        return manifest

    def verify(self, directory: Path, manifest: ReuseManifest) -> None:
        root = directory.resolve()
        for table, parts in manifest.tables.items():
            for part in parts:
                path = (root / part.path).resolve()
                try:
                    path.relative_to(root)
                except ValueError as exc:
                    raise ReuseArtifactError("snapshot part escapes root") from exc
                if not path.is_file():
                    raise ReuseArtifactError(f"snapshot part is missing: {part.path}")
                if path.stat().st_size != part.byte_size:
                    raise ReuseArtifactError(f"snapshot byte size mismatch: {part.path}")
                if sha256_file(path) != part.sha256:
                    raise ReuseArtifactError(f"snapshot hash mismatch: {part.path}")
                rows = int(pq.ParquetFile(path).metadata.num_rows)
                if rows != part.row_count:
                    raise ReuseArtifactError(f"snapshot row count mismatch: {part.path}")
            expected_prefix = f"tables/{table}/"
            if any(not part.path.startswith(expected_prefix) for part in parts):
                raise ReuseArtifactError("snapshot table path does not match table")

    def scan(self, directory: Path, parts: tuple[SnapshotPart, ...]) -> pl.LazyFrame:
        return pl.concat(
            [
                pl.scan_parquet(directory / part.path, hive_partitioning=False)
                for part in parts
            ],
            how="vertical",
        )

    def publish_analysis(
        self,
        *,
        analysis_id: str,
        dataset_manifest_sha256: str,
        dependency_sha256: str,
        start: datetime,
        end: datetime,
        factor_name: str,
        factor_version: str,
        universe_version: str,
        tables: dict[str, tuple[Path, ...]],
    ) -> AnalysisSnapshotManifest:
        existing = self.load_analysis(analysis_id)
        if existing is not None:
            if existing.dependency_sha256 != dependency_sha256:
                raise ReuseArtifactError("analysis identity collision")
            return existing
        return self._publish(
            kind="analysis",
            identity=analysis_id,
            tables=tables,
            factory=lambda parts: AnalysisSnapshotManifest(
                analysis_id=analysis_id,
                created_at=datetime.now(UTC),
                dataset_manifest_sha256=dataset_manifest_sha256,
                dependency_sha256=dependency_sha256,
                start=start,
                end=end,
                factor_name=factor_name,
                factor_version=factor_version,
                universe_version=universe_version,
                tables=parts,
            ),
        )

    def publish_signal(
        self,
        *,
        signal_id: str,
        analysis_id: str,
        analysis_manifest_sha256: str,
        dependency_sha256: str,
        start: datetime,
        end: datetime,
        factor_version: str,
        universe_version: str,
        tables: dict[str, tuple[Path, ...]],
    ) -> SignalSnapshotManifest:
        existing = self.load_signal(signal_id)
        if existing is not None:
            if (
                existing.dependency_sha256 != dependency_sha256
                or existing.analysis_manifest_sha256 != analysis_manifest_sha256
            ):
                raise ReuseArtifactError("signal identity collision or wrong parent")
            return existing
        return self._publish(
            kind="signal",
            identity=signal_id,
            tables=tables,
            factory=lambda parts: SignalSnapshotManifest(
                signal_id=signal_id,
                created_at=datetime.now(UTC),
                analysis_id=analysis_id,
                analysis_manifest_sha256=analysis_manifest_sha256,
                dependency_sha256=dependency_sha256,
                start=start,
                end=end,
                factor_version=factor_version,
                universe_version=universe_version,
                tables=parts,
            ),
        )

    def _publish(self, *, kind: str, identity: str, tables, factory):
        parent = self.root / kind
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{identity}-", dir=parent))
        final = parent / identity
        try:
            published: dict[str, tuple[SnapshotPart, ...]] = {}
            for table in sorted(tables):
                rows: list[SnapshotPart] = []
                for ordinal, source in enumerate(tables[table]):
                    target = staging / "tables" / table / f"part-{ordinal:06d}.parquet"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    with target.open("rb") as stream:
                        os.fsync(stream.fileno())
                    rows.append(
                        SnapshotPart(
                            path=target.relative_to(staging).as_posix(),
                            row_count=int(pq.ParquetFile(target).metadata.num_rows),
                            byte_size=target.stat().st_size,
                            sha256=sha256_file(target),
                        )
                    )
                published[table] = tuple(rows)
            manifest = factory(published)
            manifest_path = staging / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    manifest.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )
            with manifest_path.open("rb") as stream:
                os.fsync(stream.fileno())
            staging.replace(final)
            self.verify(final, manifest)
            return manifest
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
