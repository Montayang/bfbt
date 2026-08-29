"""User-run acceptance suite for A12 V2 configuration and contracts."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from bianbt.cli import app
from bianbt.config.backtest import (
    BacktestConfig,
    PortfolioV2Config,
    PositionSizingConfig,
    RankSelectionConfig,
    SymbolExitRuleConfig,
)
from bianbt.config.bundle import ResolvedConfig, RunReadinessError
from bianbt.config.fingerprint import config_fingerprint
from bianbt.config.loader import ConfigPaths, load_config_bundle
from bianbt.data.manifests import (
    ArtifactSchemaVersionReference,
    FactorVersionReference,
    RunDatasetReference,
    RunManifestV2,
    SchemaVersionReference,
    load_manifest_auto,
    manifest_json,
)
from bianbt.data.schemas import (
    get_schema_definition,
    list_artifact_schema_definitions,
    list_schema_definitions,
)
from bianbt.data.v2_contracts import (
    V2_EVENT_CONTRACT_VERSION,
    event_contract_descriptor,
    event_contract_fingerprint,
)

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
V2_CONFIG = BACKTEST_ROOT / "configs" / "backtest_v2.example.yaml"
FIXTURES = BACKTEST_ROOT / "tests" / "fixtures" / "config" / "acceptance_12"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _v2_payload() -> dict[str, object]:
    value = yaml.safe_load(V2_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _v2_paths() -> ConfigPaths:
    defaults = ConfigPaths.defaults(BACKTEST_ROOT)
    return ConfigPaths(
        data=defaults.data,
        universe=defaults.universe,
        factor=defaults.factor,
        backtest=V2_CONFIG,
    )


def _artifact_contract() -> dict[str, object]:
    result: dict[str, object] = {}
    for definition in list_artifact_schema_definitions():
        result[definition.dataset] = {
            "version": definition.version,
            "primary_key": list(definition.primary_key),
            "sort_key": list(definition.sort_key),
            "fields": [
                [field.name, str(field.type), field.nullable]
                for field in definition.schema
            ],
        }
    return result


def _artifact_references() -> tuple[ArtifactSchemaVersionReference, ...]:
    return tuple(
        ArtifactSchemaVersionReference(
            artifact_name=definition.dataset,
            schema_version=definition.version,
            schema_fingerprint=definition.fingerprint,
        )
        for definition in list_artifact_schema_definitions()
    )


def _run_manifest_v2(**updates: object) -> RunManifestV2:
    bars = get_schema_definition("bars", "v1")
    payload: dict[str, object] = {
        "run_id": "acceptance-a12",
        "created_at": "2026-08-03T10:00:00Z",
        "completed_at": "2026-08-03T10:01:00Z",
        "status": "failed",
        "error": "intentional contract-only fixture",
        "git_commit": "1" * 40,
        "python_version": "3.12.3",
        "dependency_fingerprint": SHA_A,
        "dataset_refs": [
            RunDatasetReference(
                dataset_id="acceptance-a12",
                dataset_version="dataset-v1",
                manifest_sha256=SHA_B,
            )
        ],
        "schema_versions": [
            SchemaVersionReference(
                dataset_name="bars",
                schema_version="v1",
                schema_fingerprint=bars.fingerprint,
            )
        ],
        "resolved_config_hash": SHA_C,
        "factor_versions": [
            FactorVersionReference(factor_name="momentum", factor_version="v1")
        ],
        "random_seed": 42,
        "artifact_hashes": [],
        "event_contract_fingerprint": event_contract_fingerprint(),
        "artifact_schema_versions": _artifact_references(),
    }
    payload.update(updates)
    return RunManifestV2.model_validate(payload)


def test_missing_config_version_is_a_v1_compatibility_adapter() -> None:
    legacy = BacktestConfig.model_validate(
        {
            "portfolio": {"long_quantile": 0.25, "short_quantile": 0.25},
            "risk": {"leverage": 2.0},
        }
    )
    explicit = BacktestConfig.model_validate(
        {
            "config_version": "v1",
            "portfolio": {"long_quantile": 0.25, "short_quantile": 0.25},
            "risk": {"leverage": 2.0},
        }
    )

    assert legacy.config_version == "v1"
    assert legacy.model_dump(exclude={"config_version"}) == explicit.model_dump(
        exclude={"config_version"}
    )
    assert legacy.portfolio.gross_exposure == 1.0
    assert legacy.risk.leverage == 2.0


def test_v2_example_round_trips_and_has_a_stable_fingerprint() -> None:
    config = BacktestConfig.model_validate(_v2_payload())
    restored = BacktestConfig.model_validate(config.model_dump(mode="python"))

    assert config == restored
    assert config.config_version == "v2"
    assert isinstance(config.portfolio, PortfolioV2Config)
    assert config.portfolio.selection.lag == 1
    assert config.portfolio.sizing.mode == "fixed_margin"
    assert config.capital is not None
    assert config_fingerprint(config, project_root=BACKTEST_ROOT) == (
        config_fingerprint(restored, project_root=BACKTEST_ROOT)
    )


def test_v1_and_v2_sections_are_strictly_dispatched() -> None:
    invalid = _v2_payload()
    invalid["portfolio"] = {
        "construction": "long_short_count",
        "long_count": 1,
        "short_count": 1,
    }
    with pytest.raises(ValidationError, match="selection"):
        BacktestConfig.model_validate(invalid)

    with pytest.raises(ValidationError, match="config_version"):
        BacktestConfig.model_validate({"config_version": "v3"})


def test_rank_contract_rejects_overlap_bad_ranges_and_empty_sides() -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        RankSelectionConfig.model_validate(
            {
                "long": {"ranks": [2]},
                "short": {"ranges": [[1, 3]]},
            }
        )
    with pytest.raises(ValidationError, match="inclusive"):
        RankSelectionConfig.model_validate(
            {"long": {"ranges": [[3, 2]]}}
        )
    with pytest.raises(ValidationError, match="at least one"):
        RankSelectionConfig.model_validate({})


def test_rank_lag_has_a_hard_configured_limit() -> None:
    payload = _v2_payload()
    payload["performance"]["max_rank_lag"] = 0
    with pytest.raises(ValidationError, match="selection.lag"):
        BacktestConfig.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "mode": "target_weight",
            "weighting": "equal",
            "target_gross_exposure": 1.0,
            "target_net_exposure": 0.0,
        },
        {
            "mode": "fixed_margin",
            "margin_amount": 100.0,
            "reverse_policy": "flatten_then_open",
        },
        {
            "mode": "fixed_notional",
            "notional_amount": 100.0,
            "reverse_policy": "net_delta",
        },
        {
            "mode": "equity_fraction",
            "fraction": 0.1,
            "reverse_policy": "flatten_only",
        },
        {
            "mode": "position_fraction",
            "fraction": 0.25,
            "reverse_policy": "net_delta",
            "zero_position_policy": "skip",
        },
    ],
)
def test_all_sizing_contracts_have_explicit_semantics(
    payload: dict[str, object],
) -> None:
    assert PositionSizingConfig.model_validate(payload).mode == payload["mode"]


def test_sizing_rejects_missing_or_cross_mode_fields() -> None:
    with pytest.raises(ValidationError, match="margin_amount"):
        PositionSizingConfig.model_validate(
            {
                "mode": "fixed_margin",
                "reverse_policy": "net_delta",
            }
        )
    with pytest.raises(ValidationError, match="zero_position_policy"):
        PositionSizingConfig.model_validate(
            {
                "mode": "position_fraction",
                "fraction": 0.25,
                "reverse_policy": "net_delta",
            }
        )
    with pytest.raises(ValidationError, match="incremental amounts"):
        PositionSizingConfig.model_validate(
            {
                "mode": "target_weight",
                "weighting": "equal",
                "target_gross_exposure": 1.0,
                "target_net_exposure": 0.0,
                "margin_amount": 100.0,
            }
        )


def test_symbol_exit_thresholds_can_be_symmetric_or_side_specific() -> None:
    symmetric = SymbolExitRuleConfig(
        enabled=True, distance=0.05, action="close"
    )
    asymmetric = SymbolExitRuleConfig(
        enabled=True,
        long_distance=0.04,
        short_distance=0.06,
        action="close",
    )
    assert symmetric.distance == 0.05
    assert asymmetric.long_distance == 0.04
    assert asymmetric.short_distance == 0.06

    with pytest.raises(ValidationError, match="either distance"):
        SymbolExitRuleConfig(
            enabled=True,
            distance=0.05,
            long_distance=0.04,
            short_distance=0.06,
        )
    with pytest.raises(ValidationError, match="both long_distance"):
        SymbolExitRuleConfig(enabled=True, long_distance=0.04)


def test_risk_frequency_and_mark_dependency_fail_in_bundle_validation() -> None:
    resolved = load_config_bundle(
        _v2_paths(), root=BACKTEST_ROOT, environment={}
    )

    frequency_payload = resolved.backtest.model_dump(mode="python")
    frequency_payload["risk"]["evaluation_interval"] = "30s"
    invalid_frequency = BacktestConfig.model_validate(frequency_payload)
    with pytest.raises(ValidationError, match="integer multiple"):
        ResolvedConfig(
            data=resolved.data,
            universe=resolved.universe,
            factor=resolved.factor,
            backtest=invalid_frequency,
        )

    mark_payload = resolved.backtest.model_dump(mode="python")
    mark_payload["risk"]["trigger_price"] = "mark"
    mark_risk = BacktestConfig.model_validate(mark_payload)
    datasets = resolved.data.datasets.model_copy(
        update={
            "mark_bars": resolved.data.datasets.mark_bars.model_copy(
                update={"enabled": False}
            )
        }
    )
    disabled_mark = resolved.data.model_copy(update={"datasets": datasets})
    with pytest.raises(ValidationError, match="trigger_price=mark"):
        ResolvedConfig(
            data=disabled_mark,
            universe=resolved.universe,
            factor=resolved.factor,
            backtest=mark_risk,
        )


def test_v2_draft_requires_run_fields_but_execution_is_supported() -> None:
    resolved = load_config_bundle(
        _v2_paths(), root=BACKTEST_ROOT, environment={}
    )
    with pytest.raises(
        RunReadinessError, match="backtest.run.name: required"
    ):
        resolved.assert_run_ready()

    resolved.backtest.assert_execution_supported()


def test_v1_market_registry_and_v2_artifact_registry_are_separate() -> None:
    assert [
        (item.dataset, item.version) for item in list_schema_definitions()
    ] == [
        ("bars", "v1"),
        ("contracts", "v1"),
        ("funding", "v1"),
        ("mark_bars", "v1"),
    ]
    assert [
        (item.dataset, item.version)
        for item in list_artifact_schema_definitions()
    ] == [
        ("position_instructions", "v1"),
        ("rankings", "v1"),
        ("risk_events", "v1"),
    ]


def test_schema_and_event_contracts_match_the_reviewable_golden() -> None:
    golden = json.loads(
        (FIXTURES / "v2_contract_golden.json").read_text(encoding="utf-8")
    )
    assert _artifact_contract() == golden["artifacts"]
    assert event_contract_descriptor() == golden["event_contract"]
    assert len(event_contract_fingerprint()) == 64


def test_run_v2_manifest_binds_all_contract_versions(tmp_path: Path) -> None:
    manifest = _run_manifest_v2()
    path = tmp_path / "run-manifest-v2.json"
    path.write_text(manifest_json(manifest), encoding="utf-8")
    restored = load_manifest_auto(path)

    assert isinstance(restored, RunManifestV2)
    assert restored.manifest_version == "run/v2"
    assert restored.config_version == "v2"
    assert restored.event_contract_version == V2_EVENT_CONTRACT_VERSION
    assert restored.event_contract_fingerprint == event_contract_fingerprint()
    assert manifest_json(restored, pretty=False) == manifest_json(
        manifest, pretty=False
    )

    incomplete = copy.deepcopy(manifest.model_dump(mode="python"))
    incomplete["artifact_schema_versions"] = incomplete[
        "artifact_schema_versions"
    ][:-1]
    with pytest.raises(ValidationError, match="must contain"):
        RunManifestV2.model_validate(incomplete)

    with pytest.raises(ValidationError, match="event_contract_fingerprint"):
        _run_manifest_v2(event_contract_fingerprint=SHA_A)


def test_cli_validates_v2_draft_and_reports_missing_run_fields() -> None:
    runner = CliRunner()
    listed = runner.invoke(app, ["schema", "list"])
    assert listed.exit_code == 0, listed.output
    assert "rankings/v1 sha256=" in listed.output
    assert "position_instructions/v1 sha256=" in listed.output
    assert "risk_events/v1 sha256=" in listed.output

    shown = runner.invoke(app, ["schema", "show", "rankings", "v1"])
    assert shown.exit_code == 0, shown.output
    assert "ordinal_rank" in shown.output

    validated = runner.invoke(
        app, ["config", "validate", "--backtest", str(V2_CONFIG)]
    )
    assert validated.exit_code == 0, validated.output
    assert "Configuration is valid (draft)." in validated.output

    guarded = runner.invoke(
        app,
        [
            "config",
            "validate",
            "--run-ready",
            "--backtest",
            str(V2_CONFIG),
        ],
    )
    assert guarded.exit_code == 2
    assert "backtest.run.name: required" in guarded.output
    assert "unavailable through A17" not in guarded.output
