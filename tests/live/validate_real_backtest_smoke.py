"""Validate all published artifacts from the real Binance E2E smoke run."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import polars as pl

from bfbt.artifacts.store import RunArtifactStore
from bfbt.data.manifests import RunManifest, load_manifest

EXPECTED_SYMBOLS = {
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
}
TABLES = (
    "factor_values",
    "universe",
    "targets",
    "trades",
    "positions",
    "costs",
    "returns",
)


def _finite(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    run = args.run.resolve()
    manifest = load_manifest(run / "manifest.json", "run")
    if not isinstance(manifest, RunManifest) or manifest.status != "succeeded":
        raise AssertionError("run manifest is not succeeded")
    RunArtifactStore.verify(run, manifest)

    frames = {
        name: pl.read_parquet(run / "tables" / f"{name}.parquet")
        for name in TABLES
    }
    rows = {name: frame.height for name, frame in frames.items()}
    if any(value == 0 for value in rows.values()):
        raise AssertionError(f"published table is empty: {rows}")

    universe = frames["universe"]
    if set(universe["symbol"].unique()) != EXPECTED_SYMBOLS:
        raise AssertionError("universe symbols differ from the selected real market")
    if universe.filter(pl.col("is_eligible")).is_empty():
        raise AssertionError("universe has no eligible rows")
    factor = frames["factor_values"]
    if factor.filter(pl.col("is_valid")).is_empty():
        raise AssertionError("factor has no valid cross-sectional values")
    if set(frames["targets"]["side"].unique()) != {"LONG", "SHORT"}:
        raise AssertionError("targets do not contain both long and short sides")
    if set(frames["trades"]["side"].unique()) != {"BUY", "SELL"}:
        raise AssertionError("trades do not contain both buy and sell fills")

    positions = frames["positions"]
    if positions["quantity"].min() >= 0 or positions["quantity"].max() <= 0:
        raise AssertionError("positions do not contain both long and short exposure")
    costs = frames["costs"]
    for column in ("fee_cost", "slippage_cost", "funding_cashflow"):
        if costs[column].abs().sum() <= 0:
            raise AssertionError(f"cost path has no non-zero values: {column}")

    returns = frames["returns"]
    numeric_returns = returns.select(pl.selectors.numeric()).drop("run_id", strict=False)
    if not numeric_returns.select(pl.all().is_finite().all()).row(0).count(True) == len(
        numeric_returns.columns
    ):
        raise AssertionError("returns contain non-finite values")
    for name in ("targets", "trades", "positions", "costs", "returns"):
        if frames[name]["run_id"].unique().to_list() != [manifest.run_id]:
            raise AssertionError(f"{name} does not use the formal run ID")

    metrics = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
    performance = json.loads((run / "performance.json").read_text(encoding="utf-8"))
    if not _finite(metrics):
        raise AssertionError("metrics contain non-finite values")
    if not performance["memory_budget_passed"]:
        raise AssertionError("performance memory budget did not pass")
    row_budget = performance["max_input_rows_per_chunk"]
    if any(sum(chunk["input_rows"].values()) > row_budget for chunk in performance["chunks"]):
        raise AssertionError("a chunk exceeded the configured input row budget")
    analysis = [item for item in performance["chunks"] if item["phase"] == "analysis"]
    contract_rows = {item["input_rows"]["contracts"] for item in analysis}
    if not analysis or len(contract_rows) != 1 or min(contract_rows) <= 0:
        raise AssertionError("real exchangeInfo rows did not enter every analysis chunk")

    print(json.dumps({
        "run_id": manifest.run_id,
        "status": manifest.status,
        "rows": rows,
        "eligible_rows": universe.filter(pl.col("is_eligible")).height,
        "valid_factor_rows": factor.filter(pl.col("is_valid")).height,
        "fee_cost": costs["fee_cost"].sum(),
        "slippage_cost": costs["slippage_cost"].sum(),
        "funding_cashflow": costs["funding_cashflow"].sum(),
        "ending_equity": metrics["performance"]["ending_equity"],
        "total_return": metrics["performance"]["total_return"],
        "memory_budget_passed": performance["memory_budget_passed"],
        "artifacts_verified": len(manifest.artifact_hashes),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
