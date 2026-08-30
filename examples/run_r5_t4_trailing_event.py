"""Run authorized monthly R5-T4 Event variants from reusable local datasets."""

from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

from bianbt.application.run import execute_formal_run
from bianbt.artifacts.environment import capture_environment
from bianbt.config.bundle import ResolvedConfig
from bianbt.data.catalog import DuckDBCatalog

from run_sampled_mean_rank5_event_2026_07 import (
    CATALOG_PATH,
    PROJECT_ROOT,
    _backtest_config,
    _data_config,
    _factor_config,
    _universe_config,
    _write_json,
)


VARIANTS = ("FIXED", "ROLLING")
FACTOR_PROFILES = {"M15": "15m", "H1": "1h", "H2": "2h"}
FACTOR_WORKSPACES = {
    "M15": "rdl_sampled_mean_ratio_15m12_pos_t4",
    "H1": "rdl_sampled_mean_ratio_1h12_pos_t4",
    "H2": "rdl_sampled_mean_ratio_2h12_pos_t4",
}
MONTHS: dict[str, dict[str, str]] = {
    "2026-05": {
        "dataset_id": "binance-usdm-perpetual-1m-2026-05-event",
        "dataset_version": "live-derived-6fed7b8df49b2c2bd661342b",
        "history_start": "2026-04-30T00:00:00+00:00",
        "start": "2026-05-01T00:00:00+00:00",
        "end": "2026-06-01T00:00:00+00:00",
        "data_end": "2026-06-01T00:02:00+00:00",
    },
    "2026-06": {
        "dataset_id": "binance-usdm-full-market-rank-descent-2026-06",
        "dataset_version": "live-11a24daad7b1e9a5f3643039",
        "history_start": "2026-05-31T00:00:00+00:00",
        "start": "2026-06-01T00:00:00+00:00",
        "end": "2026-07-01T00:00:00+00:00",
        "data_end": "2026-07-02T00:00:00+00:00",
    },
    "2026-07": {
        "dataset_id": "binance-usdm-full-market-rank-descent-2026-07",
        "dataset_version": "live-fd672b8e69b458d1c0076d74",
        "history_start": "2026-06-30T00:00:00+00:00",
        "start": "2026-07-01T00:00:00+00:00",
        "end": "2026-08-01T00:00:00+00:00",
        "data_end": "2026-08-02T00:00:00+00:00",
    },
}


def _month(month: str) -> dict[str, str]:
    try:
        return MONTHS[month]
    except KeyError as exc:
        raise ValueError(f"unknown R5-T4 month: {month}") from exc


def _monthly_data_config(month: str) -> dict[str, Any]:
    identity = _month(month)
    data = deepcopy(_data_config())
    data["time"] = {
        "base_interval": "1m",
        "derived_intervals": [],
        "start": identity["history_start"],
        "end": identity["data_end"],
    }
    return data


def resolved(
    variant: str, month: str, factor_profile: str = "M15"
) -> ResolvedConfig:
    if variant not in VARIANTS:
        raise ValueError(f"unknown T4 variant: {variant}")
    try:
        sample_interval = FACTOR_PROFILES[factor_profile]
    except KeyError as exc:
        raise ValueError(f"unknown T4 factor profile: {factor_profile}") from exc
    identity = _month(month)
    compact_month = month.replace("-", "")
    backtest = _backtest_config(
        "R5", f"T4-{variant}", take_profit=None, stop_loss=0.028
    )
    backtest["run"].update(
        {
            "name": (
                f"R5-T4-{variant}-{compact_month}-r01"
                if factor_profile == "M15"
                else f"R5-T4-{factor_profile}-{variant}-{compact_month}-r01"
            ),
            "start": identity["start"],
            "end": identity["end"],
            "dataset_version": identity["dataset_version"],
        }
    )
    backtest["risk"]["symbol_exits"]["trailing_stop"] = {
        "enabled": True,
        "distance": 0.028,
        "activation_distance": 0.058,
        "action": "close",
    }
    if variant == "ROLLING":
        backtest["capital"]["initial_equity"] = 2_000.0
        backtest["portfolio"]["sizing"] = {
            "mode": "rolling_margin",
            "rolling_initial_margin": 200.0,
            "rolling_reset_margin": 200.0,
            "rolling_min_margin": 80.0,
            "rolling_max_margin": 1_000.0,
            "reverse_policy": "net_delta",
        }
    factor = _factor_config("sampled_mean_ratio")
    factor["factors"][0]["parameters"]["sample_interval"] = sample_interval
    return ResolvedConfig.model_validate(
        {
            "data": _monthly_data_config(month),
            "universe": _universe_config(),
            "factor": factor,
            "backtest": backtest,
        }
    )


def _write_config(
    variant: str,
    month: str,
    config: ResolvedConfig,
    factor_profile: str = "M15",
) -> None:
    workspace = FACTOR_WORKSPACES[factor_profile]
    root = (
        Path(__file__).resolve().parents[1]
        / f"data/backtest/workspaces/{workspace}"
        / "configs"
        / month
        / variant
    )
    for name, model in (
        ("data", config.data),
        ("universe", config.universe),
        ("factor", config.factor),
        ("backtest", config.backtest),
    ):
        _write_json(root / f"{name}.json", model.model_dump(mode="json"))


def run(
    months: tuple[str, ...],
    variants: tuple[str, ...],
    factor_profile: str = "M15",
) -> None:
    catalog = DuckDBCatalog(CATALOG_PATH)
    environment = capture_environment(PROJECT_ROOT)
    for month in months:
        identity = _month(month)
        snapshot = catalog.resolve_dataset(
            identity["dataset_id"], identity["dataset_version"]
        )
        for variant in variants:
            config = resolved(variant, month, factor_profile)
            _write_config(variant, month, config, factor_profile)
            published = execute_formal_run(
                config,
                snapshot,
                factor_name="sampled_mean_ratio",
                catalog=catalog,
                project_root=PROJECT_ROOT,
                verify_hashes=True,
                environment=environment,
            )
            performance = published.metrics["performance"]
            print(
                f"{month} {factor_profile} {variant}: "
                f"run_id={published.manifest.run_id} "
                f"return={performance['total_return']:.6%} "
                f"report={published.path / 'report.html'}",
                flush=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", action="append", choices=tuple(MONTHS), required=True)
    parser.add_argument("--variant", action="append", choices=VARIANTS)
    parser.add_argument(
        "--factor-profile", choices=tuple(FACTOR_PROFILES), default="M15"
    )
    args = parser.parse_args()
    run(
        tuple(args.month),
        tuple(args.variant or VARIANTS),
        args.factor_profile,
    )


if __name__ == "__main__":
    main()
