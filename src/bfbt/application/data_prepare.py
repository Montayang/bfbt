"""Recorded, resumable DE-v1 data preparation orchestration."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from bfbt.data.catalog import CatalogError, DuckDBCatalog
from bfbt.config.durations import duration_seconds
from bfbt.data.hashing import canonical_json_bytes, content_sha256, sha256_file
from bfbt.data.ingest.raw_store import RawRestStore
from bfbt.data.ingest.service import ArchiveIngestService
from bfbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    PartitionManifest,
    RawObjectManifest,
    load_manifest,
    manifest_json,
    manifest_sha256,
)
from bfbt.data.normalize.core import (
    NORMALIZER_CODE_VERSION,
    NormalizationRelease,
    build_normalization_release,
)
from bfbt.data.normalize.service import NormalizationService
from bfbt.data.preparation import (
    DATASET_ORDER,
    CoverageEvidence,
    DataLineage,
    DataPlan,
    DataPreparationError,
    DataPrepareJob,
    DataReadiness,
    DataWorkspace,
    JobStep,
    LineageEdge,
    PlanActionKind,
    ResearchDataRequirement,
    SnapshotSelector,
    plan_data_preparation,
)
from bfbt.data.sources.base import ArchiveDiscoveryRequest
from bfbt.data.sources.binance_archive import BinanceArchiveSource
from bfbt.data.sources.binance_rest import BinanceRestSource
from bfbt.data.sources.http import PublicHttpClient, RetryPolicy
from bfbt.data.validation.reports import QualityPolicy


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


def _model_bytes(value) -> bytes:
    return canonical_json_bytes(value.model_dump(mode="json")) + b"\n"


def _load_job(path: Path) -> DataPrepareJob:
    return DataPrepareJob.model_validate_json(path.read_bytes())


def _step_index(job: DataPrepareJob, name: str) -> int:
    for index, step in enumerate(job.steps):
        if step.name == name:
            return index
    raise DataPreparationError(f"job is missing step {name}")


def _update_step(
    job: DataPrepareJob,
    name: str,
    *,
    status: str,
    now: datetime,
    outputs: tuple[str, ...] | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
) -> DataPrepareJob:
    index = _step_index(job, name)
    old = job.steps[index]
    updated = old.model_copy(update={
        "status": status,
        "attempts": old.attempts + (1 if status == "running" else 0),
        "outputs": old.outputs if outputs is None else outputs,
        "error_code": error_code,
        "error_detail": error_detail,
    })
    steps = list(job.steps)
    steps[index] = updated
    return job.model_copy(update={"steps": tuple(steps), "updated_at": now})


class DataPreparationService:
    """Execute an approved DataPlan with durable, resumable local state."""

    def __init__(
        self,
        *,
        http_factory: Callable[[], PublicHttpClient] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.http_factory = http_factory or (lambda: PublicHttpClient())
        self.now = now

    def prepare(
        self,
        requirement: ResearchDataRequirement,
        plan: DataPlan,
        *,
        workspace: DataWorkspace,
        jobs_root: Path,
        allow_writes: bool,
        allow_network: bool,
        max_workers: int = 4,
        max_missing_ratio: float = 0.01,
    ) -> DataReadiness:
        if plan.requirement_hash != requirement.requirement_hash:
            raise DataPreparationError("plan does not belong to this requirement")
        if not plan.executable:
            codes = ",".join(
                item.code for item in plan.issues if item.severity.value == "blocker"
            )
            raise DataPreparationError(f"plan is blocked: {codes}")
        if not allow_writes:
            raise DataPreparationError("data prepare requires explicit write approval")
        if plan.requires_network and not allow_network:
            raise DataPreparationError("plan requires explicit network approval")
        if not 1 <= max_workers <= 64:
            raise DataPreparationError("max_workers must be within 1..64")

        job_id = f"de1-{plan.plan_hash[:24]}"
        job_dir = jobs_root / job_id
        job_path = job_dir / "job.json"
        plan_path = job_dir / "plan.json"
        requirement_path = job_dir / "requirement.json"
        current = self.now()
        if job_path.is_file():
            job = _load_job(job_path)
            if job.plan_hash != plan.plan_hash:
                raise DataPreparationError("job ID conflicts with a different plan")
            if job.status == "succeeded" and job.readiness_path:
                return DataReadiness.model_validate_json(
                    (job_dir / job.readiness_path).read_bytes()
                )
        else:
            observed = plan_data_preparation(requirement, workspace=workspace)
            if observed.canonical_bytes() != plan.canonical_bytes():
                raise DataPreparationError(
                    "workspace state changed after planning; review a new DataPlan"
                )
            steps = (
                JobStep(ordinal=1, name="acquire_raw"),
                JobStep(ordinal=2, name="normalize"),
                JobStep(ordinal=3, name="publish_snapshot"),
                JobStep(ordinal=4, name="readiness"),
            )
            job = DataPrepareJob(
                job_id=job_id,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                requirement_hash=requirement.requirement_hash,
                status="pending",
                created_at=current,
                updated_at=current,
                steps=steps,
            )
            _atomic_bytes(plan_path, _model_bytes(plan))
            _atomic_bytes(requirement_path, _model_bytes(requirement))
            _atomic_bytes(job_path, _model_bytes(job))

        try:
            job = job.model_copy(update={"status": "running", "updated_at": self.now()})
            _atomic_bytes(job_path, _model_bytes(job))
            catalog = DuckDBCatalog(workspace.catalog)
            if not workspace.catalog.is_file():
                catalog.initialize()

            job = self._run_step(
                job, job_path, "acquire_raw",
                lambda: self._acquire(
                    requirement, plan, workspace, catalog,
                    allow_network=allow_network, max_workers=max_workers,
                ),
            )
            acquired = job.steps[_step_index(job, "acquire_raw")].outputs
            job = self._run_step(
                job, job_path, "normalize",
                lambda: self._normalize(
                    plan, workspace, catalog, acquired,
                    max_missing_ratio=max_missing_ratio,
                ),
            )
            job = self._run_step(
                job, job_path, "publish_snapshot",
                lambda: self._publish_snapshot(
                    requirement,
                    plan,
                    workspace,
                    catalog,
                    job.steps[_step_index(job, "normalize")].outputs,
                ),
            )
            snapshot_output = job.steps[
                _step_index(job, "publish_snapshot")
            ].outputs
            if len(snapshot_output) != 2:
                raise DataPreparationError("snapshot step did not record identity")
            selector = SnapshotSelector(
                dataset_id=snapshot_output[0], dataset_version=snapshot_output[1]
            )
            readiness_path = job_dir / "readiness.json"
            lineage_path = job_dir / "lineage.json"

            def publish_readiness() -> tuple[str, ...]:
                snapshot = catalog.resolve_dataset(
                    selector.dataset_id, selector.dataset_version
                )
                readiness, lineage = self._readiness(
                    requirement, plan, snapshot, catalog
                )
                _atomic_bytes(readiness_path, _model_bytes(readiness))
                _atomic_bytes(lineage_path, _model_bytes(lineage))
                return (readiness_path.name, lineage_path.name)

            job = self._run_step(
                job, job_path, "readiness", publish_readiness
            )
            readiness = DataReadiness.model_validate_json(readiness_path.read_bytes())
            job = job.model_copy(update={
                "status": "succeeded",
                "updated_at": self.now(),
                "snapshot": selector,
                "readiness_path": readiness_path.name,
                "lineage_path": lineage_path.name,
                "error_code": None,
                "error_detail": None,
            })
            _atomic_bytes(job_path, _model_bytes(job))
            return readiness
        except Exception as exc:
            failed = job.model_copy(update={
                "status": "failed",
                "updated_at": self.now(),
                "error_code": type(exc).__name__,
                "error_detail": str(exc),
            })
            _atomic_bytes(job_path, _model_bytes(failed))
            raise

    def _run_step(
        self,
        job: DataPrepareJob,
        job_path: Path,
        name: str,
        operation: Callable[[], tuple[str, ...]],
    ) -> DataPrepareJob:
        existing = job.steps[_step_index(job, name)]
        if existing.status == "succeeded":
            return job
        job = _update_step(job, name, status="running", now=self.now())
        _atomic_bytes(job_path, _model_bytes(job))
        try:
            outputs = operation()
        except Exception as exc:
            job = _update_step(
                job, name, status="failed", now=self.now(),
                error_code=type(exc).__name__, error_detail=str(exc),
            )
            _atomic_bytes(job_path, _model_bytes(job))
            raise
        job = _update_step(
            job, name, status="succeeded", now=self.now(), outputs=outputs
        )
        _atomic_bytes(job_path, _model_bytes(job))
        return job

    def _acquire(
        self,
        requirement: ResearchDataRequirement,
        plan: DataPlan,
        workspace: DataWorkspace,
        catalog: DuckDBCatalog,
        *,
        allow_network: bool,
        max_workers: int,
    ) -> tuple[str, ...]:
        outputs: list[str] = []
        if plan.reusable_snapshot is not None:
            return ()
        network_actions = {
            item.kind for item in plan.actions if item.action_class.value == "network"
        }
        if network_actions and not allow_network:
            raise DataPreparationError("network action was not approved")
        for dataset in plan.datasets:
            if dataset.selected_dataset_version is not None:
                continue
            if dataset.dataset_name != "contracts":
                for archive in dataset.archive_objects:
                    manifest = workspace.raw_manifests / f"{archive.object_id}.json"
                    if manifest.is_file():
                        outputs.append(str(manifest))
                missing = [
                    item for item in dataset.archive_objects
                    if not (workspace.raw_manifests / f"{item.object_id}.json").is_file()
                ]
                if missing:
                    with self.http_factory() as http:
                        service = ArchiveIngestService(BinanceArchiveSource(http))
                        grouped: dict[tuple[str, str, str | None, str], list] = {}
                        for item in missing:
                            grouped.setdefault((
                                item.dataset_name, item.symbol, item.interval, item.frequency
                            ), []).append(item)
                        for (name, symbol, interval, frequency), items in grouped.items():
                            request = ArchiveDiscoveryRequest(
                                dataset_name=name,
                                symbol=symbol,
                                interval=interval,
                                frequency=frequency,
                                start=min(item.available_from for item in items),
                                end=max(item.available_to for item in items),
                            )
                            results = service.sync(
                                request,
                                raw_root=workspace.raw,
                                manifest_root=workspace.raw_manifests,
                                catalog=catalog,
                                max_workers=max_workers,
                            )
                            outputs.extend(result.manifest_path for result in results)
                expected = {
                    str(workspace.raw_manifests / f"{item.object_id}.json")
                    for item in dataset.archive_objects
                }
                absent = sorted(path for path in expected if not Path(path).is_file())
                if absent:
                    raise DataPreparationError(
                        f"archive discovery did not provide {len(absent)} planned objects"
                    )
                outputs.extend(expected)
            elif any(
                item.kind == PlanActionKind.ACQUIRE_CONTRACTS
                for item in plan.actions
            ):
                with self.http_factory() as http:
                    page = BinanceRestSource(http).exchange_info()
                    result = RawRestStore().publish(
                        page,
                        raw_root=workspace.raw,
                        manifest_root=workspace.raw_manifests,
                        catalog=catalog,
                    )
                    outputs.append(result.manifest_path)
        return tuple(sorted(set(outputs)))

    def _normalize(
        self,
        plan: DataPlan,
        workspace: DataWorkspace,
        catalog: DuckDBCatalog,
        acquired: tuple[str, ...],
        *,
        max_missing_ratio: float,
    ) -> tuple[str, ...]:
        outputs: list[str] = []
        if plan.reusable_snapshot is not None:
            return ()
        manifests = [
            load_manifest(Path(path), "raw") for path in acquired if Path(path).is_file()
        ]
        raw = [item for item in manifests if isinstance(item, RawObjectManifest)]
        service = NormalizationService()
        for dataset in plan.datasets:
            if dataset.selected_dataset_version is not None:
                continue
            selected = [item for item in raw if item.dataset_name == dataset.dataset_name]
            if not selected:
                raise DataPreparationError(
                    f"no Raw manifests are available for {dataset.dataset_name}"
                )
            release = build_normalization_release(dataset.dataset_name, selected)
            groups: dict[tuple[int, int], list[RawObjectManifest]] = {}
            for item in selected:
                timestamp = item.available_from or item.retrieved_at
                groups.setdefault((timestamp.year, timestamp.month), []).append(item)
            for key in sorted(groups):
                paths = tuple(
                    workspace.raw_manifests / f"{item.object_id}.json"
                    for item in sorted(groups[key], key=lambda value: value.object_id)
                )
                result = service.run(
                    dataset.dataset_name,
                    paths,
                    raw_root=workspace.raw,
                    normalized_root=workspace.normalized,
                    partition_manifest_root=workspace.partition_manifests,
                    quality_root=workspace.quality,
                    catalog=catalog,
                    policy=QualityPolicy(max_missing_ratio=max_missing_ratio),
                    release=release,
                    now=self.now,
                )
                outputs.append(str(result.partition_manifest_path))
        return tuple(sorted(set(outputs)))

    def _publish_snapshot(
        self,
        requirement: ResearchDataRequirement,
        plan: DataPlan,
        workspace: DataWorkspace,
        catalog: DuckDBCatalog,
        normalized: tuple[str, ...],
    ) -> tuple[str, ...]:
        if plan.reusable_snapshot is not None:
            catalog.resolve_dataset(
                plan.reusable_snapshot.dataset_id,
                plan.reusable_snapshot.dataset_version,
            )
            return (
                plan.reusable_snapshot.dataset_id,
                plan.reusable_snapshot.dataset_version,
            )
        references: list[DatasetReference] = []
        parameter_hashes: dict[str, str] = {}
        partition_hashes: list[str] = []
        normalized_manifests = [
            load_manifest(Path(path), "partition")
            for path in normalized
            if Path(path).is_file()
        ]
        produced_versions: dict[str, set[str]] = {}
        for item in normalized_manifests:
            if isinstance(item, PartitionManifest):
                produced_versions.setdefault(item.dataset_name, set()).add(
                    item.dataset_version
                )
        for dataset in plan.datasets:
            version = dataset.selected_dataset_version
            if version is None:
                versions = produced_versions.get(dataset.dataset_name, set())
                if len(versions) != 1:
                    raise DataPreparationError(
                        f"expected one produced release for {dataset.dataset_name}; "
                        f"found {len(versions)}"
                    )
                version = next(iter(versions))
            parts = catalog.resolve_partitions(dataset.dataset_name, version)
            if not parts:
                raise DataPreparationError(
                    f"no partitions resolved for {dataset.dataset_name}/{version}"
                )
            minimum = min(item.min_time for item in parts if item.min_time is not None)
            maximum = max(item.max_time for item in parts if item.max_time is not None)
            increment = (
                timedelta(seconds=duration_seconds(dataset.interval or "1m"))
                if dataset.dataset_name in {"bars", "mark_bars"}
                else timedelta(milliseconds=1)
            )
            references.append(DatasetReference(
                dataset_name=dataset.dataset_name,
                dataset_version=version,
                schema_version=parts[0].schema_version,
                schema_fingerprint=parts[0].schema_fingerprint,
                available_from=minimum,
                available_to=maximum + increment,
                partition_manifest_ids=tuple(item.partition_id for item in parts),
                quality_report_ids=tuple(sorted({item.quality_report_id for item in parts})),
            ))
            parameter_hashes[dataset.dataset_name] = content_sha256([
                (item.partition_id, item.content_sha256) for item in parts
            ])
            partition_hashes.extend(manifest_sha256(item) for item in parts)
        references = sorted(references, key=lambda item: DATASET_ORDER.index(item.dataset_name))
        identity = {
            "requirement_hash": requirement.requirement_hash,
            "datasets": [item.model_dump(mode="json") for item in references],
            "derived_intervals": requirement.derived_intervals,
        }
        dataset_version = f"de1-{content_sha256(identity)[:24]}"
        dataset_id = f"bfbt-{requirement.purpose.value}-{requirement.requirement_hash[:12]}"
        path = workspace.snapshot_manifests / f"{dataset_id}-{dataset_version}.json"
        try:
            existing = catalog.resolve_dataset(dataset_id, dataset_version)
            if path.is_file():
                on_disk = load_manifest(path, "dataset")
                if on_disk != existing:
                    raise DataPreparationError(
                        f"snapshot manifest conflicts with Catalog: {path}"
                    )
            else:
                _atomic_bytes(path, manifest_json(existing).encode("utf-8"))
            return existing.dataset_id, existing.dataset_version
        except CatalogError:
            pass
        existing_file = load_manifest(path, "dataset") if path.is_file() else None
        snapshot = DatasetSnapshotManifest(
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            created_at=(
                existing_file.created_at
                if isinstance(existing_file, DatasetSnapshotManifest)
                else self.now()
            ),
            datasets=tuple(references),
            source_manifest_hash=content_sha256(sorted(partition_hashes)),
            normalizer_code_version=NORMALIZER_CODE_VERSION,
            normalizer_parameters_hash=content_sha256(parameter_hashes),
        )
        if path.is_file():
            if existing_file != snapshot:
                raise DataPreparationError(
                    f"snapshot path already contains different content: {path}"
                )
        else:
            _atomic_bytes(path, manifest_json(snapshot).encode("utf-8"))
        catalog.register_dataset(snapshot)
        return snapshot.dataset_id, snapshot.dataset_version

    def _readiness(
        self,
        requirement: ResearchDataRequirement,
        plan: DataPlan,
        snapshot: DatasetSnapshotManifest,
        catalog: DuckDBCatalog,
    ) -> tuple[DataReadiness, DataLineage]:
        evidence: list[CoverageEvidence] = []
        warnings = [
            item.code for item in plan.issues if item.severity.value == "warning"
        ]
        edges: list[LineageEdge] = []
        for member in snapshot.datasets:
            dataset_plan = next(
                item for item in plan.datasets if item.dataset_name == member.dataset_name
            )
            parts = catalog.resolve_partitions(
                member.dataset_name, member.dataset_version
            )
            for part in parts:
                parquet = DataWorkspace(catalog.path.parent).normalized / part.partition_path
                if not parquet.is_file() or sha256_file(parquet) != part.content_sha256:
                    raise DataPreparationError(
                        f"normalized partition integrity failed: {part.partition_id}"
                    )
            detail = "exact normalized partitions satisfy the planned member"
            status: str = "ready"
            if member.dataset_name == "funding":
                status = "warning"
                detail = "funding records are sparse events; absence of a row is not forward-filled"
                warnings.append("FUNDING_EVENT_COVERAGE_IS_SPARSE")
            if member.dataset_name == "contracts" and requirement.allow_current_contract_snapshot:
                status = "warning"
                detail = "current contract snapshot was explicitly accepted and is not historical truth"
                warnings.append("CURRENT_CONTRACT_SNAPSHOT_LIMITATION")
            evidence.append(CoverageEvidence(
                dataset_name=member.dataset_name,
                dataset_version=member.dataset_version,
                required_from=dataset_plan.required_from,
                required_to=dataset_plan.required_to,
                available_from=member.available_from,
                available_to=member.available_to,
                partition_manifest_ids=member.partition_manifest_ids,
                quality_report_ids=member.quality_report_ids,
                status=status,
                detail=detail,
            ))
            for part in parts:
                for source in part.source_object_ids:
                    edges.append(LineageEdge(
                        parent_kind="raw", parent_id=source,
                        child_kind="partition", child_id=part.partition_id,
                    ))
                edges.append(LineageEdge(
                    parent_kind="partition", parent_id=part.partition_id,
                    child_kind="snapshot",
                    child_id=f"{snapshot.dataset_id}/{snapshot.dataset_version}",
                ))
        for interval in requirement.derived_intervals:
            edges.append(LineageEdge(
                parent_kind="snapshot",
                parent_id=f"{snapshot.dataset_id}/{snapshot.dataset_version}",
                child_kind="derived_interval",
                child_id=f"bars:{requirement.base_interval}->{interval}",
            ))
        lineage = DataLineage(
            snapshot_id=snapshot.dataset_id,
            snapshot_version=snapshot.dataset_version,
            edges=tuple(sorted(edges, key=lambda item: (
                item.parent_kind, item.parent_id, item.child_kind, item.child_id
            ))),
        )
        lineage_hash = content_sha256(lineage)
        payload = {
            "requirement_hash": requirement.requirement_hash,
            "plan_id": plan.plan_id,
            "snapshot": [snapshot.dataset_id, snapshot.dataset_version],
            "snapshot_sha256": manifest_sha256(snapshot),
            "coverage": [item.model_dump(mode="json") for item in evidence],
            "lineage_sha256": lineage_hash,
        }
        readiness = DataReadiness(
            readiness_id=f"ready-{content_sha256(payload)[:24]}",
            preparation_job_id=f"de1-{plan.plan_hash[:24]}",
            requirement_hash=requirement.requirement_hash,
            plan_id=plan.plan_id,
            status="ready",
            snapshot=SnapshotSelector(
                dataset_id=snapshot.dataset_id,
                dataset_version=snapshot.dataset_version,
            ),
            snapshot_sha256=manifest_sha256(snapshot),
            lineage_sha256=lineage_hash,
            coverage=tuple(evidence),
            derived_intervals=requirement.derived_intervals,
            purposes=(requirement.purpose,),
            estimated_scan_rows=sum(item.estimated_rows for item in plan.datasets),
            warnings=tuple(sorted(set(warnings))),
        )
        return readiness, lineage


def inspect_job(jobs_root: Path, job_id: str) -> DataPrepareJob:
    path = jobs_root / job_id / "job.json"
    if not path.is_file():
        raise DataPreparationError(f"data preparation job not found: {job_id}")
    return _load_job(path)
