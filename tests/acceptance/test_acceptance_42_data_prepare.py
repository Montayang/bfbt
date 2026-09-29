"""A42: offline acceptance for the DE-v1 data preparation loop."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bfbt.application.data_prepare import DataPreparationService, inspect_job
from bfbt.cli import app
from bfbt.data.hashing import sha256_bytes
from bfbt.data.manifests import RawObjectManifest, manifest_json
from bfbt.data.preparation import (
    DataPurpose,
    DataPreparationError,
    DataWorkspace,
    DatasetRequirement,
    DatasetVersionPin,
    ResearchDataRequirement,
    SnapshotSelector,
    plan_data_preparation,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/ingest/acceptance_04/archive_bars.csv"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _requirement(*, contracts: bool = False) -> ResearchDataRequirement:
    datasets = [DatasetRequirement(dataset_name="bars", interval="1m")]
    if contracts:
        datasets.append(DatasetRequirement(dataset_name="contracts"))
    return ResearchDataRequirement(
        purpose=DataPurpose.FAST_MATRIX,
        symbols=("BTCUSDT",),
        core_start=START,
        core_end=START + timedelta(minutes=1),
        base_interval="1m",
        derived_intervals=("5m",),
        datasets=tuple(datasets),
    )


def _seed_archive(workspace: DataWorkspace) -> None:
    relative = (
        "binance/futures/um/monthly/klines/BTCUSDT/1m/"
        "BTCUSDT-1m-2024-01.zip"
    )
    target = workspace.raw / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    info = zipfile.ZipInfo(
        "BTCUSDT-1m-2024-01.csv", date_time=(2024, 1, 1, 0, 0, 0)
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(info, FIXTURE.read_bytes())
    payload = target.read_bytes()
    digest = sha256_bytes(payload)
    manifest = RawObjectManifest(
        object_id="archive-bars-BTCUSDT-1m-monthly-2024-01",
        dataset_name="bars",
        source="binance_public_archive",
        source_uri=(
            "https://data.binance.vision/data/futures/um/monthly/klines/"
            "BTCUSDT/1m/BTCUSDT-1m-2024-01.zip"
        ),
        symbol="BTCUSDT",
        interval="1m",
        available_from=START,
        available_to=datetime(2024, 2, 1, tzinfo=timezone.utc),
        retrieved_at=NOW,
        byte_size=len(payload),
        checksum_sha256=digest,
        upstream_checksum_sha256=digest,
        media_type="application/zip",
        compression="zip",
        http_status=200,
    )
    workspace.raw_manifests.mkdir(parents=True, exist_ok=True)
    (workspace.raw_manifests / f"{manifest.object_id}.json").write_text(
        manifest_json(manifest), encoding="utf-8"
    )


def test_plan_is_deterministic_side_effect_free_and_expands_window(
    tmp_path: Path,
) -> None:
    workspace = DataWorkspace(tmp_path / "absent")
    requirement = _requirement().model_copy(update={
        "factor_warmup_bars": 3,
        "universe_history_bars": 2,
        "label_future_bars": 1,
        "execution_tail_bars": 4,
    })
    first = plan_data_preparation(requirement, workspace=workspace)
    second = plan_data_preparation(requirement, workspace=workspace)
    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.required_start == START - timedelta(minutes=3)
    assert first.required_end == START + timedelta(minutes=5)
    assert first.requires_network is True
    assert not workspace.root.exists()


def test_historical_contract_state_fails_closed(tmp_path: Path) -> None:
    plan = plan_data_preparation(
        _requirement(contracts=True), workspace=DataWorkspace(tmp_path / "data")
    )
    assert plan.executable is False
    assert "HISTORICAL_CONTRACT_STATE_UNAVAILABLE" in {
        item.code for item in plan.issues
    }


def test_missing_explicit_version_blocks_instead_of_substituting(
    tmp_path: Path,
) -> None:
    requirement = _requirement().model_copy(update={
        "dataset_versions": (
            DatasetVersionPin(dataset_name="bars", dataset_version="does-not-exist"),
        )
    })
    plan = plan_data_preparation(
        requirement, workspace=DataWorkspace(tmp_path / "data")
    )
    assert plan.executable is False
    assert "PINNED_DATASET_UNAVAILABLE" in {item.code for item in plan.issues}
    assert not any(item.kind.value == "acquire_archive" for item in plan.actions)


def test_offline_prepare_is_recorded_exact_and_idempotent(tmp_path: Path) -> None:
    workspace = DataWorkspace(tmp_path / "data")
    _seed_archive(workspace)
    requirement = _requirement()
    plan = plan_data_preparation(requirement, workspace=workspace)
    assert plan.executable is True
    assert plan.requires_network is False

    service = DataPreparationService(now=lambda: NOW)
    jobs = tmp_path / "jobs"
    first = service.prepare(
        requirement,
        plan,
        workspace=workspace,
        jobs_root=jobs,
        allow_writes=True,
        allow_network=False,
    )
    second = service.prepare(
        requirement,
        plan,
        workspace=workspace,
        jobs_root=jobs,
        allow_writes=True,
        allow_network=False,
    )
    assert first == second
    assert first.status == "ready"
    assert first.snapshot is not None
    assert first.snapshot.dataset_version.startswith("de1-")
    job = inspect_job(jobs, f"de1-{plan.plan_hash[:24]}")
    assert job.status == "succeeded"
    assert [step.attempts for step in job.steps] == [1, 1, 1, 1]
    assert workspace.catalog.is_file()
    assert len(tuple(workspace.normalized.rglob("*.parquet"))) == 1
    assert (jobs / job.job_id / "readiness.json").is_file()
    assert (jobs / job.job_id / "lineage.json").is_file()

    reuse_requirement = requirement.model_copy(update={
        "snapshot": SnapshotSelector(
            dataset_id=first.snapshot.dataset_id,
            dataset_version=first.snapshot.dataset_version,
        )
    })
    reuse_plan = plan_data_preparation(reuse_requirement, workspace=workspace)
    assert reuse_plan.reusable_snapshot == reuse_requirement.snapshot
    reused = service.prepare(
        reuse_requirement,
        reuse_plan,
        workspace=workspace,
        jobs_root=jobs,
        allow_writes=True,
        allow_network=False,
    )
    assert reused.snapshot == first.snapshot
    reuse_job = inspect_job(jobs, reused.preparation_job_id)
    assert reuse_job.steps[0].outputs == ()
    assert reuse_job.steps[1].outputs == ()


def test_prepare_requires_explicit_write_approval(tmp_path: Path) -> None:
    workspace = DataWorkspace(tmp_path / "data")
    _seed_archive(workspace)
    requirement = _requirement()
    plan = plan_data_preparation(requirement, workspace=workspace)
    with pytest.raises(DataPreparationError, match="write approval"):
        DataPreparationService(now=lambda: NOW).prepare(
            requirement,
            plan,
            workspace=workspace,
            jobs_root=tmp_path / "jobs",
            allow_writes=False,
            allow_network=False,
        )
    assert not (tmp_path / "jobs").exists()


def test_cli_plan_has_json_and_bilingual_human_views(tmp_path: Path) -> None:
    requirement_path = tmp_path / "requirement.json"
    requirement_path.write_text(
        json.dumps(_requirement().model_dump(mode="json")), encoding="utf-8"
    )
    workspace = tmp_path / "never-created"
    runner = CliRunner()
    machine = runner.invoke(app, [
        "data", "plan", str(requirement_path), "--workspace", str(workspace),
        "--format", "json",
    ])
    chinese = runner.invoke(app, [
        "data", "inspect", str(requirement_path), "--workspace", str(workspace),
        "--language", "zh-CN",
    ])
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.output)["plan_version"] == "data-plan/v1"
    assert chinese.exit_code == 0, chinese.output
    assert "数据准备计划" in chinese.output
    assert "需要联网" in chinese.output
    assert not workspace.exists()


def test_public_requirement_example_is_valid() -> None:
    requirement = ResearchDataRequirement.model_validate_json(
        (ROOT / "configs/data_requirement.example.json").read_bytes()
    )
    assert requirement.purpose == DataPurpose.QUICK_RESEARCH
    assert requirement.required_start < requirement.core_start
    assert requirement.required_end > requirement.core_end
