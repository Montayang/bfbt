"""Strict, versioned manifests for raw data, datasets, and runs."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from bianbt.config.common import StrictModel, as_utc
from bianbt.config.durations import duration_seconds
from bianbt.data.hashing import canonical_json_bytes, content_sha256
from bianbt.data.schemas import UnknownSchemaError, get_schema_definition
from bianbt.data.v2_contracts import (
    event_contract_fingerprint as current_event_contract_fingerprint,
)

Sha256: TypeAlias = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DatasetName: TypeAlias = Literal["bars", "mark_bars", "funding", "contracts"]
ArtifactSchemaName: TypeAlias = Literal[
    "rankings",
    "position_instructions",
    "risk_events",
]


class ManifestLoadError(ValueError):
    """A manifest file cannot be decoded or does not match its declared kind."""


class FrozenStringMap(dict[str, str]):
    """String mapping that cannot be changed after model validation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("manifest mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _validate_utc(value: datetime | None) -> datetime | None:
    return as_utc(value)


def _explicit_version(value: str) -> str:
    if not value or value.lower() == "latest":
        raise ValueError("must be an explicit version and cannot be 'latest'")
    return value


def _registered_schema(dataset_name: str, schema_version: str):
    try:
        return get_schema_definition(dataset_name, schema_version)
    except UnknownSchemaError as exc:
        raise ValueError(str(exc)) from exc


def _relative_path(value: str) -> str:
    candidate = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or candidate.is_absolute()
        or ".." in candidate.parts
    ):
        raise ValueError(
            "must be a non-empty relative POSIX path without backslashes or '..'"
        )
    if value in {".", "./"}:
        raise ValueError("must identify a file or partition below its root")
    return candidate.as_posix()


class RawObjectManifest(StrictModel):
    manifest_version: Literal["raw-object/v1"] = "raw-object/v1"
    object_id: str = Field(min_length=1)
    dataset_name: DatasetName
    source: Literal[
        "binance_public_archive",
        "binance_rest",
        "binance_exchange_info",
    ]
    source_uri: str = Field(min_length=1)
    symbol: str | None = None
    interval: str | None = None
    available_from: datetime | None = None
    available_to: datetime | None = None
    retrieved_at: datetime
    byte_size: int = Field(gt=0)
    checksum_sha256: Sha256
    upstream_checksum_sha256: Sha256 | None = None
    media_type: str = Field(min_length=1)
    compression: Literal["zip", "gzip", "none"] = "none"
    http_status: int = Field(ge=100, le=599)
    status: Literal["verified"] = "verified"

    @field_validator("source_uri")
    @classmethod
    def reject_credentials_in_uri(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("must not contain embedded credentials")
        sensitive = {"api_key", "apikey", "signature", "x-mbx-apikey"}
        query_keys = {key.lower() for key, _ in parse_qsl(parsed.query)}
        if sensitive & query_keys:
            raise ValueError("must not contain API keys or signatures")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("must be an absolute HTTPS URI")
        return value

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str | None) -> str | None:
        return value.upper() if value is not None else None

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str | None) -> str | None:
        if value is not None:
            duration_seconds(value)
        return value

    @field_validator("available_from", "available_to", "retrieved_at")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value)

    @model_validator(mode="after")
    def validate_time_range(self) -> "RawObjectManifest":
        if (
            self.available_from is not None
            and self.available_to is not None
            and self.available_to <= self.available_from
        ):
            raise ValueError("available_to must be greater than available_from")
        if self.source == "binance_exchange_info":
            if self.dataset_name != "contracts":
                raise ValueError("exchange info raw objects must target contracts")
            if self.symbol is not None or self.interval is not None:
                raise ValueError("exchange info snapshots cannot declare symbol/interval")
        return self


class PartitionManifest(StrictModel):
    manifest_version: Literal["partition/v1"] = "partition/v1"
    partition_id: str = Field(min_length=1)
    dataset_name: DatasetName
    schema_version: str = Field(min_length=1)
    schema_fingerprint: Sha256
    dataset_version: str = Field(min_length=1)
    partition_path: str
    partition_values: dict[str, str] = Field(default_factory=FrozenStringMap)
    row_count: int = Field(ge=0)
    min_time: datetime | None = None
    max_time: datetime | None = None
    symbols_count: int = Field(ge=0)
    content_sha256: Sha256
    source_object_ids: tuple[str, ...] = Field(min_length=1)
    quality_report_id: str = Field(min_length=1)
    published_at: datetime

    @field_validator("schema_version", "dataset_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _explicit_version(value)

    @field_validator("partition_path")
    @classmethod
    def validate_partition_path(cls, value: str) -> str:
        return _relative_path(value)

    @field_validator("partition_values")
    @classmethod
    def freeze_partition_values(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or not item for key, item in value.items()):
            raise ValueError("partition keys and values must be non-empty")
        return FrozenStringMap(value)

    @field_validator("source_object_ids")
    @classmethod
    def validate_source_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item for item in value):
            raise ValueError("source_object_ids must not contain empty values")
        if len(value) != len(set(value)):
            raise ValueError("source_object_ids must be unique")
        return value

    @field_validator("min_time", "max_time", "published_at")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value)

    @model_validator(mode="after")
    def validate_schema_and_coverage(self) -> "PartitionManifest":
        definition = _registered_schema(self.dataset_name, self.schema_version)
        if self.schema_fingerprint != definition.fingerprint:
            raise ValueError("schema_fingerprint does not match the registry")
        if self.row_count == 0:
            if self.min_time is not None or self.max_time is not None:
                raise ValueError("empty partitions must not declare min_time/max_time")
            if self.symbols_count != 0:
                raise ValueError("empty partitions must have symbols_count=0")
        else:
            if self.min_time is None or self.max_time is None:
                raise ValueError("non-empty partitions require min_time and max_time")
            if self.max_time < self.min_time:
                raise ValueError("max_time must be >= min_time")
            if self.symbols_count < 1:
                raise ValueError("non-empty partitions require symbols_count >= 1")
        return self


class DatasetReference(StrictModel):
    dataset_name: DatasetName
    dataset_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    schema_fingerprint: Sha256
    available_from: datetime
    available_to: datetime
    partition_manifest_ids: tuple[str, ...] = Field(min_length=1)
    quality_report_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("schema_version", "dataset_version")
    @classmethod
    def validate_versions(cls, value: str) -> str:
        return _explicit_version(value)

    @field_validator("available_from", "available_to")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        checked = _validate_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_reference(self) -> "DatasetReference":
        if self.available_to <= self.available_from:
            raise ValueError("available_to must be greater than available_from")
        definition = _registered_schema(self.dataset_name, self.schema_version)
        if self.schema_fingerprint != definition.fingerprint:
            raise ValueError("schema_fingerprint does not match the registry")
        if len(self.partition_manifest_ids) != len(set(self.partition_manifest_ids)):
            raise ValueError("partition_manifest_ids must be unique")
        if len(self.quality_report_ids) != len(set(self.quality_report_ids)):
            raise ValueError("quality_report_ids must be unique")
        return self


class DatasetSnapshotManifest(StrictModel):
    manifest_version: Literal["dataset-snapshot/v1"] = "dataset-snapshot/v1"
    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    created_at: datetime
    status: Literal["published"] = "published"
    datasets: tuple[DatasetReference, ...] = Field(min_length=1)
    source_manifest_hash: Sha256
    normalizer_code_version: str = Field(min_length=1)
    normalizer_parameters_hash: Sha256

    @field_validator("dataset_version")
    @classmethod
    def validate_dataset_version(cls, value: str) -> str:
        return _explicit_version(value)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        checked = _validate_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_unique_datasets(self) -> "DatasetSnapshotManifest":
        names = [item.dataset_name for item in self.datasets]
        if len(names) != len(set(names)):
            raise ValueError("dataset references must be unique by dataset_name")
        return self


class RunDatasetReference(StrictModel):
    dataset_id: str = Field(min_length=1)
    dataset_version: str = Field(min_length=1)
    manifest_sha256: Sha256

    @field_validator("dataset_version")
    @classmethod
    def validate_dataset_version(cls, value: str) -> str:
        return _explicit_version(value)


class SchemaVersionReference(StrictModel):
    dataset_name: DatasetName
    schema_version: str = Field(min_length=1)
    schema_fingerprint: Sha256

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        return _explicit_version(value)

    @model_validator(mode="after")
    def validate_registry_reference(self) -> "SchemaVersionReference":
        definition = _registered_schema(self.dataset_name, self.schema_version)
        if self.schema_fingerprint != definition.fingerprint:
            raise ValueError("schema_fingerprint does not match the registry")
        return self


class ArtifactSchemaVersionReference(StrictModel):
    artifact_name: ArtifactSchemaName
    schema_version: str = Field(min_length=1)
    schema_fingerprint: Sha256

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        return _explicit_version(value)

    @model_validator(mode="after")
    def validate_registry_reference(self) -> "ArtifactSchemaVersionReference":
        definition = _registered_schema(self.artifact_name, self.schema_version)
        if self.schema_fingerprint != definition.fingerprint:
            raise ValueError("schema_fingerprint does not match the registry")
        return self


class FactorVersionReference(StrictModel):
    factor_name: str = Field(min_length=1)
    factor_version: str = Field(min_length=1)


class ArtifactHash(StrictModel):
    path: str
    byte_size: int = Field(ge=0)
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _relative_path(value)


class RunManifest(StrictModel):
    manifest_version: Literal["run/v1"] = "run/v1"
    run_id: str = Field(min_length=1)
    created_at: datetime
    completed_at: datetime | None = None
    status: Literal["pending", "running", "succeeded", "failed"]
    error: str | None = None
    git_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
    python_version: str = Field(min_length=1)
    dependency_fingerprint: Sha256
    dataset_refs: tuple[RunDatasetReference, ...] = Field(min_length=1)
    schema_versions: tuple[SchemaVersionReference, ...] = Field(min_length=1)
    quality_report_ids: tuple[str, ...] = ()
    resolved_config_hash: Sha256
    factor_versions: tuple[FactorVersionReference, ...] = Field(min_length=1)
    random_seed: int
    artifact_hashes: tuple[ArtifactHash, ...] = ()
    warnings_count: int = Field(default=0, ge=0)

    @field_validator("created_at", "completed_at")
    @classmethod
    def validate_times(cls, value: datetime | None) -> datetime | None:
        return _validate_utc(value)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "RunManifest":
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("completed_at must be >= created_at")
        if self.status in {"pending", "running"}:
            if self.completed_at is not None or self.error is not None:
                raise ValueError("unfinished runs cannot have completed_at or error")
        elif self.status == "succeeded":
            if self.completed_at is None:
                raise ValueError("succeeded runs require completed_at")
            if self.error is not None:
                raise ValueError("succeeded runs cannot have error")
            if not self.artifact_hashes:
                raise ValueError("succeeded runs require artifact_hashes")
        elif self.status == "failed":
            if self.completed_at is None or not self.error:
                raise ValueError("failed runs require completed_at and error")

        dataset_ids = [item.dataset_id for item in self.dataset_refs]
        if len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError("dataset_refs must be unique by dataset_id")
        schema_names = [item.dataset_name for item in self.schema_versions]
        if len(schema_names) != len(set(schema_names)):
            raise ValueError("schema_versions must be unique by dataset_name")
        factor_names = [item.factor_name for item in self.factor_versions]
        if len(factor_names) != len(set(factor_names)):
            raise ValueError("factor_versions must be unique by factor_name")
        if len(self.quality_report_ids) != len(set(self.quality_report_ids)):
            raise ValueError("quality_report_ids must be unique")
        artifact_paths = [item.path for item in self.artifact_hashes]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise ValueError("artifact paths must be unique")
        return self


class RunManifestV2(RunManifest):
    manifest_version: Literal["run/v2"] = "run/v2"
    config_version: Literal["v2"] = "v2"
    event_contract_version: Literal["events/v3"] = "events/v3"
    event_contract_fingerprint: Sha256
    artifact_schema_versions: tuple[
        ArtifactSchemaVersionReference, ...
    ]

    @model_validator(mode="after")
    def validate_v2_contracts(self) -> "RunManifestV2":
        if self.event_contract_fingerprint != current_event_contract_fingerprint():
            raise ValueError(
                "event_contract_fingerprint does not match events/v3"
            )
        names = [item.artifact_name for item in self.artifact_schema_versions]
        if len(names) != len(set(names)):
            raise ValueError(
                "artifact_schema_versions must be unique by artifact_name"
            )
        required = {"rankings", "position_instructions", "risk_events"}
        if set(names) != required:
            raise ValueError(
                "artifact_schema_versions must contain rankings, "
                "position_instructions, and risk_events"
            )
        return self


ManifestModel: TypeAlias = (
    RawObjectManifest
    | PartitionManifest
    | DatasetSnapshotManifest
    | RunManifest
    | RunManifestV2
)
MANIFEST_MODELS = {
    "raw": RawObjectManifest,
    "partition": PartitionManifest,
    "dataset": DatasetSnapshotManifest,
    "run": RunManifest,
    "run-v2": RunManifestV2,
}
MANIFEST_VERSION_MODELS = {
    "raw-object/v1": RawObjectManifest,
    "partition/v1": PartitionManifest,
    "dataset-snapshot/v1": DatasetSnapshotManifest,
    "run/v1": RunManifest,
    "run/v2": RunManifestV2,
}


def manifest_json(manifest: ManifestModel, *, pretty: bool = True) -> str:
    """Serialize a validated manifest using stable key ordering."""

    if not pretty:
        return canonical_json_bytes(manifest).decode("utf-8")
    return json.dumps(
        manifest.model_dump(mode="json", exclude_none=False),
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def manifest_sha256(manifest: ManifestModel) -> str:
    """Return the hash of canonical manifest content, not source formatting."""

    return content_sha256(manifest)


def _read_manifest_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestLoadError(f"{path}: cannot load manifest: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestLoadError(f"{path}: top-level JSON value must be an object")
    return raw


def load_manifest(path: Path, kind: str) -> ManifestModel:
    """Read and validate one JSON manifest of an explicitly selected kind."""

    try:
        model = MANIFEST_MODELS[kind]
    except KeyError as exc:
        choices = ", ".join(sorted(MANIFEST_MODELS))
        raise ManifestLoadError(f"unknown manifest kind {kind!r}; choose: {choices}") from exc
    raw = _read_manifest_object(path)
    return model.model_validate(raw)


def load_manifest_auto(path: Path) -> ManifestModel:
    """Read a manifest and select its model from the declared version."""

    raw = _read_manifest_object(path)
    declared_version = raw.get("manifest_version")
    if not isinstance(declared_version, str):
        raise ManifestLoadError(
            f"{path}: manifest_version must be a recognized string"
        )
    try:
        model = MANIFEST_VERSION_MODELS[declared_version]
    except KeyError as exc:
        choices = ", ".join(sorted(MANIFEST_VERSION_MODELS))
        raise ManifestLoadError(
            f"{path}: unknown manifest_version {declared_version!r}; choose: {choices}"
        ) from exc
    return model.model_validate(raw)
