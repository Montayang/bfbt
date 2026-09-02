"""Load, resolve, and validate a bundle of YAML configuration files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from bfbt.config.backtest import BacktestConfig
from bfbt.config.bundle import ResolvedConfig
from bfbt.config.data import DataConfig
from bfbt.config.factor import FactorConfig
from bfbt.config.universe import UniverseConfig

_ALLOWED_ENVIRONMENT_OVERRIDES = {
    "BIANBT_DATA_ROOT",
    "BIANBT_OUTPUT_ROOT",
}


class ConfigLoadError(ValueError):
    """A configuration document could not be safely loaded."""


def project_root() -> Path:
    """Return the backtest project root, independent of the shell directory."""

    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ConfigPaths:
    data: Path
    universe: Path
    factor: Path
    backtest: Path

    @classmethod
    def defaults(cls, root: Path | None = None) -> "ConfigPaths":
        root = (root or project_root()).resolve()
        configs = root / "configs"
        return cls(
            data=configs / "data.yaml",
            universe=configs / "universe.yaml",
            factor=configs / "factor.yaml",
            backtest=configs / "backtest.yaml",
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(f"{path}: cannot read configuration: {exc}") from exc
    try:
        loaded = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"{path}: invalid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigLoadError(f"{path}: top-level YAML value must be a mapping")
    return loaded


def _resolve_path(value: Path | None, root: Path) -> Path | None:
    if value is None:
        return None
    return value.resolve() if value.is_absolute() else (root / value).resolve()


def _reject_dangerous_root(value: Path, project: Path, field: str) -> None:
    forbidden = {Path("/"), Path.home().resolve(), project, project.parent}
    if value in forbidden:
        raise ConfigLoadError(f"{field}: unsafe broad path {value}")


def _resolve_storage_paths(config: DataConfig, root: Path) -> DataConfig:
    storage = config.storage
    data_root = _resolve_path(storage.root, root)
    assert data_root is not None
    _reject_dangerous_root(data_root, root, "data.storage.root")
    updates = {
        "root": data_root,
        "raw": _resolve_path(storage.raw, root) or data_root / "raw",
        "normalized": _resolve_path(storage.normalized, root) or data_root / "normalized",
        "curated": _resolve_path(storage.curated, root) or data_root / "curated",
        "metadata": _resolve_path(storage.metadata, root) or data_root / "metadata",
    }
    return config.model_copy(update={"storage": storage.model_copy(update=updates)})


def _environment_overrides(environment: Mapping[str, str]) -> dict[str, str]:
    unknown = sorted(
        key
        for key in environment
        if key.startswith("BIANBT_") and key not in _ALLOWED_ENVIRONMENT_OVERRIDES
    )
    if unknown:
        raise ConfigLoadError(
            "unsupported BIANBT_ environment variables: " + ", ".join(unknown)
        )
    return {
        key: environment[key]
        for key in _ALLOWED_ENVIRONMENT_OVERRIDES
        if key in environment
    }


def load_config_bundle(
    paths: ConfigPaths | None = None,
    *,
    root: Path | None = None,
    environment: Mapping[str, str] | None = None,
    require_run_ready: bool = False,
) -> ResolvedConfig:
    """Load all configuration documents and return one immutable model."""

    root = (root or project_root()).resolve()
    paths = paths or ConfigPaths.defaults(root)
    environment = os.environ if environment is None else environment
    overrides = _environment_overrides(environment)

    data = DataConfig.model_validate(_load_yaml(paths.data))
    universe = UniverseConfig.model_validate(_load_yaml(paths.universe))
    factor = FactorConfig.model_validate(_load_yaml(paths.factor))
    backtest = BacktestConfig.model_validate(_load_yaml(paths.backtest))

    if "BIANBT_DATA_ROOT" in overrides:
        storage = data.storage.model_copy(
            update={"root": Path(overrides["BIANBT_DATA_ROOT"])}
        )
        data = data.model_copy(update={"storage": storage})
    data = _resolve_storage_paths(data, root)

    output_root = backtest.output.root
    if "BIANBT_OUTPUT_ROOT" in overrides:
        output_root = Path(overrides["BIANBT_OUTPUT_ROOT"])
    output_root = _resolve_path(output_root, root)
    assert output_root is not None
    _reject_dangerous_root(output_root, root, "backtest.output.root")
    backtest = backtest.model_copy(
        update={"output": backtest.output.model_copy(update={"root": output_root})}
    )

    resolved = ResolvedConfig(
        data=data,
        universe=universe,
        factor=factor,
        backtest=backtest,
    )
    if require_run_ready:
        resolved.assert_run_ready()
    return resolved
