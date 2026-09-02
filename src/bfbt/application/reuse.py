"""Dependency identities for reusable formal-run analysis and signals."""

from __future__ import annotations

from bfbt.artifacts.reuse import REUSE_ARTIFACT_VERSION
from bfbt.config.bundle import ResolvedConfig
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import DatasetSnapshotManifest, manifest_sha256


def analysis_dependency(
    config: ResolvedConfig,
    snapshot: DatasetSnapshotManifest,
    *,
    factor_name: str,
) -> dict[str, object]:
    run = config.backtest.run
    definition = next(item for item in config.factor.factors if item.name == factor_name)
    return {
        "artifact_version": REUSE_ARTIFACT_VERSION,
        "dataset_manifest_sha256": manifest_sha256(snapshot),
        "start": run.start.isoformat() if run.start is not None else None,
        "end": run.end.isoformat() if run.end is not None else None,
        "base_interval": config.data.time.base_interval,
        "universe": config.universe.model_dump(mode="json"),
        "factor": definition.model_dump(mode="json"),
        "factor_interval": config.backtest.schedule.factor_interval,
    }


def analysis_identity(
    config: ResolvedConfig,
    snapshot: DatasetSnapshotManifest,
    *,
    factor_name: str,
) -> tuple[str, str]:
    dependency = analysis_dependency(config, snapshot, factor_name=factor_name)
    digest = content_sha256(dependency)
    return f"analysis-{digest[:24]}", digest


def signal_dependency(
    config: ResolvedConfig,
    *,
    analysis_id: str,
) -> dict[str, object]:
    selection = config.backtest.portfolio.selection.model_dump(mode="json")
    selection.pop("audit_top_n", None)
    return {
        "artifact_version": REUSE_ARTIFACT_VERSION,
        "analysis_id": analysis_id,
        "selection": selection,
        "rebalance_interval": config.backtest.schedule.rebalance_interval,
    }


def signal_identity(
    config: ResolvedConfig,
    *,
    analysis_id: str,
) -> tuple[str, str]:
    dependency = signal_dependency(config, analysis_id=analysis_id)
    digest = content_sha256(dependency)
    return f"signal-{digest[:24]}", digest
