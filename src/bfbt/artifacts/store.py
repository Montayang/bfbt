"""Atomic, hash-verified publication of terminal backtest runs."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

import polars as pl
from pydantic import BaseModel

from bfbt.artifacts.environment import EnvironmentInfo
from bfbt.config.backtest import BacktestOutputConfig
from bfbt.data.catalog import DuckDBCatalog, RegistrationResult
from bfbt.data.hashing import content_sha256, sha256_file
from bfbt.data.manifests import (
    ArtifactHash,
    DatasetSnapshotManifest,
    FactorVersionReference,
    RunDatasetReference,
    RunManifest,
    SchemaVersionReference,
    load_manifest,
    manifest_json,
    manifest_sha256,
)
from bfbt.engine.vectorized import BacktestResult
from bfbt.metrics.summary import RunMetrics, compute_run_metrics
from bfbt.reports.renderer import REPORT_VERSION, render_report_from_artifacts

ARTIFACT_CODE_VERSION = "a09-artifacts-v1"


class ArtifactStoreError(RuntimeError):
    """A run directory cannot be safely or atomically published."""


@dataclass(frozen=True)
class PublishedRun:
    path: Path
    manifest: RunManifest
    metrics: Mapping[str, object] | None
    catalog_registration: RegistrationResult | None
    already_published: bool


def final_run_id(
    *,
    engine_run_id: str,
    engine_result_hash: str,
    resolved_config_hash: str,
    dataset_manifest_hash: str,
    factor_versions: tuple[FactorVersionReference, ...],
    environment: EnvironmentInfo,
) -> str:
    identity = {
        "artifact_code_version": ARTIFACT_CODE_VERSION,
        "report_version": REPORT_VERSION,
        "engine_run_id": engine_run_id,
        "engine_result_hash": engine_result_hash,
        "resolved_config_hash": resolved_config_hash,
        "dataset_manifest_hash": dataset_manifest_hash,
        "factor_versions": [item.model_dump(mode="json") for item in factor_versions],
        "git_commit": environment.git_commit,
        "source_fingerprint": environment.source_fingerprint,
        "dependency_fingerprint": environment.dependency_fingerprint,
    }
    return f"a09-{content_sha256(identity)[:24]}"


def failed_run_id(
    *,
    resolved_config_hash: str,
    dataset_manifest_hash: str,
    factor_versions: tuple[FactorVersionReference, ...],
    environment: EnvironmentInfo,
) -> str:
    identity = {
        "artifact_code_version": ARTIFACT_CODE_VERSION,
        "status": "failed",
        "resolved_config_hash": resolved_config_hash,
        "dataset_manifest_hash": dataset_manifest_hash,
        "factor_versions": [item.model_dump(mode="json") for item in factor_versions],
        "git_commit": environment.git_commit,
        "source_fingerprint": environment.source_fingerprint,
        "dependency_fingerprint": environment.dependency_fingerprint,
    }
    return f"a09-failed-{content_sha256(identity)[:17]}"


class RunArtifactStore:
    """Publish immutable terminal run directories below one explicit root."""

    def __init__(
        self,
        root: Path,
        *,
        catalog: DuckDBCatalog | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        resolved = root.resolve()
        if resolved in {Path("/"), Path.home().resolve()}:
            raise ArtifactStoreError(f"unsafe artifact root: {resolved}")
        self.root = resolved
        self.catalog = catalog
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _terminal_time(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ArtifactStoreError("artifact clock must return timezone-aware UTC")
        if value.utcoffset().total_seconds() != 0:
            raise ArtifactStoreError("artifact clock must return UTC")
        return value

    def _staging_directory(self, run_id: str) -> Path:
        staging_root = self.root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        path = staging_root / f"{run_id}-{uuid.uuid4().hex}"
        path.mkdir()
        return path

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_table(
        root: Path,
        relative: str,
        frame: pl.LazyFrame,
        *,
        run_id: str | None,
        sort_by: tuple[str, ...],
    ) -> None:
        names = frame.collect_schema().names()
        output = frame
        if run_id is not None and "run_id" in names:
            output = output.with_columns(pl.lit(run_id).alias("run_id"))
        available_sort = [column for column in sort_by if column in names]
        if available_sort:
            output = output.sort(available_sort)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        output.sink_parquet(
            path,
            compression="zstd",
            statistics=True,
            engine="streaming",
        )

    @staticmethod
    def _artifact_hashes(root: Path) -> tuple[ArtifactHash, ...]:
        artifacts = []
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            if relative == "manifest.json":
                continue
            artifacts.append(
                ArtifactHash(
                    path=relative,
                    byte_size=path.stat().st_size,
                    sha256=sha256_file(path),
                )
            )
        return tuple(artifacts)

    @staticmethod
    def verify(directory: Path, manifest: RunManifest) -> None:
        root = directory.resolve()
        expected = {artifact.path for artifact in manifest.artifact_hashes}
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and path.relative_to(root).as_posix() != "manifest.json"
        }
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ArtifactStoreError(
                f"artifact set mismatch; missing={missing}, extra={extra}"
            )
        for artifact in manifest.artifact_hashes:
            path = root / artifact.path
            if not path.is_file():
                raise ArtifactStoreError(f"missing run artifact: {artifact.path}")
            if path.stat().st_size != artifact.byte_size:
                raise ArtifactStoreError(f"artifact size mismatch: {artifact.path}")
            if sha256_file(path) != artifact.sha256:
                raise ArtifactStoreError(f"artifact hash mismatch: {artifact.path}")

    def _existing(
        self,
        run_id: str,
        *,
        expected_config_hash: str,
        expected_dataset_hash: str,
    ) -> PublishedRun | None:
        final = self.root / run_id
        if not final.exists():
            return None
        if not final.is_dir():
            raise ArtifactStoreError(f"run target is not a directory: {final}")
        manifest = load_manifest(final / "manifest.json", "run")
        assert isinstance(manifest, RunManifest)
        if manifest.run_id != run_id:
            raise ArtifactStoreError("existing manifest run_id does not match directory")
        if manifest.resolved_config_hash != expected_config_hash:
            raise ArtifactStoreError("existing run has a different config hash")
        references = {item.manifest_sha256 for item in manifest.dataset_refs}
        if expected_dataset_hash not in references:
            raise ArtifactStoreError("existing run has a different dataset snapshot")
        self.verify(final, manifest)
        registration = self.catalog.register_run(manifest) if self.catalog else None
        metrics_path = final / "metrics.json"
        metrics = (
            json.loads(metrics_path.read_text(encoding="utf-8"))
            if metrics_path.is_file()
            else None
        )
        return PublishedRun(final, manifest, metrics, registration, True)

    @staticmethod
    def _references(
        snapshot: DatasetSnapshotManifest,
    ) -> tuple[
        tuple[RunDatasetReference, ...],
        tuple[SchemaVersionReference, ...],
        tuple[str, ...],
    ]:
        snapshot_hash = manifest_sha256(snapshot)
        datasets = (
            RunDatasetReference(
                dataset_id=snapshot.dataset_id,
                dataset_version=snapshot.dataset_version,
                manifest_sha256=snapshot_hash,
            ),
        )
        schemas = tuple(
            SchemaVersionReference(
                dataset_name=item.dataset_name,
                schema_version=item.schema_version,
                schema_fingerprint=item.schema_fingerprint,
            )
            for item in sorted(snapshot.datasets, key=lambda value: value.dataset_name)
        )
        quality = tuple(
            sorted(
                {
                    report
                    for item in snapshot.datasets
                    for report in item.quality_report_ids
                }
            )
        )
        return datasets, schemas, quality

    def _publish_directory(
        self,
        stage: Path,
        manifest: RunManifest,
    ) -> tuple[Path, RegistrationResult | None]:
        (stage / "manifest.json").write_text(
            manifest_json(manifest), encoding="utf-8"
        )
        self.verify(stage, manifest)
        self.root.mkdir(parents=True, exist_ok=True)
        final = self.root / manifest.run_id
        try:
            os.replace(stage, final)
        except OSError as exc:
            raise ArtifactStoreError(f"cannot atomically publish run: {exc}") from exc
        try:
            registration = self.catalog.register_run(manifest) if self.catalog else None
        except Exception as exc:
            try:
                os.replace(final, stage)
            except OSError as rollback_exc:
                raise ArtifactStoreError(
                    "catalog registration failed and run-directory rollback failed: "
                    f"{rollback_exc}"
                ) from exc
            raise ArtifactStoreError(
                f"catalog registration failed; publication rolled back: {exc}"
            ) from exc
        return final, registration

    def publish_success(
        self,
        result: BacktestResult,
        *,
        snapshot: DatasetSnapshotManifest,
        resolved_config: BaseModel,
        resolved_config_payload: Mapping[str, object],
        resolved_config_hash: str,
        factor_versions: tuple[FactorVersionReference, ...],
        environment: EnvironmentInfo,
        base_interval: str,
        output: BacktestOutputConfig,
        factor_values: pl.LazyFrame | None = None,
        universe: pl.LazyFrame | None = None,
        rankings: pl.LazyFrame | None = None,
        rank_selection_diagnostics: pl.LazyFrame | None = None,
    ) -> PublishedRun:
        snapshot_hash = manifest_sha256(snapshot)
        run_id = final_run_id(
            engine_run_id=result.run_id,
            engine_result_hash=result.result_hash,
            resolved_config_hash=resolved_config_hash,
            dataset_manifest_hash=snapshot_hash,
            factor_versions=factor_versions,
            environment=environment,
        )
        existing = self._existing(
            run_id,
            expected_config_hash=resolved_config_hash,
            expected_dataset_hash=snapshot_hash,
        )
        if existing is not None:
            if existing.manifest.status != "succeeded":
                raise ArtifactStoreError("run ID is already bound to a failed run")
            return existing
        created_at = self._terminal_time()
        stage = self._staging_directory(run_id)
        try:
            metrics: RunMetrics = compute_run_metrics(
                result.returns, base_interval=base_interval
            )
            tables = {
                "tables/targets.parquet": (
                    result.targets,
                    () if result.presorted else ("signal_time", "symbol"),
                ),
                "tables/returns.parquet": (
                    result.returns,
                    () if result.presorted else ("timestamp",),
                ),
            }
            if output.save_trades:
                tables["tables/trades.parquet"] = (
                    result.trades,
                    () if result.presorted else ("fill_time", "symbol", "sequence"),
                )
            if output.save_positions:
                tables["tables/positions.parquet"] = (
                    result.positions,
                    () if result.presorted else ("timestamp", "symbol"),
                )
            if output.save_costs:
                tables["tables/costs.parquet"] = (
                    result.costs,
                    () if result.presorted else ("timestamp", "symbol"),
                )
            for relative, (frame, sort_by) in tables.items():
                self._write_table(stage, relative, frame, run_id=run_id, sort_by=sort_by)
            if output.save_factor_values and factor_values is not None:
                self._write_table(
                    stage,
                    "tables/factor_values.parquet",
                    factor_values,
                    run_id=None,
                    sort_by=() if result.presorted else ("timestamp", "symbol"),
                )
            if output.save_universe and universe is not None:
                self._write_table(
                    stage,
                    "tables/universe.parquet",
                    universe,
                    run_id=None,
                    sort_by=() if result.presorted else ("timestamp", "symbol"),
                )
            if rankings is not None:
                self._write_table(
                    stage,
                    "tables/rankings.parquet",
                    rankings,
                    run_id=run_id,
                    sort_by=("timestamp", "factor_name", "ordinal_rank", "symbol"),
                )
            if rank_selection_diagnostics is not None:
                self._write_table(
                    stage,
                    "tables/rank_selection_diagnostics.parquet",
                    rank_selection_diagnostics,
                    run_id=None,
                    sort_by=("timestamp", "side", "requested_rank"),
                )
            (stage / "metrics.json").write_text(metrics.to_json(), encoding="utf-8")
            self._write_json(stage / "warnings.json", list(result.warnings))
            if result.diagnostics is not None:
                self._write_json(stage / "performance.json", result.diagnostics)
            self._write_json(stage / "resolved_config.json", resolved_config_payload)
            (stage / "environment.json").write_text(
                environment.to_json(), encoding="utf-8"
            )
            self._write_json(
                stage / "run_metadata.json",
                {
                    "artifact_code_version": ARTIFACT_CODE_VERSION,
                    "base_interval": base_interval,
                    "dataset_id": snapshot.dataset_id,
                    "dataset_version": snapshot.dataset_version,
                    "engine_result_hash": result.result_hash,
                    "engine_run_id": result.run_id,
                    "factor_names": [
                        item.factor_name for item in factor_versions
                    ],
                    "factor_versions": [
                        item.model_dump(mode="json")
                        for item in factor_versions
                    ],
                    "execution_mode": result.execution_mode,
                    "run_id": run_id,
                },
            )
            if output.render_html:
                render_report_from_artifacts(stage)
            artifacts = self._artifact_hashes(stage)
            datasets, schemas, quality = self._references(snapshot)
            completed_at = self._terminal_time()
            manifest = RunManifest(
                run_id=run_id,
                created_at=created_at,
                completed_at=completed_at,
                status="succeeded",
                error=None,
                git_commit=environment.git_commit,
                python_version=environment.python_version,
                dependency_fingerprint=environment.dependency_fingerprint,
                dataset_refs=datasets,
                schema_versions=schemas,
                quality_report_ids=quality,
                resolved_config_hash=resolved_config_hash,
                factor_versions=factor_versions,
                random_seed=int(
                    getattr(getattr(resolved_config, "backtest"), "run").random_seed
                ),
                artifact_hashes=artifacts,
                warnings_count=len(result.warnings),
            )
            final, registration = self._publish_directory(stage, manifest)
            return PublishedRun(
                final,
                manifest,
                metrics.to_dict(),
                registration,
                False,
            )
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    def publish_failure(
        self,
        *,
        error: Exception,
        snapshot: DatasetSnapshotManifest,
        resolved_config: BaseModel,
        resolved_config_payload: Mapping[str, object],
        resolved_config_hash: str,
        factor_versions: tuple[FactorVersionReference, ...],
        environment: EnvironmentInfo,
    ) -> PublishedRun:
        snapshot_hash = manifest_sha256(snapshot)
        run_id = failed_run_id(
            resolved_config_hash=resolved_config_hash,
            dataset_manifest_hash=snapshot_hash,
            factor_versions=factor_versions,
            environment=environment,
        )
        existing = self._existing(
            run_id,
            expected_config_hash=resolved_config_hash,
            expected_dataset_hash=snapshot_hash,
        )
        if existing is not None:
            return existing
        created_at = self._terminal_time()
        stage = self._staging_directory(run_id)
        try:
            message = f"{type(error).__name__}: {error}"
            self._write_json(stage / "error.json", {"error": message})
            self._write_json(stage / "resolved_config.json", resolved_config_payload)
            (stage / "environment.json").write_text(
                environment.to_json(), encoding="utf-8"
            )
            artifacts = self._artifact_hashes(stage)
            datasets, schemas, quality = self._references(snapshot)
            manifest = RunManifest(
                run_id=run_id,
                created_at=created_at,
                completed_at=self._terminal_time(),
                status="failed",
                error=message,
                git_commit=environment.git_commit,
                python_version=environment.python_version,
                dependency_fingerprint=environment.dependency_fingerprint,
                dataset_refs=datasets,
                schema_versions=schemas,
                quality_report_ids=quality,
                resolved_config_hash=resolved_config_hash,
                factor_versions=factor_versions,
                random_seed=int(
                    getattr(getattr(resolved_config, "backtest"), "run").random_seed
                ),
                artifact_hashes=artifacts,
                warnings_count=0,
            )
            final, registration = self._publish_directory(stage, manifest)
            return PublishedRun(final, manifest, None, registration, False)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise
