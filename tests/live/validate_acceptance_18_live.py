"""Run and validate the bounded A18 online DatasetSnapshot scenarios."""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import time
from pathlib import Path

import polars as pl

from bfbt.artifacts.store import RunArtifactStore
from bfbt.data.manifests import RunManifestV2, load_manifest_auto
from bfbt.data.v2_contracts import V2ReasonCode

SCENARIOS = (
    "exact_rank",
    "lag1_rank",
    "fixed_margin",
    "risk_conflict",
)
REQUIRED_TABLES = (
    "rankings",
    "position_instructions",
    "risk_events",
    "targets",
    "trades",
    "positions",
    "returns",
)


def _run(command: list[str], log: Path) -> str:
    started = time.monotonic()
    completed = subprocess.run(command, text=True, capture_output=True)
    elapsed = time.monotonic() - started
    payload = (
        f"command={' '.join(command)}\n"
        f"elapsed_seconds={elapsed:.6f}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}\n"
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(payload, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(
            f"command failed with code {completed.returncode}; log={log}"
        )
    return completed.stdout


def _value(output: str, name: str) -> str:
    prefix = f"{name}="
    values = [
        line[len(prefix) :]
        for line in output.splitlines()
        if line.startswith(prefix)
    ]
    if len(values) != 1:
        raise AssertionError(f"expected one {name} line, got {values}")
    return values[0]


def _command(context: dict[str, object], scenario: str) -> list[str]:
    configs = Path(str(context["config_root"]))
    return [
        "bfbt",
        "run",
        str(context["dataset_id"]),
        str(context["dataset_version"]),
        "momentum",
        "--database",
        str(context["database"]),
        "--data-config",
        str(configs / "data.json"),
        "--universe-config",
        str(configs / "universe.json"),
        "--factor-config",
        str(configs / "factor.json"),
        "--backtest-config",
        str(configs / f"backtest_{scenario}.json"),
    ]


def _directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _validate_run(run: Path, scenario: str) -> dict[str, object]:
    manifest = load_manifest_auto(run / "manifest.json")
    if not isinstance(manifest, RunManifestV2) or manifest.status != "succeeded":
        raise AssertionError(f"{scenario}: manifest is not succeeded run/v2")
    RunArtifactStore.verify(run, manifest)
    tables = {
        name: pl.read_parquet(run / "tables" / f"{name}.parquet")
        for name in REQUIRED_TABLES
    }
    required_nonempty = {
        "rankings",
        "position_instructions",
        "targets",
        "trades",
        "positions",
        "returns",
    }
    empty = [name for name in required_nonempty if not tables[name].height]
    if empty:
        raise AssertionError(f"{scenario}: empty required tables {empty}")
    for name in ("position_instructions", "targets", "trades", "positions", "returns"):
        if tables[name]["run_id"].unique().to_list() != [manifest.run_id]:
            raise AssertionError(f"{scenario}: {name} run_id mismatch")
    instructions = tables["position_instructions"]
    if scenario == "exact_rank":
        selected = instructions.filter(
            pl.col("priority") == 400
        )
        if set(selected["side"].unique()) != {"LONG", "SHORT"}:
            raise AssertionError("exact_rank did not execute both independent sides")
    elif scenario == "lag1_rank":
        lagged = instructions.filter(
            pl.col("rank_source_time").is_not_null()
        )
        if not lagged.height or lagged.filter(
            pl.col("rank_source_time") >= pl.col("decision_time")
        ).height:
            raise AssertionError("lag1_rank has missing or forward-looking Rank source")
    elif scenario == "fixed_margin":
        accepted = instructions.filter(
            (pl.col("instruction_mode") == "fixed_margin")
            & (pl.col("reason_code") == V2ReasonCode.ACCEPTED.value)
        )
        if not accepted.height:
            raise AssertionError("fixed_margin produced no accepted increments")
        if not accepted.filter(
            pl.col("requested_delta_notional").abs() == 200.0
        ).height:
            raise AssertionError("fixed_margin did not convert 100 margin at 2x")
    elif scenario == "risk_conflict":
        events = tables["risk_events"]
        if not events.height:
            raise AssertionError("risk_conflict produced no real-data risk event")
        reasons = set(events["reason_code"])
        if V2ReasonCode.STOP_LOSS_TRIGGERED.value not in reasons:
            raise AssertionError(
                "worst_case OHLC conflict did not select stop-loss"
            )
        if not tables["trades"].filter(
            pl.col("source_event_id").is_not_null()
        ).height:
            raise AssertionError("risk event did not link to an actual next-open fill")
    performance = json.loads(
        (run / "performance.json").read_text(encoding="utf-8")
    )
    if performance["input_trade_bar_rows"] > 100000:
        raise AssertionError("representative input exceeded the A18 row boundary")
    if performance["max_position_state_rows_observed"] > 20:
        raise AssertionError("position state exceeded its hard boundary")
    if performance["max_risk_state_rows_observed"] > 20:
        raise AssertionError("risk state exceeded its hard boundary")
    if not (run / "report.html").is_file():
        raise AssertionError("interactive report is missing")
    return {
        "run_id": manifest.run_id,
        "rows": {name: frame.height for name, frame in tables.items()},
        "performance": performance,
        "disk_bytes": _directory_size(run),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    context = json.loads(
        (root / "live-context.json").read_text(encoding="utf-8")
    )
    logs = root / "logs"
    records: dict[str, object] = {}
    for scenario in SCENARIOS:
        command = _command(context, scenario)
        first = _run(command, logs / f"{scenario}-first.log")
        second = _run(command, logs / f"{scenario}-second.log")
        first_id = _value(first, "run_id")
        second_id = _value(second, "run_id")
        if first_id != second_id:
            raise AssertionError(f"{scenario}: rerun changed run_id")
        if _value(second, "publication") != "already_published":
            raise AssertionError(f"{scenario}: rerun was not idempotent")
        run = Path(str(context["runs_root"])) / first_id
        record = _validate_run(run, scenario)
        rebuilt = root / "rebuilt-reports" / f"{scenario}.html"
        rebuilt.parent.mkdir(parents=True, exist_ok=True)
        _run(
            [
                "bfbt",
                "report",
                first_id,
                "--output-root",
                str(context["runs_root"]),
                "--output",
                str(rebuilt),
            ],
            logs / f"{scenario}-report.log",
        )
        if not rebuilt.is_file():
            raise AssertionError(f"{scenario}: rebuilt report is missing")
        records[scenario] = record
    summary = {
        "scope": {
            "symbols": context["symbols"],
            "download_range": context["download_range"],
            "run_range": context["run_range"],
            "scenario_count": len(SCENARIOS),
            "capacity_test": False,
        },
        "peak_child_rss_kib": resource.getrusage(
            resource.RUSAGE_CHILDREN
        ).ru_maxrss,
        "dataset_disk_bytes": _directory_size(root),
        "scenarios": records,
    }
    destination = root / "acceptance-summary.json"
    destination.write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
