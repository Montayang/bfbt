"""DE-v1 contracts and side-effect-free local data planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from bfbt.compat import StrEnum
from bfbt.config.common import StrictModel, as_utc
from bfbt.config.durations import duration_seconds, is_integer_multiple
from bfbt.data.catalog import (
    CatalogError,
    CatalogNotFoundError,
    CoverageSummary,
    DuckDBCatalog,
)
from bfbt.data.hashing import canonical_json_bytes, content_sha256
from bfbt.data.manifests import DatasetName, DatasetSnapshotManifest, manifest_sha256
from bfbt.data.sources.base import ArchiveDiscoveryRequest
from bfbt.data.sources.binance_archive import (
    ArchiveCoverageStatus,
    archive_candidates,
    local_archive_coverage,
)

REQUIREMENT_VERSION = "research-data-requirement/v1"
PLAN_VERSION = "data-plan/v1"
READINESS_VERSION = "data-readiness/v1"
JOB_VERSION = "data-prepare-job/v1"
LINEAGE_VERSION = "data-lineage/v1"
DATASET_ORDER: tuple[DatasetName, ...] = (
    "bars",
    "mark_bars",
    "funding",
    "contracts",
)


class DataPreparationError(ValueError):
    """A DE-v1 contract or operation cannot be completed safely."""


class DataPurpose(StrEnum):
    QUICK_RESEARCH = "quick_research"
    FAST_MATRIX = "fast_matrix"
    EVENT = "event"


class ActionClass(StrEnum):
    READ_ONLY = "read_only"
    NETWORK = "network"
    DATA_WRITE = "data_write"


class PlanActionKind(StrEnum):
    REUSE_SNAPSHOT = "reuse_snapshot"
    REUSE_PARTITIONS = "reuse_partitions"
    ACQUIRE_ARCHIVE = "acquire_archive"
    ACQUIRE_CONTRACTS = "acquire_contracts"
    NORMALIZE = "normalize"
    DERIVE_INTERVAL = "derive_interval"
    PUBLISH_SNAPSHOT = "publish_snapshot"
    VERIFY_READINESS = "verify_readiness"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class DatasetRequirement(StrictModel):
    dataset_name: DatasetName
    required: bool = True
    interval: str | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> "DatasetRequirement":
        if self.dataset_name in {"bars", "mark_bars"}:
            if self.interval is None:
                raise ValueError("bar datasets require interval")
            duration_seconds(self.interval)
        elif self.interval is not None:
            raise ValueError("funding/contracts must not declare interval")
        return self


class DatasetVersionPin(StrictModel):
    dataset_name: DatasetName
    dataset_version: str = Field(min_length=1)

    @field_validator("dataset_version")
    @classmethod
    def reject_latest(cls, value: str) -> str:
        if value.lower() == "latest":
            raise ValueError("dataset_version cannot be latest")
        return value


class SnapshotSelector(StrictModel):
    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)

    @field_validator("dataset_version")
    @classmethod
    def reject_latest(cls, value: str) -> str:
        if value.lower() == "latest":
            raise ValueError("dataset_version cannot be latest")
        return value


class ResearchDataRequirement(StrictModel):
    requirement_version: Literal["research-data-requirement/v1"] = REQUIREMENT_VERSION
    purpose: DataPurpose
    market: Literal["binance_usd_m_futures"] = "binance_usd_m_futures"
    symbols: tuple[str, ...] = Field(min_length=1)
    core_start: datetime
    core_end: datetime
    base_interval: str = "1m"
    derived_intervals: tuple[str, ...] = ()
    datasets: tuple[DatasetRequirement, ...] = Field(min_length=1)
    factor_warmup_bars: int = Field(default=0, ge=0)
    universe_history_bars: int = Field(default=0, ge=0)
    label_future_bars: int = Field(default=0, ge=0)
    execution_tail_bars: int = Field(default=0, ge=0)
    archive_frequency: Literal["monthly", "daily"] = "monthly"
    dataset_versions: tuple[DatasetVersionPin, ...] = ()
    snapshot: SnapshotSelector | None = None
    allow_current_contract_snapshot: bool = False
    intent_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    unresolved: tuple[str, ...] = ()

    @field_validator("core_start", "core_end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(value.strip().upper() for value in values))
        if any(not value for value in normalized):
            raise ValueError("symbols must be non-empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols must be unique")
        if any(
            not all(character.isalnum() or character == "_" for character in value)
            for value in normalized
        ):
            raise ValueError("symbols contain unsafe characters")
        return normalized

    @field_validator("base_interval")
    @classmethod
    def validate_base_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value

    @field_validator("derived_intervals")
    @classmethod
    def normalize_derived(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted(set(values), key=duration_seconds))
        for value in normalized:
            duration_seconds(value)
        return normalized

    @field_validator("unresolved")
    @classmethod
    def normalize_unresolved(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("unresolved items must be non-empty")
        return tuple(sorted(set(value.strip() for value in values)))

    @model_validator(mode="after")
    def validate_contract(self) -> "ResearchDataRequirement":
        if self.core_end <= self.core_start:
            raise ValueError("core_end must be greater than core_start")
        names = [item.dataset_name for item in self.datasets]
        if len(names) != len(set(names)):
            raise ValueError("datasets must be unique")
        pins = [item.dataset_name for item in self.dataset_versions]
        if len(pins) != len(set(pins)):
            raise ValueError("dataset_versions must be unique")
        if any(item not in names for item in pins):
            raise ValueError("dataset version pins must reference required datasets")
        if not any(item.dataset_name == "bars" and item.required for item in self.datasets):
            raise ValueError("bars are required for every DE-v1 research dataset")
        for value in self.derived_intervals:
            if not is_integer_multiple(value, self.base_interval):
                raise ValueError("derived intervals must be multiples of base_interval")
        return self

    @property
    def history_bars(self) -> int:
        return max(self.factor_warmup_bars, self.universe_history_bars)

    @property
    def future_bars(self) -> int:
        return max(self.label_future_bars, self.execution_tail_bars)

    @property
    def required_start(self) -> datetime:
        return self.core_start - timedelta(
            seconds=self.history_bars * duration_seconds(self.base_interval)
        )

    @property
    def required_end(self) -> datetime:
        return self.core_end + timedelta(
            seconds=self.future_bars * duration_seconds(self.base_interval)
        )

    @property
    def requirement_hash(self) -> str:
        return content_sha256(self.model_dump(mode="json"))


class PlanIssue(StrictModel):
    code: str = Field(min_length=1)
    severity: IssueSeverity
    dataset_name: DatasetName | None = None
    detail: str = Field(min_length=1)


class ArchiveObjectPlan(StrictModel):
    dataset_name: Literal["bars", "mark_bars", "funding"]
    symbol: str
    interval: str | None
    frequency: Literal["monthly", "daily"]
    period: str
    available_from: datetime
    available_to: datetime
    object_id: str
    source_uri: str
    local_status: str


class DatasetPlan(StrictModel):
    dataset_name: DatasetName
    interval: str | None
    required_from: datetime
    required_to: datetime
    selected_dataset_version: str | None = None
    candidate_dataset_versions: tuple[str, ...] = ()
    partition_manifest_ids: tuple[str, ...] = ()
    quality_report_ids: tuple[str, ...] = ()
    archive_objects: tuple[ArchiveObjectPlan, ...] = ()
    estimated_rows: int = Field(ge=0)
    estimated_download_bytes_low: int | None = Field(default=None, ge=0)
    estimated_download_bytes_high: int | None = Field(default=None, ge=0)
    estimated_disk_bytes: int = Field(ge=0)


class PlanAction(StrictModel):
    ordinal: int = Field(ge=1)
    kind: PlanActionKind
    action_class: ActionClass
    dataset_name: DatasetName | None = None
    object_ids: tuple[str, ...] = ()
    detail: str = Field(min_length=1)


class DataPlan(StrictModel):
    plan_version: Literal["data-plan/v1"] = PLAN_VERSION
    plan_id: str = Field(min_length=1)
    requirement_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_start: datetime
    core_start: datetime
    core_end: datetime
    required_end: datetime
    datasets: tuple[DatasetPlan, ...]
    reusable_snapshot: SnapshotSelector | None = None
    reusable_snapshot_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    actions: tuple[PlanAction, ...]
    issues: tuple[PlanIssue, ...]
    estimated_peak_memory_bytes: int = Field(ge=0)

    @property
    def executable(self) -> bool:
        return not any(item.severity == IssueSeverity.BLOCKER for item in self.issues)

    @property
    def requires_network(self) -> bool:
        return any(item.action_class == ActionClass.NETWORK for item in self.actions)

    @property
    def plan_hash(self) -> str:
        return content_sha256(self.model_dump(mode="json"))

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.model_dump(mode="json"))


class CoverageEvidence(StrictModel):
    dataset_name: DatasetName
    dataset_version: str
    required_from: datetime
    required_to: datetime
    available_from: datetime
    available_to: datetime
    partition_manifest_ids: tuple[str, ...]
    quality_report_ids: tuple[str, ...]
    status: Literal["ready", "warning", "blocked"]
    detail: str


class LineageEdge(StrictModel):
    parent_kind: Literal["raw", "partition", "snapshot"]
    parent_id: str
    child_kind: Literal["partition", "snapshot", "derived_interval"]
    child_id: str


class DataLineage(StrictModel):
    lineage_version: Literal["data-lineage/v1"] = LINEAGE_VERSION
    snapshot_id: str
    snapshot_version: str
    edges: tuple[LineageEdge, ...]


class DataReadiness(StrictModel):
    readiness_version: Literal["data-readiness/v1"] = READINESS_VERSION
    readiness_id: str
    preparation_job_id: str
    requirement_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str
    status: Literal["ready", "blocked"]
    snapshot: SnapshotSelector | None = None
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lineage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage: tuple[CoverageEvidence, ...]
    derived_intervals: tuple[str, ...]
    purposes: tuple[DataPurpose, ...]
    estimated_scan_rows: int = Field(ge=0)
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_terminal_state(self) -> "DataReadiness":
        if self.status == "ready":
            if self.snapshot is None or self.snapshot_sha256 is None:
                raise ValueError("ready evidence requires an exact snapshot and hash")
            if not self.coverage:
                raise ValueError("ready evidence requires dataset coverage")
            if self.blockers:
                raise ValueError("ready evidence cannot retain blockers")
        elif self.snapshot is not None or self.snapshot_sha256 is not None:
            raise ValueError("blocked evidence cannot publish a snapshot")
        return self


class JobStep(StrictModel):
    ordinal: int = Field(ge=1)
    name: str
    status: Literal["pending", "running", "succeeded", "failed"] = "pending"
    attempts: int = Field(default=0, ge=0)
    outputs: tuple[str, ...] = ()
    error_code: str | None = None
    error_detail: str | None = None


class DataPrepareJob(StrictModel):
    job_version: Literal["data-prepare-job/v1"] = JOB_VERSION
    job_id: str
    plan_id: str
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    requirement_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["pending", "running", "succeeded", "failed"]
    created_at: datetime
    updated_at: datetime
    steps: tuple[JobStep, ...]
    snapshot: SnapshotSelector | None = None
    readiness_path: str | None = None
    lineage_path: str | None = None
    error_code: str | None = None
    error_detail: str | None = None


@dataclass(frozen=True)
class DataWorkspace:
    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def raw_manifests(self) -> Path:
        return self.root / "raw-manifests"

    @property
    def normalized(self) -> Path:
        return self.root / "normalized"

    @property
    def partition_manifests(self) -> Path:
        return self.root / "partition-manifests"

    @property
    def quality(self) -> Path:
        return self.root / "quality"

    @property
    def snapshot_manifests(self) -> Path:
        return self.root / "snapshot-manifests"

    @property
    def catalog(self) -> Path:
        return self.root / "catalog.duckdb"


def _archive_object_id(dataset: str, symbol: str, interval: str | None, frequency: str, period: str) -> str:
    pieces = ["archive", dataset, symbol]
    if interval is not None:
        pieces.append(interval)
    pieces.extend((frequency, period))
    return "-".join(pieces)


def _required_window(
    requirement: ResearchDataRequirement, dataset: DatasetRequirement
) -> tuple[datetime, datetime]:
    if dataset.dataset_name == "contracts":
        return requirement.core_start, requirement.core_end
    return requirement.required_start, requirement.required_end


def _estimated_rows(
    requirement: ResearchDataRequirement, dataset: DatasetRequirement
) -> int:
    seconds = (requirement.required_end - requirement.required_start).total_seconds()
    if dataset.dataset_name in {"bars", "mark_bars"}:
        assert dataset.interval is not None
        return int(seconds // duration_seconds(dataset.interval)) * len(requirement.symbols)
    if dataset.dataset_name == "funding":
        return max(1, int(seconds // (8 * 3600))) * len(requirement.symbols)
    return len(requirement.symbols)


def _covers(
    summary: CoverageSummary,
    dataset: DatasetRequirement,
    required_from: datetime,
    required_to: datetime,
) -> bool:
    if summary.available_from is None or summary.available_to is None:
        return False
    if dataset.dataset_name in {"bars", "mark_bars"}:
        assert dataset.interval is not None
        available_to = summary.available_to + timedelta(
            seconds=duration_seconds(dataset.interval)
        )
        return summary.available_from <= required_from and available_to >= required_to
    if dataset.dataset_name == "contracts":
        return summary.available_from <= required_from
    return summary.available_from <= required_to and summary.available_to >= required_from


def _catalog_candidates(
    catalog: DuckDBCatalog | None,
    dataset: DatasetRequirement,
    pin: str | None,
    required_from: datetime,
    required_to: datetime,
    symbols: tuple[str, ...],
) -> tuple[CoverageSummary, ...]:
    if catalog is None:
        return ()
    try:
        if pin is not None:
            values = (catalog.coverage(dataset.dataset_name, pin),)
        else:
            values = catalog.list_coverages(
                dataset.dataset_name, interval=dataset.interval
            )
    except (CatalogError, CatalogNotFoundError):
        return ()
    results = []
    for item in values:
        if not _covers(item, dataset, required_from, required_to):
            continue
        if dataset.dataset_name != "contracts":
            available_symbols = set(
                catalog.coverage_symbols(dataset.dataset_name, item.dataset_version)
            )
            if not set(symbols).issubset(available_symbols):
                continue
        results.append(item)
    return tuple(results)


def _validate_snapshot(
    requirement: ResearchDataRequirement,
    snapshot: DatasetSnapshotManifest,
    catalog: DuckDBCatalog,
) -> tuple[PlanIssue, ...]:
    issues: list[PlanIssue] = []
    members = {item.dataset_name: item for item in snapshot.datasets}
    for need in requirement.datasets:
        if not need.required:
            continue
        member = members.get(need.dataset_name)
        if member is None:
            issues.append(PlanIssue(
                code="SNAPSHOT_DATASET_MISSING", severity=IssueSeverity.BLOCKER,
                dataset_name=need.dataset_name,
                detail=f"snapshot lacks required dataset {need.dataset_name}",
            ))
            continue
        summary = catalog.coverage(need.dataset_name, member.dataset_version)
        start, end = _required_window(requirement, need)
        if not _covers(summary, need, start, end):
            issues.append(PlanIssue(
                code="SNAPSHOT_COVERAGE_INSUFFICIENT", severity=IssueSeverity.BLOCKER,
                dataset_name=need.dataset_name,
                detail=f"snapshot member {member.dataset_version} does not cover the required window",
            ))
        if need.dataset_name != "contracts":
            available_symbols = set(
                catalog.coverage_symbols(need.dataset_name, member.dataset_version)
            )
            missing_symbols = sorted(set(requirement.symbols) - available_symbols)
            if missing_symbols:
                issues.append(PlanIssue(
                    code="SNAPSHOT_SYMBOLS_MISSING",
                    severity=IssueSeverity.BLOCKER,
                    dataset_name=need.dataset_name,
                    detail="snapshot lacks required symbols: " + ",".join(missing_symbols),
                ))
        if need.interval is not None:
            partitions = catalog.resolve_partitions(need.dataset_name, member.dataset_version)
            intervals = {item.partition_values.get("interval") for item in partitions}
            if need.interval not in intervals:
                issues.append(PlanIssue(
                    code="SNAPSHOT_INTERVAL_MISSING", severity=IssueSeverity.BLOCKER,
                    dataset_name=need.dataset_name,
                    detail=f"snapshot does not contain interval {need.interval}",
                ))
    return tuple(issues)


def plan_data_preparation(
    requirement: ResearchDataRequirement,
    *,
    workspace: DataWorkspace,
) -> DataPlan:
    """Create a deterministic plan without network access or filesystem writes."""

    catalog: DuckDBCatalog | None = None
    catalog_payload: dict[str, object] = {"initialized": False}
    if workspace.catalog.is_file():
        catalog = DuckDBCatalog(workspace.catalog)
        info = catalog.info()
        catalog_payload = {
            "initialized": True,
            "schema_version": info.schema_version,
            "counts": [(item.table, item.rows) for item in info.counts],
        }
    catalog_state_hash = content_sha256(catalog_payload)
    issues: list[PlanIssue] = [
        PlanIssue(
            code="UNRESOLVED_REQUIREMENT",
            severity=IssueSeverity.BLOCKER,
            detail=value,
        )
        for value in requirement.unresolved
    ]
    actions: list[PlanAction] = []
    plans: list[DatasetPlan] = []
    reusable: SnapshotSelector | None = None
    reusable_hash: str | None = None

    if requirement.snapshot is not None:
        if catalog is None:
            issues.append(PlanIssue(
                code="CATALOG_NOT_INITIALIZED", severity=IssueSeverity.BLOCKER,
                detail="the selected snapshot cannot be resolved without an initialized catalog",
            ))
        else:
            try:
                snapshot = catalog.resolve_dataset(
                    requirement.snapshot.dataset_id,
                    requirement.snapshot.dataset_version,
                )
                issues.extend(_validate_snapshot(requirement, snapshot, catalog))
                if not any(item.severity == IssueSeverity.BLOCKER for item in issues):
                    reusable = requirement.snapshot
                    reusable_hash = manifest_sha256(snapshot)
                    actions.append(PlanAction(
                        ordinal=1, kind=PlanActionKind.REUSE_SNAPSHOT,
                        action_class=ActionClass.READ_ONLY,
                        detail=f"reuse exact snapshot {snapshot.dataset_id}/{snapshot.dataset_version}",
                    ))
            except CatalogError as exc:
                issues.append(PlanIssue(
                    code="SNAPSHOT_NOT_FOUND", severity=IssueSeverity.BLOCKER,
                    detail=str(exc),
                ))

    pins = {item.dataset_name: item.dataset_version for item in requirement.dataset_versions}
    for need in requirement.datasets:
        if not need.required:
            continue
        required_from, required_to = _required_window(requirement, need)
        candidates = _catalog_candidates(
            catalog,
            need,
            pins.get(need.dataset_name),
            required_from,
            required_to,
            requirement.symbols,
        )
        selected: CoverageSummary | None = None
        selection_blocked = False
        if reusable is None:
            if len(candidates) == 1:
                selected = candidates[0]
            elif len(candidates) > 1:
                selection_blocked = True
                issues.append(PlanIssue(
                    code="DATASET_VERSION_SELECTION_REQUIRED",
                    severity=IssueSeverity.BLOCKER,
                    dataset_name=need.dataset_name,
                    detail="multiple compatible versions exist; pin one explicitly",
                ))
            elif need.dataset_name in pins:
                selection_blocked = True
                issues.append(PlanIssue(
                    code="PINNED_DATASET_UNAVAILABLE",
                    severity=IssueSeverity.BLOCKER,
                    dataset_name=need.dataset_name,
                    detail=(
                        f"pinned version {pins[need.dataset_name]} is absent or does not "
                        "cover the required time and symbols"
                    ),
                ))

        archives: list[ArchiveObjectPlan] = []
        if (
            selected is None
            and reusable is None
            and not selection_blocked
            and need.dataset_name != "contracts"
        ):
            for symbol in requirement.symbols:
                actual_request = ArchiveDiscoveryRequest(
                    dataset_name=need.dataset_name,
                    symbol=symbol,
                    interval=need.interval,
                    frequency=requirement.archive_frequency,
                    start=required_from,
                    end=required_to,
                )
                coverage = local_archive_coverage(
                    actual_request,
                    raw_root=workspace.raw,
                    manifest_root=workspace.raw_manifests,
                )
                for item in coverage:
                    remote = item.remote
                    object_id = _archive_object_id(
                        remote.dataset_name, remote.symbol, remote.interval,
                        remote.frequency, remote.period,
                    )
                    archives.append(ArchiveObjectPlan(
                        dataset_name=remote.dataset_name,
                        symbol=remote.symbol,
                        interval=remote.interval,
                        frequency=remote.frequency,
                        period=remote.period,
                        available_from=remote.available_from,
                        available_to=remote.available_to,
                        object_id=object_id,
                        source_uri=remote.url,
                        local_status=item.status.value,
                    ))
                    if item.status in {
                        ArchiveCoverageStatus.CONFLICT,
                        ArchiveCoverageStatus.ORPHAN_MANIFEST,
                    }:
                        issues.append(PlanIssue(
                            code="LOCAL_RAW_CONFLICT", severity=IssueSeverity.BLOCKER,
                            dataset_name=need.dataset_name,
                            detail=f"{object_id} is {item.status.value}",
                        ))
            missing_ids = tuple(
                item.object_id for item in archives
                if item.local_status != ArchiveCoverageStatus.VERIFIED.value
            )
            if missing_ids:
                actions.append(PlanAction(
                    ordinal=len(actions) + 1,
                    kind=PlanActionKind.ACQUIRE_ARCHIVE,
                    action_class=ActionClass.NETWORK,
                    dataset_name=need.dataset_name,
                    object_ids=missing_ids,
                    detail=f"acquire {len(missing_ids)} checksum-backed archive objects",
                ))
            actions.append(PlanAction(
                ordinal=len(actions) + 1,
                kind=PlanActionKind.NORMALIZE,
                action_class=ActionClass.DATA_WRITE,
                dataset_name=need.dataset_name,
                object_ids=tuple(item.object_id for item in archives),
                detail="normalize bounded monthly groups and pass quality gates",
            ))
        elif selected is None and reusable is None and not selection_blocked:
            if not requirement.allow_current_contract_snapshot:
                issues.append(PlanIssue(
                    code="HISTORICAL_CONTRACT_STATE_UNAVAILABLE",
                    severity=IssueSeverity.BLOCKER,
                    dataset_name="contracts",
                    detail="no compatible historical contracts version exists; current exchangeInfo cannot be assumed historical",
                ))
            else:
                actions.append(PlanAction(
                    ordinal=len(actions) + 1,
                    kind=PlanActionKind.ACQUIRE_CONTRACTS,
                    action_class=ActionClass.NETWORK,
                    dataset_name="contracts",
                    detail="capture an explicitly accepted current exchangeInfo snapshot",
                ))
                actions.append(PlanAction(
                    ordinal=len(actions) + 1,
                    kind=PlanActionKind.NORMALIZE,
                    action_class=ActionClass.DATA_WRITE,
                    dataset_name="contracts",
                    detail="normalize and quality-gate the contracts snapshot",
                ))
        elif selected is not None and reusable is None:
            actions.append(PlanAction(
                ordinal=len(actions) + 1,
                kind=PlanActionKind.REUSE_PARTITIONS,
                action_class=ActionClass.READ_ONLY,
                dataset_name=need.dataset_name,
                detail=f"reuse exact fact version {selected.dataset_version}",
            ))

        rows = _estimated_rows(requirement, need)
        missing_count = sum(
            item.local_status != ArchiveCoverageStatus.VERIFIED.value
            for item in archives
        )
        # Public archives vary materially by symbol and market regime.  The
        # estimate is deliberately a range, not false byte-level precision.
        download_low = rows * 12 if missing_count else 0
        download_high = rows * 72 if missing_count else 0
        plans.append(DatasetPlan(
            dataset_name=need.dataset_name,
            interval=need.interval,
            required_from=required_from,
            required_to=required_to,
            selected_dataset_version=selected.dataset_version if selected else None,
            candidate_dataset_versions=tuple(item.dataset_version for item in candidates),
            partition_manifest_ids=(
                tuple(item.partition_id for item in catalog.resolve_partitions(need.dataset_name, selected.dataset_version))
                if selected is not None and catalog is not None else ()
            ),
            quality_report_ids=selected.quality_report_ids if selected else (),
            archive_objects=tuple(sorted(archives, key=lambda item: item.object_id)),
            estimated_rows=rows,
            estimated_download_bytes_low=download_low,
            estimated_download_bytes_high=download_high,
            estimated_disk_bytes=rows * (88 if need.dataset_name in {"bars", "mark_bars"} else 48),
        ))

    if reusable is None:
        for interval in requirement.derived_intervals:
            actions.append(PlanAction(
                ordinal=len(actions) + 1,
                kind=PlanActionKind.DERIVE_INTERVAL,
                action_class=ActionClass.DATA_WRITE,
                dataset_name="bars",
                detail=f"register deterministic virtual derivation {requirement.base_interval}->{interval}",
            ))
        actions.append(PlanAction(
            ordinal=len(actions) + 1,
            kind=PlanActionKind.PUBLISH_SNAPSHOT,
            action_class=ActionClass.DATA_WRITE,
            detail="publish one exact immutable DatasetSnapshot",
        ))
    actions.append(PlanAction(
        ordinal=len(actions) + 1,
        kind=PlanActionKind.VERIFY_READINESS,
        action_class=ActionClass.READ_ONLY,
        detail="produce coverage, quality, identity, resource, and lineage evidence",
    ))

    catalog_state_hash = content_sha256({
        "catalog": catalog_payload,
        "reusable_snapshot_sha256": reusable_hash,
        "datasets": [
            {
                "dataset_name": item.dataset_name,
                "candidates": item.candidate_dataset_versions,
                "partitions": item.partition_manifest_ids,
                "quality": item.quality_report_ids,
            }
            for item in plans
        ],
    })
    plan_payload = {
        "version": PLAN_VERSION,
        "requirement_hash": requirement.requirement_hash,
        "catalog_state_hash": catalog_state_hash,
        "datasets": [item.model_dump(mode="json") for item in plans],
        "snapshot": reusable.model_dump(mode="json") if reusable else None,
        "actions": [item.model_dump(mode="json") for item in actions],
        "issues": [item.model_dump(mode="json") for item in issues],
    }
    plan_id = f"dp-{content_sha256(plan_payload)[:24]}"
    return DataPlan(
        plan_id=plan_id,
        requirement_hash=requirement.requirement_hash,
        catalog_state_hash=catalog_state_hash,
        required_start=requirement.required_start,
        core_start=requirement.core_start,
        core_end=requirement.core_end,
        required_end=requirement.required_end,
        datasets=tuple(sorted(plans, key=lambda item: DATASET_ORDER.index(item.dataset_name))),
        reusable_snapshot=reusable,
        reusable_snapshot_sha256=reusable_hash,
        actions=tuple(actions),
        issues=tuple(sorted(issues, key=lambda item: (item.severity.value, item.code, item.dataset_name or ""))),
        estimated_peak_memory_bytes=min(
            2 * 1024**3,
            max((item.estimated_disk_bytes for item in plans), default=0),
        ),
    )


def load_requirement(path: Path) -> ResearchDataRequirement:
    return ResearchDataRequirement.model_validate_json(path.read_bytes())


def load_plan(path: Path) -> DataPlan:
    return DataPlan.model_validate_json(path.read_bytes())
