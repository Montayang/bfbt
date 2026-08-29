"""Offline acceptance suite for A09 metrics, artifacts, and reporting."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from bianbt.artifacts.environment import EnvironmentInfo, capture_environment
from bianbt.artifacts.store import ArtifactStoreError, RunArtifactStore
from bianbt.cli import app
from bianbt.config.backtest import BacktestOutputConfig
from bianbt.data.hashing import sha256_file
from bianbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    FactorVersionReference,
    RunManifest,
    load_manifest,
)
from bianbt.data.schemas import get_schema_definition
from bianbt.engine.vectorized import BacktestResult
from bianbt.metrics import MetricsError, compute_run_metrics
from bianbt.reports.renderer import _report_metrics, render_report_from_artifacts

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
SHA_A = "a" * 64
SHA_B = "b" * 64


class _RunConfig(BaseModel):
    random_seed: int = 42


class _BacktestConfig(BaseModel):
    run: _RunConfig = _RunConfig()


class _ResolvedConfig(BaseModel):
    backtest: _BacktestConfig = _BacktestConfig()


def _environment() -> EnvironmentInfo:
    return EnvironmentInfo(
        git_commit="1" * 40,
        source_fingerprint=SHA_A,
        git_dirty=False,
        python_version="3.12.3",
        dependency_fingerprint=SHA_B,
        dependencies=("polars==1.43.1",),
    )


def _snapshot() -> DatasetSnapshotManifest:
    definition = get_schema_definition("bars", "v1")
    return DatasetSnapshotManifest(
        dataset_id="a09-fixture",
        dataset_version="snapshot-v1",
        created_at=START,
        datasets=(
            DatasetReference(
                dataset_name="bars",
                dataset_version="bars-v1",
                schema_version="v1",
                schema_fingerprint=definition.fingerprint,
                available_from=START,
                available_to=START + timedelta(days=1),
                partition_manifest_ids=("bars-partition",),
                quality_report_ids=("quality-bars",),
            ),
        ),
        source_manifest_hash=SHA_A,
        normalizer_code_version="normalizer-v1",
        normalizer_parameters_hash=SHA_B,
    )


def test_environment_fingerprint_covers_standalone_repository_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "bianbt"
    repository.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=repository, check=True)
    subprocess.run(
        ("git", "config", "user.name", "bianbt-test"), cwd=repository, check=True
    )
    subprocess.run(
        ("git", "config", "user.email", "bianbt@example.invalid"),
        cwd=repository,
        check=True,
    )
    source = repository / "source.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(("git", "add", "source.py"), cwd=repository, check=True)
    subprocess.run(
        ("git", "commit", "-q", "-m", "fixture"), cwd=repository, check=True
    )

    clean = capture_environment(repository)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    dirty = capture_environment(repository)

    assert clean.git_dirty is False
    assert dirty.git_dirty is True
    assert dirty.source_fingerprint != clean.source_fingerprint


def _returns() -> pl.LazyFrame:
    values = (0.10, -0.10, 0.05)
    equities = (1.10, 0.99, 1.0395)
    return pl.DataFrame(
        {
            "timestamp": [START + timedelta(minutes=index + 1) for index in range(3)],
            "gross_price_return": values,
            "fee_cost": [0.0] * 3,
            "slippage_cost": [0.0] * 3,
            "funding_return": [0.0] * 3,
            "net_return": values,
            "equity": equities,
            "drawdown": [0.0, -0.10, -0.055],
            "gross_exposure": [1.0, 0.8, 1.2],
            "net_exposure": [0.0, -0.1, 0.2],
            "turnover": [1.0, 0.2, 0.0],
            "run_id": ["engine-a09"] * 3,
        }
    ).lazy()


def _result() -> BacktestResult:
    signal = START
    targets = pl.DataFrame(
        {
            "signal_time": [signal],
            "symbol": ["BTCUSDT"],
            "score": [1.0],
            "side": ["LONG"],
            "unconstrained_weight": [1.0],
            "target_weight": [1.0],
            "constraint_flags": [""],
            "portfolio_version": ["portfolio-v1"],
            "run_id": ["engine-a09"],
        }
    ).lazy()
    trades = pl.DataFrame(
        {
            "signal_time": [signal],
            "fill_time": [signal + timedelta(minutes=1)],
            "symbol": ["BTCUSDT"],
            "sequence": [1],
            "side": ["BUY"],
            "old_weight": [0.0],
            "target_weight": [1.0],
            "filled_weight": [1.0],
            "turnover": [1.0],
            "reference_price": [100.0],
            "fill_price": [100.0],
            "notional": [1.0],
            "status": ["FILLED"],
            "constraint_flags": [""],
            "run_id": ["engine-a09"],
        }
    ).lazy()
    positions = pl.DataFrame(
        {
            "timestamp": [signal + timedelta(minutes=1)],
            "symbol": ["BTCUSDT"],
            "quantity": [0.01],
            "signed_notional": [1.0],
            "target_weight": [1.0],
            "actual_weight": [1.0],
            "mark_price": [100.0],
            "unrealized_pnl": [0.0],
            "run_id": ["engine-a09"],
        }
    ).lazy()
    costs = pl.DataFrame(
        {
            "timestamp": [signal + timedelta(minutes=1)],
            "symbol": ["BTCUSDT"],
            "fee_cost": [0.0],
            "slippage_cost": [0.0],
            "funding_cashflow": [0.0],
            "total_cost": [0.0],
            "run_id": ["engine-a09"],
        }
    ).lazy()
    return BacktestResult(
        run_id="engine-a09",
        result_hash="result-a09",
        targets=targets,
        trades=trades,
        positions=positions,
        costs=costs,
        returns=_returns(),
        warnings=("fixture-warning",),
    )


def _publish(tmp_path: Path, **updates: object):
    store = RunArtifactStore(
        tmp_path / "runs",
        now=lambda: START + timedelta(days=2),
    )
    arguments = {
        "snapshot": _snapshot(),
        "resolved_config": _ResolvedConfig(),
        "resolved_config_payload": {"fixture": "a09"},
        "resolved_config_hash": SHA_A,
        "factor_versions": (
            FactorVersionReference(
                factor_name="momentum", factor_version="factor-v1"
            ),
        ),
        "environment": _environment(),
        "base_interval": "1m",
        "output": BacktestOutputConfig(root=tmp_path / "runs"),
    }
    arguments.update(updates)
    return store, store.publish_success(_result(), **arguments)


def test_metrics_compound_equity_drawdown_and_hit_rate() -> None:
    metrics = compute_run_metrics(_returns(), base_interval="1m")
    performance = metrics.performance
    assert performance.initial_equity == pytest.approx(1.0)
    assert performance.ending_equity == pytest.approx(1.0395)
    assert performance.total_return == pytest.approx(0.0395)
    assert performance.max_drawdown == pytest.approx(-0.10)
    assert performance.hit_rate == pytest.approx(2 / 3)
    assert performance.annualized_return is None


def test_risk_and_attribution_are_additive_and_explicit() -> None:
    metrics = compute_run_metrics(_returns(), base_interval="1m")
    assert metrics.risk.average_gross_exposure == pytest.approx(1.0)
    assert metrics.risk.maximum_absolute_net_exposure == pytest.approx(0.2)
    assert metrics.risk.total_turnover == pytest.approx(1.2)
    assert metrics.attribution.net_contribution == pytest.approx(0.05)
    assert metrics.attribution.maximum_identity_error == pytest.approx(0.0)


def test_report_actual_costs_and_total_pnl_use_artifact_currency_amounts() -> None:
    enriched = _report_metrics(
        {
            "performance": {
                "initial_equity": 10_000.0,
                "ending_equity": 8_750.0,
            },
            "attribution": {},
        },
        {
            "backtest": {
                "execution": {
                    "fee": {"model": "fixed_bps", "taker_bps": 4.0},
                    "slippage": {"model": "fixed_bps", "bps": 1.0},
                }
            }
        },
        {"total_notional": 1_250_000.0},
    )
    attribution = enriched["attribution"]
    assert attribution["fee_cost_amount"] == pytest.approx(500.0)
    assert attribution["slippage_cost_amount"] == pytest.approx(125.0)
    assert attribution["net_profit_loss_amount"] == pytest.approx(-1_250.0)


def test_metrics_reject_broken_equity_or_return_identity() -> None:
    broken_equity = _returns().with_columns(
        pl.when(pl.col("timestamp") == START + timedelta(minutes=2))
        .then(2.0)
        .otherwise(pl.col("equity"))
        .alias("equity")
    )
    with pytest.raises(MetricsError, match="does not compound"):
        compute_run_metrics(broken_equity, base_interval="1m")
    broken_identity = _returns().with_columns(
        (pl.col("net_return") + 0.01).alias("net_return")
    )
    with pytest.raises(MetricsError, match="identity"):
        compute_run_metrics(broken_identity, base_interval="1m")


def test_success_publish_is_terminal_hashed_and_complete(tmp_path: Path) -> None:
    _, published = _publish(tmp_path)
    assert published.manifest.status == "succeeded"
    assert published.manifest.warnings_count == 1
    assert published.path.name == published.manifest.run_id
    expected = {
        "environment.json",
        "metrics.json",
        "report.html",
        "resolved_config.json",
        "run_metadata.json",
        "tables/costs.parquet",
        "tables/positions.parquet",
        "tables/returns.parquet",
        "tables/targets.parquet",
        "tables/trades.parquet",
        "warnings.json",
    }
    assert {item.path for item in published.manifest.artifact_hashes} == expected
    loaded = load_manifest(published.path / "manifest.json", "run")
    assert isinstance(loaded, RunManifest)
    assert loaded == published.manifest
    RunArtifactStore.verify(published.path, loaded)
    metadata = (published.path / "run_metadata.json").read_text(
        encoding="utf-8"
    )
    assert '"factor_names": [' in metadata
    assert '"factor_name": "momentum"' in metadata
    report = (published.path / "report.html").read_text(encoding="utf-8")
    for expected_text in (
        "数据与运行概览 / Data & Run Overview",
        "因子公式 / Factor Formula",
        "动量因子 / Momentum",
        "总收益率 / Total Return",
        "手续费实际扣除 / Actual Fee Deduction",
        "滑点实际扣除 / Actual Slippage Deduction",
        "总盈利/亏损 / Total Profit &amp; Loss",
        "成交详情 / Trade Details",
        "期末持仓 / Ending Positions",
        "净值走势与时点审计 / Equity & Point-in-time Audit",
        'id="interactive-report-data"',
        'data-chart-action="worst"',
        'data-chart-action="next-trade"',
        "持仓变化 / Position State",
        "关联成交 / Trades",
        'data-local-target="overview"',
        'data-local-target="metrics"',
        "成交前 / Before",
        "成交后 / After",
    ):
        assert expected_text in report
    assert "<details" not in report
    match = re.search(
        r'<script id="interactive-report-data" '
        r'type="application/json">(.*?)</script>',
        report,
        re.DOTALL,
    )
    assert match is not None
    interactive = json.loads(match.group(1))
    assert 1 <= len(interactive["points"]) <= 1_500
    assert interactive["snapshots"]
    assert any(
        snapshot["positions"]
        for snapshot in interactive["snapshots"].values()
    )



@pytest.mark.parametrize(
    "table",
    ["targets", "trades", "positions", "costs", "returns"],
)
def test_published_ledgers_use_formal_run_id(tmp_path: Path, table: str) -> None:
    _, published = _publish(tmp_path)
    frame = pl.read_parquet(published.path / "tables" / f"{table}.parquet")
    assert frame["run_id"].unique().to_list() == [published.manifest.run_id]


def test_repeat_publish_is_idempotent(tmp_path: Path) -> None:
    store, first = _publish(tmp_path)
    second = store.publish_success(
        _result(),
        snapshot=_snapshot(),
        resolved_config=_ResolvedConfig(),
        resolved_config_payload={"fixture": "a09"},
        resolved_config_hash=SHA_A,
        factor_versions=(
            FactorVersionReference(
                factor_name="momentum", factor_version="factor-v1"
            ),
        ),
        environment=_environment(),
        base_interval="1m",
        output=BacktestOutputConfig(root=tmp_path / "runs"),
    )
    assert second.already_published is True
    assert second.path == first.path
    assert second.manifest == first.manifest


def test_tampered_artifact_is_rejected(tmp_path: Path) -> None:
    _, published = _publish(tmp_path)
    metrics = published.path / "metrics.json"
    metrics.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ArtifactStoreError, match="mismatch"):
        RunArtifactStore.verify(published.path, published.manifest)


def test_report_rebuild_is_byte_identical_without_engine(tmp_path: Path) -> None:
    _, published = _publish(tmp_path)
    rebuilt = render_report_from_artifacts(
        published.path,
        output_path=tmp_path / "rebuilt.html",
    )
    assert sha256_file(rebuilt) == sha256_file(published.path / "report.html")


def test_failed_run_has_error_and_no_success_report(tmp_path: Path) -> None:
    store = RunArtifactStore(
        tmp_path / "runs", now=lambda: START + timedelta(days=2)
    )
    published = store.publish_failure(
        error=ValueError("intentional fixture failure"),
        snapshot=_snapshot(),
        resolved_config=_ResolvedConfig(),
        resolved_config_payload={"fixture": "a09-failed"},
        resolved_config_hash=SHA_B,
        factor_versions=(
            FactorVersionReference(factor_name="momentum", factor_version="v1"),
        ),
        environment=_environment(),
    )
    assert published.manifest.status == "failed"
    assert "intentional fixture failure" in (published.manifest.error or "")
    assert (published.path / "error.json").is_file()
    assert not (published.path / "report.html").exists()


def test_render_failure_cleans_staging_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_report(*args: object, **kwargs: object) -> None:
        raise ValueError("intentional renderer failure")

    monkeypatch.setattr(
        "bianbt.artifacts.store.render_report_from_artifacts", fail_report
    )
    store = RunArtifactStore(
        tmp_path / "runs", now=lambda: START + timedelta(days=2)
    )
    with pytest.raises(ValueError, match="renderer failure"):
        store.publish_success(
            _result(),
            snapshot=_snapshot(),
            resolved_config=_ResolvedConfig(),
            resolved_config_payload={"fixture": "a09"},
            resolved_config_hash=SHA_A,
            factor_versions=(
                FactorVersionReference(
                    factor_name="momentum", factor_version="factor-v1"
                ),
            ),
            environment=_environment(),
            base_interval="1m",
            output=BacktestOutputConfig(root=tmp_path / "runs"),
        )
    run_root = tmp_path / "runs"
    assert not [item for item in run_root.iterdir() if item.name != ".staging"]
    assert not list((run_root / ".staging").iterdir())


def test_unsafe_artifact_root_and_cli_contracts(tmp_path: Path) -> None:
    with pytest.raises(ArtifactStoreError, match="unsafe"):
        RunArtifactStore(Path("/"))
    runner = CliRunner()
    assert runner.invoke(app, ["run", "--help"]).exit_code == 0
    assert runner.invoke(app, ["report", "--help"]).exit_code == 0
