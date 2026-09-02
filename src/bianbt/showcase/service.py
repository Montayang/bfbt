"""Verified evidence collection and deterministic showcase publication."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

import polars as pl

from bianbt.artifacts.store import ArtifactStoreError, RunArtifactStore
from bianbt.data.hashing import content_sha256
from bianbt.data.manifests import (
    ManifestLoadError,
    RunManifest,
    load_manifest_auto,
    manifest_sha256,
)
from bianbt.showcase.models import ShowcaseRunReference, ShowcaseSpec
from bianbt.showcase.renderer import render_showcase


class ShowcaseError(RuntimeError):
    """Showcase evidence cannot be verified or published safely."""


def _json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShowcaseError(f"cannot read JSON evidence {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ShowcaseError(f"{path.name} must contain a JSON object")
    return payload


def _json_list(path: Path) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShowcaseError(f"cannot read JSON evidence {path.name}: {exc}") from exc
    if not isinstance(payload, list):
        raise ShowcaseError(f"{path.name} must contain a JSON array")
    return payload


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise ShowcaseError(f"missing evidence field: {'.'.join(keys)}")
        value = value[key]
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ShowcaseError(f"{label} evidence must be an object")
    return value


def _relative_link(source: Path, target: Path) -> str:
    return os.path.relpath(target.resolve(), source.resolve()).replace(os.sep, "/")


def _run_evidence(
    reference: ShowcaseRunReference,
    *,
    runs_root: Path,
    showcase_directory: Path,
) -> dict[str, Any]:
    run_directory = (runs_root / reference.run_id).resolve()
    try:
        run_directory.relative_to(runs_root.resolve())
    except ValueError as exc:
        raise ShowcaseError("run ID resolves outside runs_root") from exc
    if not run_directory.is_dir():
        raise ShowcaseError(f"showcase run is missing: {reference.run_id}")
    try:
        manifest = load_manifest_auto(run_directory / "manifest.json")
    except (ManifestLoadError, OSError, ValueError) as exc:
        raise ShowcaseError(f"invalid run manifest {reference.run_id}: {exc}") from exc
    if not isinstance(manifest, RunManifest):
        raise ShowcaseError(f"{reference.run_id} is not a terminal run manifest")
    if manifest.run_id != reference.run_id:
        raise ShowcaseError(f"manifest run ID mismatch: {reference.run_id}")
    if manifest.status != "succeeded":
        raise ShowcaseError(f"showcase run did not succeed: {reference.run_id}")
    try:
        RunArtifactStore.verify(run_directory, manifest)
    except ArtifactStoreError as exc:
        raise ShowcaseError(f"artifact verification failed for {reference.run_id}: {exc}") from exc

    metrics = _json_object(run_directory / "metrics.json")
    config = _json_object(run_directory / "resolved_config.json")
    environment = _json_object(run_directory / "environment.json")
    warnings = _json_list(run_directory / "warnings.json")
    if any(not isinstance(item, str) for item in warnings):
        raise ShowcaseError("warnings.json entries must be strings")
    metadata = _json_object(run_directory / "run_metadata.json")
    backtest = _object(_nested(config, "backtest"), "backtest")
    leverage = float(_nested(backtest, "risk", "leverage"))
    if leverage <= 0:
        raise ShowcaseError(f"invalid leverage in {reference.run_id}")
    trades_path = run_directory / "tables/trades.parquet"
    try:
        trades = pl.scan_parquet(trades_path, hive_partitioning=False)
        trade_count = int(trades.select(pl.len()).collect(engine="streaming").item())
        openings = (
            trades.filter(pl.col("side") == "BUY")
            .select(
                pl.col("fill_time"),
                pl.col("symbol"),
                (pl.col("notional").abs() / leverage).alias("margin"),
            )
            .sort("fill_time")
            .collect(engine="streaming")
        )
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise ShowcaseError(f"cannot inspect trades for {reference.run_id}: {exc}") from exc
    margins = [
        {
            "fill_time": row["fill_time"].isoformat().replace("+00:00", "Z"),
            "symbol": row["symbol"],
            "margin": row["margin"],
        }
        for row in openings.iter_rows(named=True)
    ]
    performance = _object(_nested(metrics, "performance"), "performance")
    risk = _object(_nested(metrics, "risk"), "risk")
    attribution = _object(_nested(metrics, "attribution"), "attribution")
    factors = _nested(config, "factor", "factors")
    if not isinstance(factors, list) or not factors:
        raise ShowcaseError("factor evidence must contain at least one factor")
    factor = factors[0]
    if not isinstance(factor, dict):
        raise ShowcaseError("factor evidence must be an object")
    factor_versions = metadata.get("factor_versions")
    if (
        not isinstance(factor_versions, list)
        or not factor_versions
        or not isinstance(factor_versions[0], dict)
    ):
        raise ShowcaseError("run metadata must contain a factor version")
    git_dirty = environment.get("git_dirty")
    git_commit = environment.get("git_commit")
    source_fingerprint = environment.get("source_fingerprint")
    if not isinstance(git_dirty, bool):
        raise ShowcaseError("environment git_dirty must be boolean")
    if not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64:
        raise ShowcaseError("environment source_fingerprint must be SHA-256")
    if not isinstance(git_commit, str) or not 7 <= len(git_commit) <= 64:
        raise ShowcaseError("environment git_commit must be a Git object ID")
    if margins and any(
        not isinstance(item["margin"], (int, float))
        or not math.isfinite(float(item["margin"]))
        or item["margin"] <= 0
        for item in margins
    ):
        raise ShowcaseError("opening margins must be positive finite numbers")
    datasets = [
        {
            "dataset_id": item.dataset_id,
            "dataset_version": item.dataset_version,
            "manifest_sha256": item.manifest_sha256,
        }
        for item in manifest.dataset_refs
    ]
    economic_backtest = {
        key: value for key, value in backtest.items() if key != "run"
    }
    economic_identity = {
        "factor": config.get("factor"),
        "universe": config.get("universe"),
        "backtest": economic_backtest,
    }
    return {
        "run_id": reference.run_id,
        "label": reference.label,
        "period_label": reference.period_label,
        "run_name": _nested(backtest, "run", "name"),
        "manifest_version": manifest.manifest_version,
        "manifest_sha256": manifest_sha256(manifest),
        "resolved_config_hash": manifest.resolved_config_hash,
        "economic_identity_sha256": content_sha256(economic_identity),
        "dataset_refs": datasets,
        "factor": {
            "name": factor.get("name"),
            "version": factor_versions[0].get("factor_version"),
            "parameters": factor.get("parameters", {}),
        },
        "execution": {
            "backend": _nested(backtest, "engine", "backend"),
            "mode": metadata.get("execution_mode"),
            "leverage": leverage,
            "sizing": _nested(backtest, "portfolio", "sizing"),
            "fill": _nested(backtest, "execution", "fill_price"),
            "fee": _nested(backtest, "execution", "fee"),
            "slippage": _nested(backtest, "execution", "slippage"),
            "funding": _nested(backtest, "execution", "funding"),
            "risk": _nested(backtest, "risk", "symbol_exits"),
            "core_start": _nested(backtest, "run", "start"),
            "core_end": _nested(backtest, "run", "end"),
        },
        "performance": {
            "initial_equity": performance["initial_equity"],
            "ending_equity": performance["ending_equity"],
            "total_return": performance["total_return"],
            "max_drawdown": performance["max_drawdown"],
            "sharpe_ratio": performance["sharpe_ratio"],
            "total_turnover": risk["total_turnover"],
            "gross_price_contribution": attribution["gross_price_contribution"],
            "fee_contribution": attribution["fee_contribution"],
            "slippage_contribution": attribution["slippage_contribution"],
            "funding_contribution": attribution["funding_contribution"],
        },
        "trade_count": trade_count,
        "opening_count": len(margins),
        "opening_margin_trajectory": margins,
        "warnings": warnings,
        "provenance": {
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "source_fingerprint": source_fingerprint,
            "python_version": environment.get("python_version"),
            "dependency_fingerprint": environment.get("dependency_fingerprint"),
        },
        "evidence_links": {
            "report": _relative_link(showcase_directory, run_directory / "report.html"),
            "manifest": _relative_link(showcase_directory, run_directory / "manifest.json"),
            "config": _relative_link(showcase_directory, run_directory / "resolved_config.json"),
            "metrics": _relative_link(showcase_directory, run_directory / "metrics.json"),
            "warnings": _relative_link(showcase_directory, run_directory / "warnings.json"),
        },
    }


def inspect_showcase(
    spec: ShowcaseSpec,
    *,
    runs_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Verify inputs and return deterministic evidence without writing files."""

    resolved_runs = runs_root.resolve()
    resolved_output = output_root.resolve()
    if resolved_output in {Path("/"), Path.home().resolve()}:
        raise ShowcaseError(f"unsafe showcase output root: {resolved_output}")
    showcase_directory = (resolved_output / spec.showcase_id).resolve()
    try:
        showcase_directory.relative_to(resolved_output)
    except ValueError as exc:
        raise ShowcaseError("showcase output resolves outside output_root") from exc
    try:
        showcase_directory.relative_to(resolved_runs)
    except ValueError:
        pass
    else:
        raise ShowcaseError("showcase output must be outside immutable runs_root")
    runs = [
        _run_evidence(
            reference,
            runs_root=resolved_runs,
            showcase_directory=showcase_directory,
        )
        for reference in spec.runs
    ]
    periods = {item.label: item for item in spec.intent.periods}
    for reference, run in zip(spec.runs, runs, strict=True):
        period = periods[reference.period_label]
        expected_start = period.start.isoformat().replace("+00:00", "Z")
        expected_end = period.end.isoformat().replace("+00:00", "Z")
        if run["execution"]["core_start"] != expected_start:
            raise ShowcaseError(f"run start does not match intent: {reference.run_id}")
        if run["execution"]["core_end"] != expected_end:
            raise ShowcaseError(f"run end does not match intent: {reference.run_id}")
        if run["factor"]["name"] != spec.intent.factor.name:
            raise ShowcaseError(f"run factor does not match intent: {reference.run_id}")
        if run["factor"]["parameters"] != spec.intent.factor.parameters:
            raise ShowcaseError(
                f"run factor parameters do not match intent: {reference.run_id}"
            )
        if not str(run["run_name"]).startswith(spec.strategy_identity + "-"):
            raise ShowcaseError(
                f"run name does not match strategy identity: {reference.run_id}"
            )
    economic_identities = {run["economic_identity_sha256"] for run in runs}
    if len(economic_identities) != 1:
        raise ShowcaseError("selected runs do not share one frozen economic identity")
    dirty_count = sum(bool(run["provenance"]["git_dirty"]) for run in runs)
    warning_count = sum(len(run["warnings"]) for run in runs)
    payload: dict[str, Any] = {
        "evidence_version": "bianbt-showcase-evidence/v1",
        "showcase_id": spec.showcase_id,
        "spec_sha256": content_sha256(spec),
        "intent_executable": spec.intent.executable,
        "intent": spec.intent.model_dump(mode="json"),
        "presentation": {
            "title": spec.title,
            "subtitle": spec.subtitle,
            "strategy_identity": spec.strategy_identity,
            "narrative": list(spec.narrative),
            "disclosures": list(spec.disclosures),
        },
        "summary": {
            "run_count": len(runs),
            "verified_run_count": len(runs),
            "dirty_run_count": dirty_count,
            "warning_count": warning_count,
            "provenance_status": "qualified" if dirty_count else "clean",
        },
        "runs": runs,
    }
    payload["evidence_sha256"] = content_sha256(payload)
    return payload


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            stream.write(content)
            temporary = stream.name
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def build_showcase(
    spec: ShowcaseSpec,
    *,
    runs_root: Path,
    output_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """Verify evidence and atomically publish a derived static presentation."""

    if not spec.intent.executable:
        raise ShowcaseError(
            "cannot build showcase with unresolved economic ambiguities: "
            + ", ".join(spec.intent.unresolved_ambiguities)
        )
    evidence = inspect_showcase(spec, runs_root=runs_root, output_root=output_root)
    destination = output_root.resolve() / spec.showcase_id
    evidence_text = json.dumps(
        evidence,
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    html = render_showcase(evidence)
    _atomic_text(destination / "evidence.json", evidence_text)
    _atomic_text(destination / "index.html", html)
    return destination / "index.html", evidence
