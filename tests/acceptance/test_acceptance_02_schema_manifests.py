"""User-run acceptance suite for A02; Codex does not execute it."""

from pathlib import Path

import pyarrow as pa
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bianbt.cli import app
from bianbt.data.hashing import canonical_json_bytes, content_sha256, sha256_file
from bianbt.data.manifests import (
    ArtifactHash,
    DatasetReference,
    DatasetSnapshotManifest,
    FactorVersionReference,
    PartitionManifest,
    RawObjectManifest,
    RunDatasetReference,
    RunManifest,
    SchemaVersionReference,
    load_manifest,
    manifest_json,
    manifest_sha256,
)
from bianbt.data.schemas import (
    UnknownSchemaError,
    get_schema_definition,
    list_schema_definitions,
    validate_arrow_schema,
)

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = BACKTEST_ROOT / "tests" / "fixtures" / "manifests" / "acceptance_02"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def _partition(**updates: object) -> PartitionManifest:
    definition = get_schema_definition("bars", "v1")
    payload: dict[str, object] = {
        "partition_id": "bars-1m-2024-01",
        "dataset_name": "bars",
        "schema_version": "v1",
        "schema_fingerprint": definition.fingerprint,
        "dataset_version": "dataset-v1",
        "partition_path": "bars/schema=v1/interval=1m/year=2024/month=01/part-0.parquet",
        "partition_values": {"interval": "1m", "year": "2024", "month": "01"},
        "row_count": 100,
        "min_time": "2024-01-01T00:00:00Z",
        "max_time": "2024-01-31T23:59:00Z",
        "symbols_count": 2,
        "content_sha256": SHA_A,
        "source_object_ids": ["raw-btc", "raw-eth"],
        "quality_report_id": "quality-bars-2024-01",
        "published_at": "2026-07-29T12:00:00Z",
    }
    payload.update(updates)
    return PartitionManifest.model_validate(payload)


def _dataset_reference() -> DatasetReference:
    definition = get_schema_definition("bars", "v1")
    return DatasetReference(
        dataset_name="bars",
        dataset_version="dataset-v1",
        schema_version="v1",
        schema_fingerprint=definition.fingerprint,
        available_from="2024-01-01T00:00:00Z",
        available_to="2024-02-01T00:00:00Z",
        partition_manifest_ids=("bars-1m-2024-01",),
        quality_report_ids=("quality-bars-2024-01",),
    )


def _run_manifest(**updates: object) -> RunManifest:
    definition = get_schema_definition("bars", "v1")
    payload: dict[str, object] = {
        "run_id": "acceptance-a02",
        "created_at": "2026-07-29T12:00:00Z",
        "completed_at": "2026-07-29T12:01:00Z",
        "status": "succeeded",
        "error": None,
        "git_commit": "1" * 40,
        "python_version": "3.12.3",
        "dependency_fingerprint": SHA_A,
        "dataset_refs": [
            RunDatasetReference(
                dataset_id="usd-m-perpetual",
                dataset_version="dataset-v1",
                manifest_sha256=SHA_B,
            )
        ],
        "schema_versions": [
            SchemaVersionReference(
                dataset_name="bars",
                schema_version="v1",
                schema_fingerprint=definition.fingerprint,
            )
        ],
        "quality_report_ids": ["quality-bars-2024-01"],
        "resolved_config_hash": SHA_C,
        "factor_versions": [
            FactorVersionReference(factor_name="momentum", factor_version="v1")
        ],
        "random_seed": 42,
        "artifact_hashes": [
            ArtifactHash(path="summary.json", byte_size=100, sha256=SHA_D)
        ],
        "warnings_count": 0,
    }
    payload.update(updates)
    return RunManifest.model_validate(payload)


def test_registry_contains_exactly_four_v1_schemas() -> None:
    definitions = list_schema_definitions()
    assert [(item.dataset, item.version) for item in definitions] == [
        ("bars", "v1"),
        ("contracts", "v1"),
        ("funding", "v1"),
        ("mark_bars", "v1"),
    ]
    assert all(len(item.fingerprint) == 64 for item in definitions)


def test_bars_schema_has_expected_key_types_and_metadata() -> None:
    definition = get_schema_definition("bars", "v1")
    schema = definition.schema

    assert definition.primary_key == ("open_time", "symbol", "interval")
    assert definition.sort_key == ("open_time", "symbol")
    assert schema.field("open_time").type == pa.timestamp("ms", tz="UTC")
    assert schema.field("open_time").nullable is False
    assert schema.field("close").type == pa.float64()
    assert schema.field("trades").type == pa.int64()
    assert schema.metadata[b"bianbt.schema_version"] == b"v1"


def test_dataset_specific_columns_and_nullability_are_not_conflated() -> None:
    mark = get_schema_definition("mark_bars", "v1").schema
    funding = get_schema_definition("funding", "v1").schema
    contracts = get_schema_definition("contracts", "v1").schema

    assert "volume" not in mark.names
    assert "quote_volume" not in mark.names
    assert funding.field("funding_rate").nullable is False
    assert funding.field("mark_price").nullable is True
    assert contracts.field("price_tick").nullable is False
    assert contracts.field("observed_last_bar").nullable is True


def test_arrow_schema_round_trip_and_strict_validation() -> None:
    definition = get_schema_definition("bars", "v1")
    restored = pa.ipc.read_schema(pa.BufferReader(definition.schema.serialize()))
    assert restored.equals(definition.schema, check_metadata=True)
    validate_arrow_schema(restored, dataset="bars", version="v1")

    changed = restored.set(
        restored.get_field_index("trades"),
        pa.field("trades", pa.int32(), nullable=False),
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_arrow_schema(changed, dataset="bars", version="v1")


def test_schema_resolution_requires_an_exact_registered_version() -> None:
    with pytest.raises(UnknownSchemaError, match="bars/latest"):
        get_schema_definition("bars", "latest")
    with pytest.raises(UnknownSchemaError, match="unknown/v1"):
        get_schema_definition("unknown", "v1")


def test_byte_hash_and_canonical_json_hash_have_explicit_semantics(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"abc")
    assert sha256_file(sample) == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert content_sha256({"a": 1, "b": 2}) == content_sha256({"b": 2, "a": 1})


def test_raw_manifest_fixture_is_strict_safe_and_deterministic() -> None:
    manifest = load_manifest(FIXTURES / "raw_object.json", "raw")
    assert isinstance(manifest, RawObjectManifest)
    assert manifest.symbol == "BTCUSDT"
    assert manifest.available_from is not None
    assert manifest.available_from.utcoffset().total_seconds() == 0
    assert manifest_sha256(manifest) == manifest_sha256(manifest)

    with pytest.raises(ValidationError):
        load_manifest(FIXTURES / "invalid_raw_object.json", "raw")


def test_partition_manifest_binds_schema_coverage_and_safe_paths() -> None:
    manifest = _partition()
    assert manifest.partition_values["interval"] == "1m"
    with pytest.raises(TypeError, match="immutable"):
        manifest.partition_values["interval"] = "5m"

    with pytest.raises(ValidationError, match="relative POSIX path"):
        _partition(partition_path="/absolute/part.parquet")
    with pytest.raises(ValidationError, match="schema_fingerprint"):
        _partition(schema_fingerprint=SHA_B)
    with pytest.raises(ValidationError, match="cannot be 'latest'"):
        _partition(schema_version="latest")
    with pytest.raises(ValidationError, match="require min_time"):
        _partition(min_time=None)


def test_dataset_snapshot_rejects_latest_and_duplicate_dataset_refs() -> None:
    reference = _dataset_reference()
    snapshot = DatasetSnapshotManifest(
        dataset_id="usd-m-perpetual",
        dataset_version="dataset-v1",
        created_at="2026-07-29T12:00:00Z",
        datasets=(reference,),
        source_manifest_hash=SHA_A,
        normalizer_code_version="normalizer-v1",
        normalizer_parameters_hash=SHA_B,
    )
    assert snapshot.status == "published"

    with pytest.raises(ValidationError, match="cannot be 'latest'"):
        DatasetSnapshotManifest.model_validate(
            {**snapshot.model_dump(), "dataset_version": "latest"}
        )
    with pytest.raises(ValidationError, match="unique by dataset_name"):
        DatasetSnapshotManifest(
            **{**snapshot.model_dump(), "datasets": (reference, reference)}
        )


def test_run_manifest_enforces_terminal_lifecycle_and_artifact_hashes() -> None:
    succeeded = _run_manifest()
    assert succeeded.status == "succeeded"

    with pytest.raises(ValidationError, match="require artifact_hashes"):
        _run_manifest(artifact_hashes=[])
    with pytest.raises(ValidationError, match="unfinished runs"):
        _run_manifest(status="running")

    failed = _run_manifest(
        status="failed",
        error="intentional fixture failure",
        artifact_hashes=[],
    )
    assert failed.completed_at is not None


def test_manifest_serialization_is_stable_and_normalized() -> None:
    first = _run_manifest()
    second = RunManifest.model_validate(first.model_dump(mode="json"))
    assert manifest_json(first, pretty=False) == manifest_json(second, pretty=False)
    assert manifest_sha256(first) == manifest_sha256(second)
    assert manifest_json(first).endswith("\n")


def test_schema_and_manifest_cli_have_success_and_error_exit_codes() -> None:
    runner = CliRunner()
    listed = runner.invoke(app, ["schema", "list"])
    assert listed.exit_code == 0, listed.output
    assert "bars/v1 sha256=" in listed.output
    assert "mark_bars/v1 sha256=" in listed.output

    shown = runner.invoke(app, ["schema", "show", "funding", "v1"])
    assert shown.exit_code == 0, shown.output
    assert "funding_rate" in shown.output

    valid = runner.invoke(
        app,
        ["manifest", "validate", "raw", str(FIXTURES / "raw_object.json")],
    )
    assert valid.exit_code == 0, valid.output
    assert "Manifest is valid (raw-object/v1)." in valid.output
    assert "manifest_sha256=" in valid.output

    invalid = runner.invoke(
        app,
        [
            "manifest",
            "validate",
            "raw",
            str(FIXTURES / "invalid_raw_object.json"),
        ],
    )
    assert invalid.exit_code == 2
    assert "Manifest error" in invalid.output
