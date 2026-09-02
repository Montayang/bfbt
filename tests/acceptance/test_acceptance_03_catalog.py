"""User-run acceptance suite for A03; Codex does not execute it."""

from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from bfbt.cli import app
from bfbt.data.catalog import (
    CATALOG_SCHEMA_VERSION,
    CatalogConflictError,
    CatalogNotFoundError,
    CatalogReferenceError,
    CatalogVersionError,
    DuckDBCatalog,
    discover_manifests,
    rebuild_catalog,
    rebuild_catalog_from_directory,
)
from bfbt.data.manifests import (
    ArtifactHash,
    DatasetSnapshotManifest,
    FactorVersionReference,
    PartitionManifest,
    RawObjectManifest,
    RunDatasetReference,
    RunManifest,
    SchemaVersionReference,
    load_manifest_auto,
    manifest_sha256,
)
from bfbt.data.schemas import get_schema_definition

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = BACKTEST_ROOT / "tests" / "fixtures" / "catalog" / "acceptance_03" / "manifests"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _raw(month: str) -> RawObjectManifest:
    return load_manifest_auto(MANIFESTS / f"0{month}-raw-2024-0{month}.json")  # type: ignore[return-value]


def _partition(month: str) -> PartitionManifest:
    name = "03-partition-2024-01.json" if month == "1" else "04-partition-2024-02.json"
    return load_manifest_auto(MANIFESTS / name)  # type: ignore[return-value]


def _dataset() -> DatasetSnapshotManifest:
    return load_manifest_auto(MANIFESTS / "05-dataset.json")  # type: ignore[return-value]


def _catalog(tmp_path: Path) -> DuckDBCatalog:
    catalog = DuckDBCatalog(tmp_path / "catalog.duckdb")
    catalog.initialize()
    return catalog


def _register_dataset_chain(catalog: DuckDBCatalog) -> DatasetSnapshotManifest:
    for month in ("1", "2"):
        catalog.register_raw(_raw(month))
        catalog.register_partition(_partition(month))
    dataset = _dataset()
    catalog.register_dataset(dataset)
    return dataset


def _run(dataset: DatasetSnapshotManifest, **updates: object) -> RunManifest:
    schema = get_schema_definition("bars", "v1")
    payload: dict[str, object] = {
        "run_id": "acceptance-a03",
        "created_at": "2026-07-29T12:10:00Z",
        "completed_at": "2026-07-29T12:11:00Z",
        "status": "succeeded",
        "error": None,
        "git_commit": "1" * 40,
        "python_version": "3.12.3",
        "dependency_fingerprint": SHA_A,
        "dataset_refs": [
            RunDatasetReference(
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.dataset_version,
                manifest_sha256=manifest_sha256(dataset),
            )
        ],
        "schema_versions": [
            SchemaVersionReference(
                dataset_name="bars",
                schema_version="v1",
                schema_fingerprint=schema.fingerprint,
            )
        ],
        "quality_report_ids": [
            "quality-bars-2024-01",
            "quality-bars-2024-02",
        ],
        "resolved_config_hash": SHA_B,
        "factor_versions": [
            FactorVersionReference(factor_name="momentum", factor_version="v1")
        ],
        "random_seed": 42,
        "artifact_hashes": [
            ArtifactHash(path="summary.json", byte_size=10, sha256=SHA_C)
        ],
        "warnings_count": 0,
    }
    payload.update(updates)
    return RunManifest.model_validate(payload)


def test_empty_catalog_initialization_is_versioned_and_idempotent(tmp_path: Path) -> None:
    catalog = DuckDBCatalog(tmp_path / "nested" / "catalog.duckdb")
    first = catalog.initialize()
    second = catalog.initialize()

    assert first.schema_version == CATALOG_SCHEMA_VERSION == 1
    assert second == first
    assert dict((item.table, item.rows) for item in second.counts) == {
        "schema_registry": 4,
        "raw_objects": 0,
        "partitions": 0,
        "dataset_snapshots": 0,
        "runs": 0,
    }


def test_newer_catalog_schema_is_rejected_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "future.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("CREATE TABLE catalog_metadata (key VARCHAR, value VARCHAR)")
        connection.execute(
            "INSERT INTO catalog_metadata VALUES ('catalog_schema_version', '999')"
        )

    with pytest.raises(CatalogVersionError, match="newer than supported"):
        DuckDBCatalog(path).initialize()
    with duckdb.connect(str(path), read_only=True) as connection:
        assert connection.execute("SELECT value FROM catalog_metadata").fetchone() == ("999",)


def test_raw_registration_is_idempotent_and_detects_id_conflicts(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    manifest = _raw("1")

    assert catalog.register_raw(manifest).inserted is True
    assert catalog.register_raw(manifest).inserted is False
    changed = manifest.model_copy(update={"byte_size": manifest.byte_size + 1})
    with pytest.raises(CatalogConflictError, match="different content"):
        catalog.register_raw(changed)


def test_partition_registration_checks_references_and_rolls_back(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    partition = _partition("1")

    with pytest.raises(CatalogReferenceError, match="missing raw object"):
        catalog.register_partition(partition)
    with duckdb.connect(str(catalog.path), read_only=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM partitions").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM quality_report_refs").fetchone() == (0,)

    catalog.register_raw(_raw("1"))
    assert catalog.register_partition(partition).inserted is True
    assert catalog.register_partition(partition).inserted is False
    changed = partition.model_copy(update={"row_count": 101})
    with pytest.raises(CatalogConflictError, match="different content"):
        catalog.register_partition(changed)


def test_dataset_registration_requires_complete_exact_partition_metadata(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    dataset = _dataset()
    catalog.register_raw(_raw("1"))
    catalog.register_partition(_partition("1"))

    with pytest.raises(CatalogReferenceError, match="missing partition"):
        catalog.register_dataset(dataset)

    catalog.register_raw(_raw("2"))
    catalog.register_partition(_partition("2"))
    assert catalog.register_dataset(dataset).inserted is True
    assert catalog.register_dataset(dataset).inserted is False
    assert catalog.resolve_dataset("usd-m-perpetual", "snapshot-v1") == dataset
    with pytest.raises(CatalogNotFoundError, match="explicit non-'latest'"):
        catalog.resolve_dataset("usd-m-perpetual", "latest")


def test_coverage_aggregates_registered_partition_metadata(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _register_dataset_chain(catalog)

    summary = catalog.coverage("bars", "bars-dataset-v1")
    assert summary.partition_count == 2
    assert summary.row_count == 300
    assert summary.available_from.isoformat() == "2024-01-01T00:00:00+00:00"
    assert summary.available_to.isoformat() == "2024-02-29T23:59:00+00:00"
    assert summary.max_symbols_per_partition == 1
    assert summary.quality_report_ids == (
        "quality-bars-2024-01",
        "quality-bars-2024-02",
    )


def test_run_registration_binds_exact_dataset_hash(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    dataset = _register_dataset_chain(catalog)
    run = _run(dataset)

    assert catalog.register_run(run).inserted is True
    assert catalog.register_run(run).inserted is False
    changed = run.model_copy(update={"random_seed": 7})
    with pytest.raises(CatalogConflictError, match="different content"):
        catalog.register_run(changed)
    bad_reference = RunDatasetReference(
        dataset_id=dataset.dataset_id,
        dataset_version=dataset.dataset_version,
        manifest_sha256=SHA_D,
    )
    bad_run = _run(dataset, run_id="bad-hash", dataset_refs=[bad_reference])
    with pytest.raises(CatalogReferenceError, match="hash mismatch"):
        catalog.register_run(bad_run)


def test_rebuild_is_dependency_ordered_and_failure_preserves_target(tmp_path: Path) -> None:
    target = tmp_path / "rebuilt.duckdb"
    info = rebuild_catalog_from_directory(target, MANIFESTS)
    assert dict((item.table, item.rows) for item in info.counts)["dataset_snapshots"] == 1
    assert DuckDBCatalog(target).resolve_dataset("usd-m-perpetual", "snapshot-v1") == _dataset()

    with pytest.raises(CatalogReferenceError, match="missing raw object"):
        rebuild_catalog(target, [_partition("1")])
    assert DuckDBCatalog(target).resolve_dataset("usd-m-perpetual", "snapshot-v1") == _dataset()
    assert not tuple(tmp_path.glob(".rebuilt.duckdb.*.tmp"))


def test_manifest_discovery_is_recursive_validated_and_dependency_ordered(tmp_path: Path) -> None:
    discovered = discover_manifests(MANIFESTS)
    assert [item.manifest_version for item in discovered] == [
        "raw-object/v1",
        "raw-object/v1",
        "partition/v1",
        "partition/v1",
        "dataset-snapshot/v1",
    ]

    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    (invalid_root / "unknown.json").write_text(
        '{"manifest_version":"unknown/v1"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown manifest_version"):
        discover_manifests(invalid_root)


def test_catalog_cli_supports_init_register_query_and_rebuild(tmp_path: Path) -> None:
    runner = CliRunner()
    database = tmp_path / "cli.duckdb"
    base = ["--database", str(database)]

    initialized = runner.invoke(app, ["catalog", "init", *base])
    assert initialized.exit_code == 0, initialized.output
    assert "catalog_schema_version=1" in initialized.output

    registrations = (
        ("raw", "01-raw-2024-01.json"),
        ("raw", "02-raw-2024-02.json"),
        ("partition", "03-partition-2024-01.json"),
        ("partition", "04-partition-2024-02.json"),
        ("dataset", "05-dataset.json"),
    )
    for kind, filename in registrations:
        result = runner.invoke(
            app,
            ["catalog", "register", kind, str(MANIFESTS / filename), *base],
        )
        assert result.exit_code == 0, result.output
        assert "registration=inserted" in result.output

    coverage = runner.invoke(
        app, ["catalog", "coverage", "bars", "bars-dataset-v1", *base]
    )
    assert coverage.exit_code == 0, coverage.output
    assert "partition_count=2" in coverage.output
    assert "row_count=300" in coverage.output

    resolved = runner.invoke(
        app, ["catalog", "resolve", "usd-m-perpetual", "snapshot-v1", *base]
    )
    assert resolved.exit_code == 0, resolved.output
    assert '"dataset_id": "usd-m-perpetual"' in resolved.output

    latest = runner.invoke(
        app, ["catalog", "resolve", "usd-m-perpetual", "latest", *base]
    )
    assert latest.exit_code == 2
    assert "Catalog error" in latest.output

    rebuilt = tmp_path / "cli-rebuilt.duckdb"
    rebuild_result = runner.invoke(
        app,
        ["catalog", "rebuild", str(MANIFESTS), "--database", str(rebuilt)],
    )
    assert rebuild_result.exit_code == 0, rebuild_result.output
    assert "Catalog was rebuilt atomically." in rebuild_result.output
