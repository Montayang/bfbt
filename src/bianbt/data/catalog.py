"""Transactional DuckDB catalog for versioned manifests and coverage metadata."""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

import duckdb

from bianbt.data.manifests import (
    DatasetSnapshotManifest,
    ManifestModel,
    PartitionManifest,
    RawObjectManifest,
    RunManifest,
    load_manifest_auto,
    manifest_json,
    manifest_sha256,
)
from bianbt.data.schemas import SchemaDefinition, list_schema_definitions

CATALOG_SCHEMA_VERSION = 1
_TABLES_FOR_INFO = (
    "schema_registry",
    "raw_objects",
    "partitions",
    "dataset_snapshots",
    "runs",
)


class CatalogError(RuntimeError):
    """Base error for catalog operations."""


class CatalogNotInitializedError(CatalogError):
    """The database does not contain a supported catalog schema."""


class CatalogVersionError(CatalogError):
    """The database schema is newer than this code or otherwise unsupported."""


class CatalogConflictError(CatalogError):
    """A business identifier is already bound to different manifest content."""


class CatalogReferenceError(CatalogError):
    """A manifest references metadata that has not been registered or mismatches."""


class CatalogNotFoundError(CatalogError):
    """An exact requested dataset or manifest is absent."""


@dataclass(frozen=True)
class RegistrationResult:
    kind: str
    identifier: str
    manifest_sha256: str
    inserted: bool


@dataclass(frozen=True)
class TableCount:
    table: str
    rows: int


@dataclass(frozen=True)
class CatalogInfo:
    path: Path
    schema_version: int
    counts: tuple[TableCount, ...]


@dataclass(frozen=True)
class CoverageSummary:
    dataset_name: str
    dataset_version: str
    partition_count: int
    row_count: int
    available_from: datetime | None
    available_to: datetime | None
    max_symbols_per_partition: int
    quality_report_ids: tuple[str, ...]


_MIGRATION_1 = (
    """
    CREATE TABLE schema_registry (
        dataset_name VARCHAR NOT NULL,
        schema_version VARCHAR NOT NULL,
        schema_fingerprint VARCHAR NOT NULL,
        descriptor_json JSON NOT NULL,
        PRIMARY KEY (dataset_name, schema_version)
    )
    """,
    """
    CREATE TABLE quality_report_refs (
        quality_report_id VARCHAR PRIMARY KEY
    )
    """,
    """
    CREATE TABLE raw_objects (
        object_id VARCHAR PRIMARY KEY,
        dataset_name VARCHAR NOT NULL,
        source VARCHAR NOT NULL,
        symbol VARCHAR,
        interval VARCHAR,
        available_from TIMESTAMPTZ,
        available_to TIMESTAMPTZ,
        retrieved_at TIMESTAMPTZ NOT NULL,
        byte_size BIGINT NOT NULL CHECK (byte_size > 0),
        checksum_sha256 VARCHAR NOT NULL,
        manifest_sha256 VARCHAR NOT NULL UNIQUE,
        manifest_json JSON NOT NULL
    )
    """,
    """
    CREATE TABLE partitions (
        partition_id VARCHAR PRIMARY KEY,
        dataset_name VARCHAR NOT NULL,
        schema_version VARCHAR NOT NULL,
        schema_fingerprint VARCHAR NOT NULL,
        dataset_version VARCHAR NOT NULL,
        partition_path VARCHAR NOT NULL UNIQUE,
        interval VARCHAR,
        row_count BIGINT NOT NULL CHECK (row_count >= 0),
        min_time TIMESTAMPTZ,
        max_time TIMESTAMPTZ,
        symbols_count BIGINT NOT NULL CHECK (symbols_count >= 0),
        content_sha256 VARCHAR NOT NULL,
        quality_report_id VARCHAR NOT NULL,
        published_at TIMESTAMPTZ NOT NULL,
        manifest_sha256 VARCHAR NOT NULL UNIQUE,
        manifest_json JSON NOT NULL,
        FOREIGN KEY (dataset_name, schema_version)
            REFERENCES schema_registry (dataset_name, schema_version),
        FOREIGN KEY (quality_report_id)
            REFERENCES quality_report_refs (quality_report_id)
    )
    """,
    """
    CREATE TABLE partition_sources (
        partition_id VARCHAR NOT NULL,
        object_id VARCHAR NOT NULL,
        PRIMARY KEY (partition_id, object_id),
        FOREIGN KEY (partition_id) REFERENCES partitions (partition_id),
        FOREIGN KEY (object_id) REFERENCES raw_objects (object_id)
    )
    """,
    """
    CREATE TABLE dataset_snapshots (
        dataset_id VARCHAR NOT NULL,
        dataset_version VARCHAR NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        source_manifest_hash VARCHAR NOT NULL,
        normalizer_code_version VARCHAR NOT NULL,
        normalizer_parameters_hash VARCHAR NOT NULL,
        manifest_sha256 VARCHAR NOT NULL UNIQUE,
        manifest_json JSON NOT NULL,
        PRIMARY KEY (dataset_id, dataset_version)
    )
    """,
    """
    CREATE TABLE dataset_members (
        dataset_id VARCHAR NOT NULL,
        dataset_version VARCHAR NOT NULL,
        dataset_name VARCHAR NOT NULL,
        member_dataset_version VARCHAR NOT NULL,
        schema_version VARCHAR NOT NULL,
        schema_fingerprint VARCHAR NOT NULL,
        available_from TIMESTAMPTZ NOT NULL,
        available_to TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (dataset_id, dataset_version, dataset_name),
        FOREIGN KEY (dataset_id, dataset_version)
            REFERENCES dataset_snapshots (dataset_id, dataset_version),
        FOREIGN KEY (dataset_name, schema_version)
            REFERENCES schema_registry (dataset_name, schema_version)
    )
    """,
    """
    CREATE TABLE dataset_partitions (
        dataset_id VARCHAR NOT NULL,
        dataset_version VARCHAR NOT NULL,
        dataset_name VARCHAR NOT NULL,
        partition_id VARCHAR NOT NULL,
        PRIMARY KEY (dataset_id, dataset_version, partition_id),
        FOREIGN KEY (dataset_id, dataset_version, dataset_name)
            REFERENCES dataset_members (dataset_id, dataset_version, dataset_name),
        FOREIGN KEY (partition_id) REFERENCES partitions (partition_id)
    )
    """,
    """
    CREATE TABLE dataset_quality_refs (
        dataset_id VARCHAR NOT NULL,
        dataset_version VARCHAR NOT NULL,
        dataset_name VARCHAR NOT NULL,
        quality_report_id VARCHAR NOT NULL,
        PRIMARY KEY (
            dataset_id, dataset_version, dataset_name, quality_report_id
        ),
        FOREIGN KEY (dataset_id, dataset_version, dataset_name)
            REFERENCES dataset_members (dataset_id, dataset_version, dataset_name),
        FOREIGN KEY (quality_report_id)
            REFERENCES quality_report_refs (quality_report_id)
    )
    """,
    """
    CREATE TABLE runs (
        run_id VARCHAR PRIMARY KEY,
        created_at TIMESTAMPTZ NOT NULL,
        completed_at TIMESTAMPTZ,
        status VARCHAR NOT NULL,
        git_commit VARCHAR NOT NULL,
        resolved_config_hash VARCHAR NOT NULL,
        manifest_sha256 VARCHAR NOT NULL UNIQUE,
        manifest_json JSON NOT NULL
    )
    """,
    """
    CREATE TABLE run_dataset_refs (
        run_id VARCHAR NOT NULL,
        dataset_id VARCHAR NOT NULL,
        dataset_version VARCHAR NOT NULL,
        manifest_sha256 VARCHAR NOT NULL,
        PRIMARY KEY (run_id, dataset_id),
        FOREIGN KEY (run_id) REFERENCES runs (run_id),
        FOREIGN KEY (dataset_id, dataset_version)
            REFERENCES dataset_snapshots (dataset_id, dataset_version)
    )
    """,
    """
    CREATE TABLE run_schema_refs (
        run_id VARCHAR NOT NULL,
        dataset_name VARCHAR NOT NULL,
        schema_version VARCHAR NOT NULL,
        schema_fingerprint VARCHAR NOT NULL,
        PRIMARY KEY (run_id, dataset_name),
        FOREIGN KEY (run_id) REFERENCES runs (run_id),
        FOREIGN KEY (dataset_name, schema_version)
            REFERENCES schema_registry (dataset_name, schema_version)
    )
    """,
    """
    CREATE TABLE run_quality_refs (
        run_id VARCHAR NOT NULL,
        quality_report_id VARCHAR NOT NULL,
        PRIMARY KEY (run_id, quality_report_id),
        FOREIGN KEY (run_id) REFERENCES runs (run_id),
        FOREIGN KEY (quality_report_id)
            REFERENCES quality_report_refs (quality_report_id)
    )
    """,
    """
    CREATE TABLE run_factor_refs (
        run_id VARCHAR NOT NULL,
        factor_name VARCHAR NOT NULL,
        factor_version VARCHAR NOT NULL,
        PRIMARY KEY (run_id, factor_name),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
    """
    CREATE TABLE run_artifacts (
        run_id VARCHAR NOT NULL,
        artifact_path VARCHAR NOT NULL,
        byte_size BIGINT NOT NULL CHECK (byte_size >= 0),
        sha256 VARCHAR NOT NULL,
        PRIMARY KEY (run_id, artifact_path),
        FOREIGN KEY (run_id) REFERENCES runs (run_id)
    )
    """,
)
_MIGRATIONS = {1: _MIGRATION_1}


@contextmanager
def _transaction(connection: duckdb.DuckDBPyConnection) -> Iterator[None]:
    connection.execute("BEGIN TRANSACTION")
    try:
        yield
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _manifest_payload(manifest: ManifestModel) -> tuple[str, str]:
    return manifest_sha256(manifest), manifest_json(manifest, pretty=False)


def _parse_catalog_version(value: object) -> int:
    try:
        version = int(str(value))
    except (TypeError, ValueError) as exc:
        raise CatalogVersionError(
            f"catalog schema version is not an integer: {value!r}"
        ) from exc
    if version < 0:
        raise CatalogVersionError(
            f"catalog schema version cannot be negative: {version}"
        )
    return version


def _registration_state(
    connection: duckdb.DuckDBPyConnection,
    *,
    table: str,
    where: str,
    parameters: list[object],
    expected_hash: str,
    identifier: str,
) -> bool | None:
    row = connection.execute(
        f"SELECT manifest_sha256 FROM {table} WHERE {where}",
        parameters,
    ).fetchone()
    if row is None:
        return None
    if row[0] != expected_hash:
        raise CatalogConflictError(
            f"{table} identifier {identifier!r} already exists with different content"
        )
    return False


class DuckDBCatalog:
    """Short-lived-connection catalog with transactional manifest registration."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _connect(self, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        if read_only and not self.path.is_file():
            raise CatalogNotInitializedError(f"catalog does not exist: {self.path}")
        connection = duckdb.connect(str(self.path), read_only=read_only)
        connection.execute("SET TimeZone = 'UTC'")
        return connection

    def initialize(self) -> CatalogInfo:
        """Create or migrate the catalog and register built-in Arrow schemas."""

        if self.path.exists() and not self.path.is_file():
            raise CatalogError(f"catalog path is not a file: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_metadata (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM catalog_metadata WHERE key = 'catalog_schema_version'"
            ).fetchone()
            current = _parse_catalog_version(row[0]) if row is not None else 0
            if current > CATALOG_SCHEMA_VERSION:
                raise CatalogVersionError(
                    f"catalog schema version {current} is newer than supported "
                    f"version {CATALOG_SCHEMA_VERSION}"
                )
            for version in range(current + 1, CATALOG_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise CatalogVersionError(f"missing migration for version {version}")
                with _transaction(connection):
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "DELETE FROM catalog_metadata WHERE key = 'catalog_schema_version'"
                    )
                    connection.execute(
                        "INSERT INTO catalog_metadata VALUES (?, ?)",
                        ["catalog_schema_version", str(version)],
                    )
            self._register_builtin_schemas(connection)
        return self.info()

    def _register_builtin_schemas(
        self, connection: duckdb.DuckDBPyConnection
    ) -> None:
        with _transaction(connection):
            for definition in list_schema_definitions():
                self._register_schema(connection, definition)

    @staticmethod
    def _register_schema(
        connection: duckdb.DuckDBPyConnection,
        definition: SchemaDefinition,
    ) -> None:
        descriptor_json = json.dumps(
            definition.descriptor(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        row = connection.execute(
            """
            SELECT schema_fingerprint, descriptor_json::VARCHAR
            FROM schema_registry
            WHERE dataset_name = ? AND schema_version = ?
            """,
            [definition.dataset, definition.version],
        ).fetchone()
        if row is not None:
            stored_descriptor = json.dumps(
                json.loads(row[1]),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if row[0] != definition.fingerprint or stored_descriptor != descriptor_json:
                raise CatalogConflictError(
                    f"schema {definition.dataset}/{definition.version} conflicts "
                    "with the built-in registry"
                )
            return
        connection.execute(
            "INSERT INTO schema_registry VALUES (?, ?, ?, ?)",
            [
                definition.dataset,
                definition.version,
                definition.fingerprint,
                descriptor_json,
            ],
        )

    def _require_version(
        self, connection: duckdb.DuckDBPyConnection
    ) -> int:
        try:
            row = connection.execute(
                "SELECT value FROM catalog_metadata WHERE key = 'catalog_schema_version'"
            ).fetchone()
        except duckdb.Error as exc:
            raise CatalogNotInitializedError(
                f"not a bianbt catalog: {self.path}"
            ) from exc
        if row is None:
            raise CatalogNotInitializedError(f"not a bianbt catalog: {self.path}")
        version = _parse_catalog_version(row[0])
        if version != CATALOG_SCHEMA_VERSION:
            raise CatalogVersionError(
                f"catalog schema version {version} is not supported; "
                f"expected {CATALOG_SCHEMA_VERSION}"
            )
        return version

    def info(self) -> CatalogInfo:
        with self._connect(read_only=True) as connection:
            version = self._require_version(connection)
            counts = tuple(
                TableCount(
                    table=table,
                    rows=int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
                )
                for table in _TABLES_FOR_INFO
            )
        return CatalogInfo(path=self.path, schema_version=version, counts=counts)

    def register_raw(self, manifest: RawObjectManifest) -> RegistrationResult:
        digest, payload = _manifest_payload(manifest)
        with self._connect() as connection:
            self._require_version(connection)
            state = _registration_state(
                connection,
                table="raw_objects",
                where="object_id = ?",
                parameters=[manifest.object_id],
                expected_hash=digest,
                identifier=manifest.object_id,
            )
            if state is False:
                return RegistrationResult("raw", manifest.object_id, digest, False)
            with _transaction(connection):
                connection.execute(
                    """
                    INSERT INTO raw_objects VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        manifest.object_id,
                        manifest.dataset_name,
                        manifest.source,
                        manifest.symbol,
                        manifest.interval,
                        manifest.available_from,
                        manifest.available_to,
                        manifest.retrieved_at,
                        manifest.byte_size,
                        manifest.checksum_sha256,
                        digest,
                        payload,
                    ],
                )
        return RegistrationResult("raw", manifest.object_id, digest, True)

    def _assert_partition_sources(
        self,
        connection: duckdb.DuckDBPyConnection,
        manifest: PartitionManifest,
    ) -> None:
        for object_id in manifest.source_object_ids:
            row = connection.execute(
                "SELECT dataset_name FROM raw_objects WHERE object_id = ?",
                [object_id],
            ).fetchone()
            if row is None:
                raise CatalogReferenceError(
                    f"partition {manifest.partition_id!r} references missing raw "
                    f"object {object_id!r}"
                )
            if row[0] != manifest.dataset_name:
                raise CatalogReferenceError(
                    f"raw object {object_id!r} belongs to {row[0]!r}, not "
                    f"{manifest.dataset_name!r}"
                )

    def register_partition(
        self, manifest: PartitionManifest
    ) -> RegistrationResult:
        digest, payload = _manifest_payload(manifest)
        with self._connect() as connection:
            self._require_version(connection)
            state = _registration_state(
                connection,
                table="partitions",
                where="partition_id = ?",
                parameters=[manifest.partition_id],
                expected_hash=digest,
                identifier=manifest.partition_id,
            )
            if state is False:
                return RegistrationResult(
                    "partition", manifest.partition_id, digest, False
                )
            self._assert_partition_sources(connection, manifest)
            with _transaction(connection):
                connection.execute(
                    "INSERT OR IGNORE INTO quality_report_refs VALUES (?)",
                    [manifest.quality_report_id],
                )
                connection.execute(
                    """
                    INSERT INTO partitions VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        manifest.partition_id,
                        manifest.dataset_name,
                        manifest.schema_version,
                        manifest.schema_fingerprint,
                        manifest.dataset_version,
                        manifest.partition_path,
                        manifest.partition_values.get("interval"),
                        manifest.row_count,
                        manifest.min_time,
                        manifest.max_time,
                        manifest.symbols_count,
                        manifest.content_sha256,
                        manifest.quality_report_id,
                        manifest.published_at,
                        digest,
                        payload,
                    ],
                )
                for object_id in manifest.source_object_ids:
                    connection.execute(
                        "INSERT INTO partition_sources VALUES (?, ?)",
                        [manifest.partition_id, object_id],
                    )
        return RegistrationResult("partition", manifest.partition_id, digest, True)

    def _assert_dataset_references(
        self,
        connection: duckdb.DuckDBPyConnection,
        manifest: DatasetSnapshotManifest,
    ) -> None:
        for member in manifest.datasets:
            quality_report_ids: set[str] = set()
            min_times: list[datetime] = []
            max_times: list[datetime] = []
            for partition_id in member.partition_manifest_ids:
                row = connection.execute(
                    """
                    SELECT dataset_name, dataset_version, schema_version,
                           schema_fingerprint, quality_report_id, min_time, max_time
                    FROM partitions WHERE partition_id = ?
                    """,
                    [partition_id],
                ).fetchone()
                expected = (
                    member.dataset_name,
                    member.dataset_version,
                    member.schema_version,
                    member.schema_fingerprint,
                )
                if row is None:
                    raise CatalogReferenceError(
                        f"dataset snapshot references missing partition {partition_id!r}"
                    )
                if tuple(row[:4]) != expected:
                    raise CatalogReferenceError(
                        f"partition {partition_id!r} metadata does not match its "
                        "dataset reference"
                    )
                quality_report_ids.add(row[4])
                if row[5] is not None:
                    min_times.append(row[5])
                if row[6] is not None:
                    max_times.append(row[6])
            if quality_report_ids != set(member.quality_report_ids):
                raise CatalogReferenceError(
                    f"quality report set for {member.dataset_name!r} does not "
                    "match its referenced partitions"
                )
            if min_times and min(min_times) < member.available_from:
                raise CatalogReferenceError(
                    f"available_from for {member.dataset_name!r} excludes rows"
                )
            if max_times and max(max_times) >= member.available_to:
                raise CatalogReferenceError(
                    f"available_to for {member.dataset_name!r} must be an "
                    "exclusive upper bound after all rows"
                )

    def register_dataset(
        self, manifest: DatasetSnapshotManifest
    ) -> RegistrationResult:
        digest, payload = _manifest_payload(manifest)
        identifier = f"{manifest.dataset_id}/{manifest.dataset_version}"
        with self._connect() as connection:
            self._require_version(connection)
            state = _registration_state(
                connection,
                table="dataset_snapshots",
                where="dataset_id = ? AND dataset_version = ?",
                parameters=[manifest.dataset_id, manifest.dataset_version],
                expected_hash=digest,
                identifier=identifier,
            )
            if state is False:
                return RegistrationResult("dataset", identifier, digest, False)
            self._assert_dataset_references(connection, manifest)
            with _transaction(connection):
                connection.execute(
                    "INSERT INTO dataset_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        manifest.dataset_id,
                        manifest.dataset_version,
                        manifest.created_at,
                        manifest.source_manifest_hash,
                        manifest.normalizer_code_version,
                        manifest.normalizer_parameters_hash,
                        digest,
                        payload,
                    ],
                )
                for member in manifest.datasets:
                    connection.execute(
                        """
                        INSERT INTO dataset_members VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        [
                            manifest.dataset_id,
                            manifest.dataset_version,
                            member.dataset_name,
                            member.dataset_version,
                            member.schema_version,
                            member.schema_fingerprint,
                            member.available_from,
                            member.available_to,
                        ],
                    )
                    for partition_id in member.partition_manifest_ids:
                        connection.execute(
                            "INSERT INTO dataset_partitions VALUES (?, ?, ?, ?)",
                            [
                                manifest.dataset_id,
                                manifest.dataset_version,
                                member.dataset_name,
                                partition_id,
                            ],
                        )
                    for report_id in member.quality_report_ids:
                        connection.execute(
                            "INSERT OR IGNORE INTO quality_report_refs VALUES (?)",
                            [report_id],
                        )
                        connection.execute(
                            "INSERT INTO dataset_quality_refs VALUES (?, ?, ?, ?)",
                            [
                                manifest.dataset_id,
                                manifest.dataset_version,
                                member.dataset_name,
                                report_id,
                            ],
                        )
        return RegistrationResult("dataset", identifier, digest, True)

    def resolve_dataset(
        self, dataset_id: str, dataset_version: str
    ) -> DatasetSnapshotManifest:
        if not dataset_version or dataset_version.lower() == "latest":
            raise CatalogNotFoundError(
                "dataset resolution requires an explicit non-'latest' version"
            )
        with self._connect(read_only=True) as connection:
            self._require_version(connection)
            row = connection.execute(
                """
                SELECT manifest_json::VARCHAR FROM dataset_snapshots
                WHERE dataset_id = ? AND dataset_version = ?
                """,
                [dataset_id, dataset_version],
            ).fetchone()
        if row is None:
            raise CatalogNotFoundError(
                f"dataset snapshot not found: {dataset_id}/{dataset_version}"
            )
        return DatasetSnapshotManifest.model_validate_json(row[0])

    def resolve_partitions(
        self, dataset_name: str, dataset_version: str
    ) -> tuple[PartitionManifest, ...]:
        """Resolve immutable partition manifests for one exact fact-data version."""

        if not dataset_version or dataset_version.lower() == "latest":
            raise CatalogNotFoundError(
                "partition resolution requires an explicit non-'latest' version"
            )
        with self._connect(read_only=True) as connection:
            self._require_version(connection)
            rows = connection.execute(
                """
                SELECT manifest_json::VARCHAR FROM partitions
                WHERE dataset_name = ? AND dataset_version = ?
                ORDER BY partition_path, partition_id
                """,
                [dataset_name, dataset_version],
            ).fetchall()
        if not rows:
            raise CatalogNotFoundError(
                f"no partitions for {dataset_name}/{dataset_version}"
            )
        return tuple(PartitionManifest.model_validate_json(row[0]) for row in rows)

    def coverage(
        self, dataset_name: str, dataset_version: str
    ) -> CoverageSummary:
        if not dataset_version or dataset_version.lower() == "latest":
            raise CatalogNotFoundError(
                "coverage requires an explicit non-'latest' dataset version"
            )
        with self._connect(read_only=True) as connection:
            self._require_version(connection)
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(row_count), 0),
                       MIN(min_time), MAX(max_time),
                       COALESCE(MAX(symbols_count), 0)
                FROM partitions
                WHERE dataset_name = ? AND dataset_version = ?
                """,
                [dataset_name, dataset_version],
            ).fetchone()
            if row is None or int(row[0]) == 0:
                raise CatalogNotFoundError(
                    f"no partitions for {dataset_name}/{dataset_version}"
                )
            reports = connection.execute(
                """
                SELECT DISTINCT quality_report_id FROM partitions
                WHERE dataset_name = ? AND dataset_version = ?
                ORDER BY quality_report_id
                """,
                [dataset_name, dataset_version],
            ).fetchall()
        return CoverageSummary(
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            partition_count=int(row[0]),
            row_count=int(row[1]),
            available_from=row[2],
            available_to=row[3],
            max_symbols_per_partition=int(row[4]),
            quality_report_ids=tuple(item[0] for item in reports),
        )

    def _assert_run_references(
        self,
        connection: duckdb.DuckDBPyConnection,
        manifest: RunManifest,
    ) -> None:
        for reference in manifest.dataset_refs:
            row = connection.execute(
                """
                SELECT manifest_sha256 FROM dataset_snapshots
                WHERE dataset_id = ? AND dataset_version = ?
                """,
                [reference.dataset_id, reference.dataset_version],
            ).fetchone()
            if row is None:
                raise CatalogReferenceError(
                    f"run references missing dataset "
                    f"{reference.dataset_id}/{reference.dataset_version}"
                )
            if row[0] != reference.manifest_sha256:
                raise CatalogReferenceError(
                    f"run dataset hash mismatch for {reference.dataset_id}/"
                    f"{reference.dataset_version}"
                )

    def register_run(self, manifest: RunManifest) -> RegistrationResult:
        digest, payload = _manifest_payload(manifest)
        with self._connect() as connection:
            self._require_version(connection)
            state = _registration_state(
                connection,
                table="runs",
                where="run_id = ?",
                parameters=[manifest.run_id],
                expected_hash=digest,
                identifier=manifest.run_id,
            )
            if state is False:
                return RegistrationResult("run", manifest.run_id, digest, False)
            self._assert_run_references(connection, manifest)
            with _transaction(connection):
                for report_id in manifest.quality_report_ids:
                    connection.execute(
                        "INSERT OR IGNORE INTO quality_report_refs VALUES (?)",
                        [report_id],
                    )
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        manifest.run_id,
                        manifest.created_at,
                        manifest.completed_at,
                        manifest.status,
                        manifest.git_commit,
                        manifest.resolved_config_hash,
                        digest,
                        payload,
                    ],
                )
                for reference in manifest.dataset_refs:
                    connection.execute(
                        "INSERT INTO run_dataset_refs VALUES (?, ?, ?, ?)",
                        [
                            manifest.run_id,
                            reference.dataset_id,
                            reference.dataset_version,
                            reference.manifest_sha256,
                        ],
                    )
                for reference in manifest.schema_versions:
                    connection.execute(
                        "INSERT INTO run_schema_refs VALUES (?, ?, ?, ?)",
                        [
                            manifest.run_id,
                            reference.dataset_name,
                            reference.schema_version,
                            reference.schema_fingerprint,
                        ],
                    )
                for report_id in manifest.quality_report_ids:
                    connection.execute(
                        "INSERT INTO run_quality_refs VALUES (?, ?)",
                        [manifest.run_id, report_id],
                    )
                for reference in manifest.factor_versions:
                    connection.execute(
                        "INSERT INTO run_factor_refs VALUES (?, ?, ?)",
                        [
                            manifest.run_id,
                            reference.factor_name,
                            reference.factor_version,
                        ],
                    )
                for artifact in manifest.artifact_hashes:
                    connection.execute(
                        "INSERT INTO run_artifacts VALUES (?, ?, ?, ?)",
                        [
                            manifest.run_id,
                            artifact.path,
                            artifact.byte_size,
                            artifact.sha256,
                        ],
                    )
        return RegistrationResult("run", manifest.run_id, digest, True)

    def register(self, manifest: ManifestModel) -> RegistrationResult:
        if isinstance(manifest, RawObjectManifest):
            return self.register_raw(manifest)
        if isinstance(manifest, PartitionManifest):
            return self.register_partition(manifest)
        if isinstance(manifest, DatasetSnapshotManifest):
            return self.register_dataset(manifest)
        if isinstance(manifest, RunManifest):
            return self.register_run(manifest)
        raise TypeError(f"unsupported manifest type: {type(manifest)!r}")


_MANIFEST_ORDER = {
    RawObjectManifest: 0,
    PartitionManifest: 1,
    DatasetSnapshotManifest: 2,
    RunManifest: 3,
}


def discover_manifests(root: Path) -> tuple[ManifestModel, ...]:
    """Load every JSON manifest below root and return dependency order."""

    if not root.is_dir():
        raise CatalogError(f"manifest root is not a directory: {root}")
    manifests = [load_manifest_auto(path) for path in sorted(root.rglob("*.json"))]
    return tuple(sorted(manifests, key=lambda item: _MANIFEST_ORDER[type(item)]))


def rebuild_catalog(
    target: Path,
    manifests: Iterable[ManifestModel],
) -> CatalogInfo:
    """Build a fresh catalog and atomically replace target only after success."""

    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        catalog = DuckDBCatalog(temporary)
        catalog.initialize()
        for manifest in manifests:
            catalog.register(manifest)
        os.replace(temporary, target)
    except Exception:
        if temporary.is_file():
            temporary.unlink()
        wal = Path(f"{temporary}.wal")
        if wal.is_file():
            wal.unlink()
        raise
    return DuckDBCatalog(target).info()


def rebuild_catalog_from_directory(target: Path, root: Path) -> CatalogInfo:
    """Discover validated JSON manifests and rebuild a catalog atomically."""

    return rebuild_catalog(target, discover_manifests(root))
