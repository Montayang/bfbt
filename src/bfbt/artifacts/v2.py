"""Atomic publication of A17 V2 audit tables and interactive reports."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import polars as pl
import pyarrow as pa
from pydantic import BaseModel

from bfbt.artifacts.environment import EnvironmentInfo
from bfbt.artifacts.store import (
    ARTIFACT_CODE_VERSION,
    ArtifactStoreError,
    PublishedRun,
    RunArtifactStore,
)
from bfbt.config.backtest import BacktestOutputConfig
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import (
    ArtifactSchemaVersionReference,
    DatasetSnapshotManifest,
    FactorVersionReference,
    RunManifestV2,
    load_manifest_auto,
    manifest_sha256,
)
from bfbt.data.schemas import get_schema_definition, list_artifact_schema_definitions
from bfbt.data.v2_contracts import event_contract_fingerprint
from bfbt.engine.vectorized import BacktestResult
from bfbt.metrics.summary import RunMetrics, compute_run_metrics
from bfbt.reports.renderer import REPORT_VERSION, render_report_from_artifacts

V2_ARTIFACT_CODE_VERSION = "a21-artifacts-v2-streaming"


@dataclass(frozen=True)
class V2AuditArtifacts:
    """Formal V2 audit facts produced before immutable run publication."""

    rankings: pl.DataFrame | pl.LazyFrame
    position_instructions: pl.DataFrame | pl.LazyFrame
    risk_events: pl.DataFrame | pl.LazyFrame
    linked_trades: pl.DataFrame | pl.LazyFrame
    audit_result_hash: str
    arbitration_trace: pl.DataFrame | pl.LazyFrame | None = None


def final_v2_run_id(
    *,
    engine_run_id: str,
    engine_result_hash: str,
    audit_result_hash: str,
    resolved_config_hash: str,
    dataset_manifest_hash: str,
    factor_versions: tuple[FactorVersionReference, ...],
    environment: EnvironmentInfo,
) -> str:
    identity = {
        "artifact_code_version": V2_ARTIFACT_CODE_VERSION,
        "base_artifact_code_version": ARTIFACT_CODE_VERSION,
        "report_version": REPORT_VERSION,
        "engine_run_id": engine_run_id,
        "engine_result_hash": engine_result_hash,
        "audit_result_hash": audit_result_hash,
        "resolved_config_hash": resolved_config_hash,
        "dataset_manifest_hash": dataset_manifest_hash,
        "factor_versions": [
            item.model_dump(mode="json") for item in factor_versions
        ],
        "git_commit": environment.git_commit,
        "source_fingerprint": environment.source_fingerprint,
        "dependency_fingerprint": environment.dependency_fingerprint,
    }
    return f"a17-{content_sha256(identity)[:24]}"


def _polars_dtype(value: pa.DataType) -> pl.DataType:
    if pa.types.is_string(value):
        return pl.String
    if pa.types.is_float64(value):
        return pl.Float64
    if pa.types.is_int16(value):
        return pl.Int16
    if pa.types.is_int32(value):
        return pl.Int32
    if pa.types.is_timestamp(value):
        return pl.Datetime(value.unit, value.tz)
    raise ArtifactStoreError(f"unsupported V2 artifact Arrow type: {value}")


class V2RunArtifactStore(RunArtifactStore):
    """Publish a complete V2 run without changing the V1 store contract."""

    @staticmethod
    def _normalize_artifact(
        value: pl.DataFrame | pl.LazyFrame,
        *,
        dataset: str,
        run_id: str,
    ) -> pl.LazyFrame:
        definition = get_schema_definition(dataset, "v1")
        frame = value.lazy() if isinstance(value, pl.DataFrame) else value
        names = frame.collect_schema().names()
        expected = definition.schema.names
        missing = set(expected) - set(names)
        if missing:
            raise ArtifactStoreError(
                f"{dataset}/v1 is missing columns: {sorted(missing)}"
            )
        expressions = []
        for field in definition.schema:
            expression = pl.col(field.name)
            if field.name == "run_id":
                expression = pl.lit(run_id)
            expressions.append(expression.cast(_polars_dtype(field.type)).alias(field.name))
        normalized = frame.select(expressions)
        required = [field.name for field in definition.schema if not field.nullable]
        if required:
            nulls = normalized.select(
                [pl.col(column).null_count().alias(column) for column in required]
            ).collect(engine="streaming")
            broken = [
                column for column in required if int(nulls.item(0, column)) > 0
            ]
            if broken:
                raise ArtifactStoreError(
                    f"{dataset}/v1 has nulls in required columns: {broken}"
                )
        return normalized

    @staticmethod
    def _validate_primary_key(path: Path, *, dataset: str) -> None:
        definition = get_schema_definition(dataset, "v1")
        keys = list(definition.primary_key)
        ordered = pl.scan_parquet(path, hive_partitioning=False).select(
            keys
        ).sort(keys)
        duplicate = ordered.select(
            pl.all_horizontal(
                *[pl.col(key).eq(pl.col(key).shift(1)) for key in keys]
            ).any().alias("duplicate")
        ).collect(engine="streaming").item()
        if duplicate:
            raise ArtifactStoreError(
                f"{dataset}/v1 violates primary key {definition.primary_key}"
            )

    @staticmethod
    def _artifact_references() -> tuple[
        ArtifactSchemaVersionReference, ...
    ]:
        return tuple(
            ArtifactSchemaVersionReference(
                artifact_name=item.dataset,
                schema_version=item.version,
                schema_fingerprint=item.fingerprint,
            )
            for item in list_artifact_schema_definitions()
        )

    def _existing_v2(
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
        manifest = load_manifest_auto(final / "manifest.json")
        if not isinstance(manifest, RunManifestV2):
            raise ArtifactStoreError("existing A17 run is not a run/v2 manifest")
        if manifest.run_id != run_id:
            raise ArtifactStoreError("existing manifest run_id does not match directory")
        if manifest.resolved_config_hash != expected_config_hash:
            raise ArtifactStoreError("existing run has a different config hash")
        references = {item.manifest_sha256 for item in manifest.dataset_refs}
        if expected_dataset_hash not in references:
            raise ArtifactStoreError("existing run has a different dataset snapshot")
        self.verify(final, manifest)
        registration = self.catalog.register_run(manifest) if self.catalog else None
        metrics = json.loads((final / "metrics.json").read_text(encoding="utf-8"))
        return PublishedRun(final, manifest, metrics, registration, True)

    def publish_success_v2(
        self,
        result: BacktestResult,
        *,
        audit: V2AuditArtifacts,
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
        max_audit_rows: int | None = None,
    ) -> PublishedRun:
        """Publish all core and V2 audit facts through one atomic rename."""

        backtest = getattr(resolved_config, "backtest", None)
        if getattr(backtest, "config_version", None) != "v2":
            raise ArtifactStoreError("publish_success_v2 requires config_version=v2")
        if (
            len(audit.audit_result_hash) != 64
            or any(value not in "0123456789abcdef" for value in audit.audit_result_hash)
        ):
            raise ArtifactStoreError("audit_result_hash must be lowercase SHA-256")
        if max_audit_rows is not None and max_audit_rows < 1:
            raise ArtifactStoreError("max_audit_rows must be positive")
        snapshot_hash = manifest_sha256(snapshot)
        run_id = final_v2_run_id(
            engine_run_id=result.run_id,
            engine_result_hash=result.result_hash,
            audit_result_hash=audit.audit_result_hash,
            resolved_config_hash=resolved_config_hash,
            dataset_manifest_hash=snapshot_hash,
            factor_versions=factor_versions,
            environment=environment,
        )
        existing = self._existing_v2(
            run_id,
            expected_config_hash=resolved_config_hash,
            expected_dataset_hash=snapshot_hash,
        )
        if existing is not None:
            return existing

        normalized = {
            name: self._normalize_artifact(value, dataset=name, run_id=run_id)
            for name, value in {
                "rankings": audit.rankings,
                "position_instructions": audit.position_instructions,
                "risk_events": audit.risk_events,
            }.items()
        }
        row_counts = {
            name: int(
                frame.select(pl.len()).collect(engine="streaming").item()
            )
            for name, frame in normalized.items()
        }
        total_rows = sum(row_counts.values())
        if max_audit_rows is not None and total_rows > max_audit_rows:
            raise ArtifactStoreError(
                f"V2 audit rows {total_rows} exceed max_audit_rows={max_audit_rows}"
            )
        trade_names = set(
            (
                audit.linked_trades.collect_schema()
                if isinstance(audit.linked_trades, pl.LazyFrame)
                else audit.linked_trades.schema
            ).names()
        )
        required_links = {"instruction_id", "source_event_id", "priority"}
        if required_links - trade_names:
            raise ArtifactStoreError(
                "linked trades are missing instruction/risk references"
            )

        created_at = self._terminal_time()
        stage = self._staging_directory(run_id)
        try:
            tables = {
                "tables/targets.parquet": (
                    result.targets,
                    () if result.presorted else ("signal_time", "symbol"),
                ),
                "tables/returns.parquet": (
                    result.returns,
                    () if result.presorted else ("timestamp",),
                ),
                "tables/trades.parquet": (
                    audit.linked_trades,
                    ("fill_time", "symbol", "sequence"),
                ),
            }
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
                lazy_frame = (
                    frame.lazy() if isinstance(frame, pl.DataFrame) else frame
                )
                self._write_table(
                    stage, relative, lazy_frame, run_id=run_id, sort_by=sort_by
                )
            if output.save_factor_values and factor_values is not None:
                self._write_table(
                    stage,
                    "tables/factor_values.parquet",
                    factor_values,
                    run_id=None,
                    sort_by=("timestamp", "symbol"),
                )
            if output.save_universe and universe is not None:
                self._write_table(
                    stage,
                    "tables/universe.parquet",
                    universe,
                    run_id=None,
                    sort_by=("timestamp", "symbol"),
                )
            for name, frame in normalized.items():
                definition = get_schema_definition(name, "v1")
                relative = f"tables/{name}.parquet"
                self._write_table(
                    stage,
                    relative,
                    frame,
                    run_id=run_id,
                    sort_by=definition.sort_key,
                )
                self._validate_primary_key(
                    stage / relative, dataset=name
                )
            if audit.arbitration_trace is not None:
                trace = audit.arbitration_trace
                self._write_table(
                    stage,
                    "tables/arbitration_trace.parquet",
                    trace.lazy() if isinstance(trace, pl.DataFrame) else trace,
                    run_id=None,
                    sort_by=("fill_time", "symbol", "priority", "instruction_id"),
                )

            metrics: RunMetrics = compute_run_metrics(
                pl.scan_parquet(
                    stage / "tables/returns.parquet",
                    hive_partitioning=False,
                ),
                base_interval=base_interval,
            )
            suppressed = normalized["position_instructions"].filter(
                pl.col("reason_code") == "SUPPRESSED_BY_HIGHER_PRIORITY"
            ).select(pl.len()).collect(engine="streaming").item()
            self._write_json(
                stage / "audit_summary.json",
                {
                    "artifact_contract": "v2/a17",
                    "audit_result_hash": audit.audit_result_hash,
                    "row_counts": row_counts,
                    "suppressed_instruction_count": int(suppressed),
                },
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
                    "artifact_code_version": V2_ARTIFACT_CODE_VERSION,
                    "audit_result_hash": audit.audit_result_hash,
                    "base_interval": base_interval,
                    "config_version": "v2",
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
            manifest = RunManifestV2(
                run_id=run_id,
                created_at=created_at,
                completed_at=self._terminal_time(),
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
                random_seed=int(backtest.run.random_seed),
                artifact_hashes=artifacts,
                warnings_count=len(result.warnings),
                event_contract_fingerprint=event_contract_fingerprint(),
                artifact_schema_versions=self._artifact_references(),
            )
            final, registration = self._publish_directory(stage, manifest)
            return PublishedRun(
                final, manifest, metrics.to_dict(), registration, False
            )
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise
