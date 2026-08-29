"""Formal version-pinned backtest orchestration and terminal publication."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from bianbt.application.chunked import execute_chunked_pipeline
from bianbt.application.planning import contracts_scan_end, future_end, history_start
from bianbt.artifacts.v2 import V2AuditArtifacts, V2RunArtifactStore
from bianbt.artifacts.environment import EnvironmentInfo, capture_environment
from bianbt.artifacts.store import PublishedRun, RunArtifactStore
from bianbt.config.bundle import ResolvedConfig
from bianbt.config.fingerprint import canonical_config, config_fingerprint
from bianbt.engine.fast_matrix.capabilities import plan_backend
from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    FactorVersionReference,
)
from bianbt.data.storage import ParquetDataStore
from bianbt.engine.v2 import run_v2_backtest
from bianbt.engine.vectorized import run_vectorized_backtest
from bianbt.factors.registry import compute_factor
from bianbt.portfolio.constraints import construct_portfolio
from bianbt.universe.point_in_time import (
    build_point_in_time_universe,
    build_schedule,
)


class RunExecutionError(RuntimeError):
    """A formal run failed and, when possible, was published as failed."""

    def __init__(self, message: str, *, failed_run: PublishedRun | None = None):
        self.failed_run = failed_run
        super().__init__(message)


def _member_map(
    snapshot: DatasetSnapshotManifest,
) -> dict[str, DatasetReference]:
    return {item.dataset_name: item for item in snapshot.datasets}


def _require_member(
    members: dict[str, DatasetReference],
    name: str,
    *,
    start: datetime,
    end: datetime,
) -> DatasetReference:
    member = members.get(name)
    if member is None:
        raise ValueError(f"dataset snapshot is missing required member: {name}")
    if member.available_from > start or member.available_to < end:
        raise ValueError(
            f"dataset member {name} does not cover "
            f"[{start.isoformat()}, {end.isoformat()})"
        )
    return member


def _require_member_present(
    members: dict[str, DatasetReference], name: str
) -> DatasetReference:
    member = members.get(name)
    if member is None:
        raise ValueError(f"dataset snapshot is missing required member: {name}")
    return member


def execute_formal_run(
    config: ResolvedConfig,
    snapshot: DatasetSnapshotManifest,
    *,
    factor_name: str,
    catalog: DuckDBCatalog,
    project_root: Path,
    verify_hashes: bool = True,
    environment: EnvironmentInfo | None = None,
) -> PublishedRun:
    """Execute and atomically publish one complete local-data run."""

    config.assert_run_ready()
    backend = plan_backend(config.backtest)
    if backend.selected_backend == "fast_matrix":
        raise ValueError(
            "Fast Matrix creates fm-* research runs; use the matrix research "
            "application, then explicitly promote the candidate to Event/formal"
        )
    run = config.backtest.run
    assert run.start is not None and run.end is not None
    if run.dataset_version != snapshot.dataset_version:
        raise ValueError(
            "backtest.run.dataset_version must equal the resolved snapshot version"
        )
    try:
        factor_definition = next(
            item for item in config.factor.factors if item.name == factor_name
        )
    except StopIteration as exc:
        raise ValueError(f"factor is not configured: {factor_name}") from exc
    required_history_start = history_start(config, factor_name)
    required_future_end = future_end(config)
    members = _member_map(snapshot)
    bars_member = _require_member(
        members, "bars", start=required_history_start, end=required_future_end
    )
    contracts_member = (
        _require_member(members, "contracts", start=run.start, end=run.end)
        if config.universe.point_in_time.use_contract_snapshots
        else _require_member_present(members, "contracts")
    )
    mark_member = None
    if config.backtest.valuation.price == "mark_close":
        mark_member = _require_member(
            members, "mark_bars", start=run.start, end=required_future_end
        )
    funding_member = None
    if config.backtest.execution.funding.enabled:
        funding_member = _require_member(
            members, "funding", start=run.start, end=required_future_end
        )
    project_root = project_root.resolve()
    environment = environment or capture_environment(project_root)
    payload = canonical_config(config, project_root=project_root)
    fingerprint = config_fingerprint(config, project_root=project_root)
    configured_factors = (
        FactorVersionReference(
            factor_name=factor_definition.name,
            factor_version=factor_definition.version,
        ),
    )
    artifact_type = (
        V2RunArtifactStore
        if config.backtest.config_version == "v2"
        else RunArtifactStore
    )
    artifacts = artifact_type(
        config.backtest.output.root,
        catalog=catalog,
    )
    try:
        if config.backtest.performance.mode == "chunked":
            return execute_chunked_pipeline(
                config,
                snapshot,
                factor_name=factor_name,
                bars_member=bars_member,
                contracts_member=contracts_member,
                mark_member=mark_member,
                funding_member=funding_member,
                catalog=catalog,
                artifacts=artifacts,
                environment=environment,
                resolved_config_payload=payload,
                resolved_config_hash=fingerprint,
                verify_hashes=verify_hashes,
            )
        store = ParquetDataStore(
            normalized_root=config.data.storage.normalized,
            catalog=catalog,
            verify_hashes=verify_hashes,
        )
        base_interval = config.data.time.base_interval
        bars = store.scan_bars(
            dataset_version=bars_member.dataset_version,
            start=required_history_start,
            end=required_future_end,
            interval=base_interval,
        )
        contracts = store.scan_contracts(
            dataset_version=contracts_member.dataset_version,
            start=contracts_member.available_from,
            end=contracts_scan_end(config, contracts_member),
        )
        universe_schedule = build_schedule(
            start=run.start,
            end=run.end,
            interval=config.universe.schedule.interval,
        )
        universe = build_point_in_time_universe(
            bars.filter(pl.col("open_time") < run.end),
            contracts,
            universe_schedule,
            config=config.universe,
            base_interval=base_interval,
            bars_dataset_version=bars_member.dataset_version,
            contracts_dataset_version=contracts_member.dataset_version,
        )
        factor = compute_factor(
            bars,
            universe.frame,
            factor_definition,
            base_interval=base_interval,
            bars_dataset_version=bars_member.dataset_version,
            universe_version=universe.universe_version,
        )
        rebalance = build_schedule(
            start=run.start,
            end=run.end,
            interval=config.backtest.schedule.rebalance_interval,
        )
        scores = factor.frame.join(
            rebalance.select("timestamp"), on="timestamp", how="inner"
        )
        portfolio = construct_portfolio(
            scores,
            config.backtest.portfolio,
            factor_name=factor.factor_name,
            rank_scores=(
                factor.frame
                if (
                    config.backtest.config_version == "v2"
                    and config.backtest.portfolio.selection.clock == "factor"
                )
                else scores
            ),
            max_rank_lag=getattr(
                config.backtest.performance, "max_rank_lag", 0
            ),
            max_rank_state_rows=getattr(
                config.backtest.performance, "max_rank_state_rows", 1
            ),
            factor_version=factor.factor_version,
            universe_version=universe.universe_version,
        )
        mark_bars = (
            store.scan_mark_bars(
                dataset_version=mark_member.dataset_version,
                start=run.start,
                end=required_future_end,
                interval=base_interval,
            )
            if mark_member is not None
            else None
        )
        funding = (
            store.scan_funding(
                dataset_version=funding_member.dataset_version,
                start=run.start,
                end=required_future_end,
            )
            if funding_member is not None
            else None
        )
        if config.backtest.config_version == "v2":
            if portfolio.rankings is None:
                raise ValueError("V2 execution requires rankings")
            if portfolio.selections is None:
                raise ValueError("V2 execution requires selected rank rows")
            if config.backtest.portfolio.sizing.mode == "target_weight":
                rank_sources = portfolio.selections.select(
                    "signal_time",
                    "symbol",
                    "rank_source_time",
                )
                strategy = portfolio.frame.join(
                    rank_sources,
                    on=["signal_time", "symbol"],
                    how="left",
                )
            else:
                strategy = portfolio.selections
            v2_result = run_v2_backtest(
                strategy,
                portfolio.frame,
                portfolio.rankings,
                bars.filter(pl.col("open_time") >= run.start),
                mark_bars,
                funding,
                config=config.backtest,
                base_interval=base_interval,
                portfolio_version=portfolio.portfolio_version,
                bars_dataset_version=bars_member.dataset_version,
                mark_dataset_version=(
                    mark_member.dataset_version if mark_member is not None else None
                ),
                funding_dataset_version=(
                    funding_member.dataset_version
                    if funding_member is not None
                    else None
                ),
            )
            actual_factors = (
                FactorVersionReference(
                    factor_name=factor.factor_name,
                    factor_version=factor.factor_version,
                ),
            )
            assert isinstance(artifacts, V2RunArtifactStore)
            return artifacts.publish_success_v2(
                v2_result.result,
                audit=V2AuditArtifacts(
                    rankings=portfolio.rankings,
                    position_instructions=v2_result.position_instructions,
                    risk_events=v2_result.risk_events,
                    linked_trades=v2_result.linked_trades,
                    arbitration_trace=v2_result.arbitration_trace,
                    audit_result_hash=v2_result.audit_result_hash,
                ),
                snapshot=snapshot,
                resolved_config=config,
                resolved_config_payload=payload,
                resolved_config_hash=fingerprint,
                factor_versions=actual_factors,
                environment=environment,
                base_interval=base_interval,
                output=config.backtest.output,
                factor_values=factor.frame,
                universe=universe.frame,
            )
        result = run_vectorized_backtest(
            portfolio.frame,
            bars.filter(pl.col("open_time") >= run.start),
            mark_bars,
            funding,
            config=config.backtest,
            base_interval=base_interval,
            portfolio_version=portfolio.portfolio_version,
            bars_dataset_version=bars_member.dataset_version,
            mark_dataset_version=(
                mark_member.dataset_version if mark_member is not None else None
            ),
            funding_dataset_version=(
                funding_member.dataset_version if funding_member is not None else None
            ),
        )
        actual_factors = (
            FactorVersionReference(
                factor_name=factor.factor_name,
                factor_version=factor.factor_version,
            ),
        )
        return artifacts.publish_success(
            result,
            snapshot=snapshot,
            resolved_config=config,
            resolved_config_payload=payload,
            resolved_config_hash=fingerprint,
            factor_versions=actual_factors,
            environment=environment,
            base_interval=base_interval,
            output=config.backtest.output,
            factor_values=factor.frame,
            universe=universe.frame,
            rankings=portfolio.rankings,
            rank_selection_diagnostics=portfolio.selection_diagnostics,
        )
    except Exception as exc:
        try:
            failed = artifacts.publish_failure(
                error=exc,
                snapshot=snapshot,
                resolved_config=config,
                resolved_config_payload=payload,
                resolved_config_hash=fingerprint,
                factor_versions=configured_factors,
                environment=environment,
            )
        except Exception as publish_exc:
            raise RunExecutionError(
                f"run failed ({exc}); failed-run publication also failed ({publish_exc})"
            ) from exc
        raise RunExecutionError(str(exc), failed_run=failed) from exc
