"""User-run acceptance suite for milestone 01; Codex does not execute it."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from bianbt.cli import app
from bianbt.config.backtest import BacktestConfig, RunConfig
from bianbt.config.bundle import ResolvedConfig, RunReadinessError
from bianbt.config.data import DataConfig
from bianbt.config.fingerprint import config_fingerprint
from bianbt.config.loader import ConfigLoadError, ConfigPaths, load_config_bundle

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = BACKTEST_ROOT / "tests" / "fixtures" / "config" / "acceptance_01"


def test_default_files_are_a_valid_draft_and_paths_are_project_relative() -> None:
    config = load_config_bundle(root=BACKTEST_ROOT, environment={})

    default_dataset = BACKTEST_ROOT / "data" / "backtest" / "datasets" / "default"
    assert config.data.storage.root == default_dataset
    assert config.data.storage.raw == default_dataset / "raw"
    assert config.backtest.output.root == (
        BACKTEST_ROOT / "data" / "backtest" / "runs"
    )
    assert config.factor.factors[0].version == "v1"
    with pytest.raises(TypeError, match="immutable"):
        config.factor.factors[0].parameters["lookback"] = "7d"


def test_complete_fixture_is_run_ready() -> None:
    defaults = ConfigPaths.defaults(BACKTEST_ROOT)
    paths = ConfigPaths(
        data=defaults.data,
        universe=defaults.universe,
        factor=defaults.factor,
        backtest=FIXTURES / "backtest.yaml",
    )

    config = load_config_bundle(
        paths,
        root=BACKTEST_ROOT,
        environment={},
        require_run_ready=True,
    )

    assert config.backtest.run.name == "acceptance_01"
    assert config.backtest.execution.fee.taker_bps == 4.0


def test_draft_reports_all_missing_run_values_at_once() -> None:
    config = load_config_bundle(root=BACKTEST_ROOT, environment={})

    with pytest.raises(RunReadinessError) as captured:
        config.assert_run_ready()

    message = str(captured.value)
    assert "backtest.run.name" in message
    assert "backtest.run.start" in message
    assert "backtest.run.end" in message
    assert "backtest.run.dataset_version" in message
    assert "backtest.execution.fee.taker_bps" in message
    assert "backtest.execution.slippage.bps" in message


def test_invalid_quantile_is_rejected_with_a_field_path() -> None:
    with pytest.raises(ValidationError) as captured:
        BacktestConfig.model_validate(
            {"portfolio": {"long_quantile": 0.6, "short_quantile": 0.2}}
        )

    assert "portfolio.long_quantile" in str(captured.value)


def test_time_and_interval_contracts_are_enforced() -> None:
    with pytest.raises(ValidationError, match="UTC timezone"):
        RunConfig.model_validate(
            {
                "start": "2024-01-01T00:00:00",
                "end": "2024-02-01T00:00:00Z",
            }
        )

    with pytest.raises(ValidationError, match="integer multiple"):
        DataConfig.model_validate(
            {"time": {"base_interval": "5m", "derived_intervals": ["7m"]}}
        )


def test_cross_file_data_dependencies_are_enforced() -> None:
    defaults = ConfigPaths.defaults(BACKTEST_ROOT)
    paths = ConfigPaths(
        data=defaults.data,
        universe=defaults.universe,
        factor=defaults.factor,
        backtest=FIXTURES / "backtest.yaml",
    )
    config = load_config_bundle(paths, root=BACKTEST_ROOT, environment={})
    disabled_mark_bars = config.data.datasets.model_copy(
        update={
            "mark_bars": config.data.datasets.mark_bars.model_copy(
                update={"enabled": False}
            )
        }
    )
    changed_data = config.data.model_copy(update={"datasets": disabled_mark_bars})
    changed = ResolvedConfig(
        data=changed_data,
        universe=config.universe,
        factor=config.factor,
        backtest=config.backtest,
    )

    with pytest.raises(RunReadinessError, match="mark_bars.enabled"):
        changed.assert_run_ready()


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError) as captured:
        BacktestConfig.model_validate({"portfolio": {"long_quntile": 0.2}})

    assert "portfolio.long_quntile" in str(captured.value)
    assert "Extra inputs are not permitted" in str(captured.value)


def test_only_documented_environment_overrides_are_accepted(tmp_path: Path) -> None:
    data_root = tmp_path / "market-data"
    config = load_config_bundle(
        root=BACKTEST_ROOT,
        environment={"BIANBT_DATA_ROOT": str(data_root)},
    )
    assert config.data.storage.root == data_root
    assert config.data.storage.normalized == data_root / "normalized"

    with pytest.raises(ConfigLoadError, match="unsafe broad path"):
        load_config_bundle(
            root=BACKTEST_ROOT,
            environment={"BIANBT_DATA_ROOT": "/"},
        )

    with pytest.raises(ConfigLoadError, match="BIANBT_API_KEY"):
        load_config_bundle(
            root=BACKTEST_ROOT,
            environment={"BIANBT_API_KEY": "must-not-be-used"},
        )


def test_fingerprint_is_stable_across_project_locations(tmp_path: Path) -> None:
    first_root = tmp_path / "checkout-a" / "backtest"
    second_root = tmp_path / "checkout-b" / "backtest"
    paths = ConfigPaths.defaults(BACKTEST_ROOT)
    first = load_config_bundle(paths, root=first_root, environment={})
    second = load_config_bundle(paths, root=second_root, environment={})

    assert config_fingerprint(first, project_root=first_root) == config_fingerprint(
        second, project_root=second_root
    )


def test_cli_has_success_and_failure_exit_codes() -> None:
    runner = CliRunner()
    success = runner.invoke(
        app,
        [
            "config",
            "validate",
            "--run-ready",
            "--backtest",
            str(FIXTURES / "backtest.yaml"),
        ],
    )
    assert success.exit_code == 0, success.output
    assert "Configuration is valid (run-ready)." in success.output
    assert "resolved_config_hash=" in success.output

    failure = runner.invoke(
        app,
        [
            "config",
            "validate",
            "--backtest",
            str(FIXTURES / "invalid_quantile.yaml"),
        ],
    )
    assert failure.exit_code == 2
    assert "long_quantile" in failure.output
