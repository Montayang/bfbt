"""A10 bounded formal-run pipeline with overlap and stateful execution."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

from bfbt.application.planning import (
    contracts_scan_end,
    future_end,
    history_seconds,
    history_start,
)
from bfbt.application.reuse import analysis_identity, signal_identity
from bfbt.artifacts.environment import EnvironmentInfo
from bfbt.artifacts.reuse import (
    AnalysisSnapshotManifest,
    ReusableSnapshotStore,
    SignalSnapshotManifest,
    reuse_manifest_sha256,
)
from bfbt.artifacts.store import PublishedRun, RunArtifactStore
from bfbt.artifacts.v2 import V2AuditArtifacts, V2RunArtifactStore
from bfbt.config.bundle import ResolvedConfig
from bfbt.config.durations import duration_seconds
from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import content_sha256, sha256_file
from bfbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    FactorVersionReference,
    manifest_sha256,
)
from bfbt.data.storage import ParquetDataStore
from bfbt.engine.streaming import StreamingLedger
from bfbt.engine.v2_chunked import run_v2_backtest_chunked
from bfbt.engine.vectorized import (
    COST_SCHEMA,
    POSITION_SCHEMA,
    RETURN_SCHEMA,
    TARGET_SCHEMA,
    TRADE_SCHEMA,
    BacktestError,
    BacktestResult,
)
from bfbt.factors.registry import compute_factor
from bfbt.performance.chunks import plan_time_chunks
from bfbt.performance.diagnostics import PerformanceMonitor
from bfbt.performance.spool import ChunkWorkspace, ParquetSpool
from bfbt.portfolio.constraints import (
    construct_portfolio,
    finalize_v2_selections,
)
from bfbt.portfolio.crossover import FactorCrossoverTracker
from bfbt.portfolio.history import RankDescentTracker, RankHistoryBuffer
from bfbt.portfolio.ranking import RANKING_SCHEMA
from bfbt.universe.point_in_time import (
    build_point_in_time_universe,
    build_schedule,
)


def _count(frame: pl.LazyFrame) -> int:
    return int(
        frame.select(pl.len().alias("rows"))
        .collect(engine="streaming")
        .item()
    )


def _history_offsets(
    counts: dict[str, int],
    first_bars: dict[str, datetime],
) -> pl.LazyFrame:
    symbols = sorted(set(counts) | set(first_bars))
    return pl.DataFrame(
        {
            "symbol": symbols,
            "history_bars_offset": [counts.get(symbol, 0) for symbol in symbols],
            "prior_first_bar_open": [first_bars.get(symbol) for symbol in symbols],
        },
        schema={
            "symbol": pl.String,
            "history_bars_offset": pl.Int64,
            "prior_first_bar_open": pl.Datetime("ms", "UTC"),
        },
    ).lazy()


def _advance_history(
    bars: pl.LazyFrame,
    *,
    start: datetime,
    end: datetime,
    counts: dict[str, int],
    first_bars: dict[str, datetime],
) -> None:
    if end <= start:
        return
    rows = (
        bars.filter(
            (pl.col("open_time") >= pl.lit(start))
            & (pl.col("open_time") < pl.lit(end))
        )
        .group_by("symbol")
        .agg(
            pl.col("is_complete").cast(pl.Int64).sum().alias("complete"),
            pl.col("open_time").min().alias("first_open"),
        )
        .collect(engine="streaming")
        .to_dicts()
    )
    for row in rows:
        symbol = str(row["symbol"])
        counts[symbol] = counts.get(symbol, 0) + int(row["complete"])
        first = row["first_open"]
        if isinstance(first, datetime):
            first_bars[symbol] = min(first_bars.get(symbol, first), first)


def _part_scan(spool: ParquetSpool, table: str) -> pl.LazyFrame:
    path = spool.files(table)[-1]
    return pl.scan_parquet(path, hive_partitioning=False)


def _funding_exclusions(
    targets: pl.LazyFrame,
    funding: pl.LazyFrame | None,
    *,
    enabled: bool,
    policy: str,
) -> frozenset[str]:
    if not enabled or policy != "exclude_symbol":
        return frozenset()
    target_symbols = set(
        targets.select("symbol")
        .unique()
        .collect(engine="streaming")["symbol"]
        .to_list()
    )
    if funding is None:
        return frozenset(str(symbol) for symbol in target_symbols)
    rows = (
        funding.select("funding_time", "symbol")
        .unique()
        .collect(engine="streaming")
        .to_dicts()
    )
    event_symbols: dict[object, set[str]] = {}
    for row in rows:
        event_symbols.setdefault(row["funding_time"], set()).add(
            str(row["symbol"])
        )
    return frozenset(
        str(symbol)
        for symbol in target_symbols
        if not event_symbols
        or any(str(symbol) not in symbols for symbols in event_symbols.values())
    )


def _spool_identity(spool: ParquetSpool, tables: tuple[str, ...]) -> dict[str, object]:
    return {
        table: [sha256_file(path) for path in spool.files(table)]
        for table in tables
    }


def _snapshot_paths(
    store: ReusableSnapshotStore,
    *,
    kind: str,
    identity: str,
    manifest: AnalysisSnapshotManifest | SignalSnapshotManifest,
    table: str,
) -> tuple[Path, ...]:
    directory = store.directory(kind, identity)  # type: ignore[arg-type]
    return tuple(directory / part.path for part in manifest.tables[table])


def _restore_selection_state(
    config: ResolvedConfig,
    state: pl.DataFrame,
) -> RankHistoryBuffer | RankDescentTracker | FactorCrossoverTracker:
    selection = config.backtest.portfolio.selection
    maximum = config.backtest.performance.max_rank_state_rows
    if selection.mode == "factor_crossover":
        assert selection.crossover is not None
        return FactorCrossoverTracker(
            config=selection.crossover,
            max_state_rows=maximum,
            restored_state=state,
        )
    if selection.mode == "rank_descent":
        assert selection.descent is not None
        return RankDescentTracker(
            config=selection.descent,
            max_state_rows=maximum,
            restored_state=state,
        )
    return RankHistoryBuffer(
        lag=selection.lag,
        max_rank_lag=config.backtest.performance.max_rank_lag,
        max_state_rows=maximum,
        restored_state=state,
    )


def execute_chunked_pipeline(
    config: ResolvedConfig,
    snapshot: DatasetSnapshotManifest,
    *,
    factor_name: str,
    bars_member: DatasetReference,
    contracts_member: DatasetReference,
    mark_member: DatasetReference | None,
    funding_member: DatasetReference | None,
    catalog: DuckDBCatalog,
    artifacts: RunArtifactStore,
    environment: EnvironmentInfo,
    resolved_config_payload: dict[str, object],
    resolved_config_hash: str,
    verify_hashes: bool,
) -> PublishedRun:
    """Compute, spool, execute, and publish one bounded local-data run."""

    run = config.backtest.run
    assert run.start is not None and run.end is not None
    factor_definition = next(
        item for item in config.factor.factors if item.name == factor_name
    )
    base_interval = config.data.time.base_interval
    performance = config.backtest.performance
    monitor = PerformanceMonitor(
        mode=performance.mode,
        chunk_interval=performance.chunk_interval,
        max_input_rows_per_chunk=performance.max_input_rows_per_chunk,
        max_incremental_rss_mib=performance.max_incremental_rss_mib,
    )
    store = ParquetDataStore(
        normalized_root=config.data.storage.normalized,
        catalog=catalog,
        verify_hashes=verify_hashes,
    )
    contracts = store.scan_contracts(
        dataset_version=contracts_member.dataset_version,
        start=contracts_member.available_from,
        end=contracts_scan_end(config, contracts_member),
    )
    overlap = history_seconds(config, factor_name)
    analysis_chunks = plan_time_chunks(
        start=run.start,
        end=run.end,
        chunk_interval=performance.chunk_interval,
        overlap_seconds=overlap,
        earliest_input=history_start(config, factor_name),
    )
    output_root = config.backtest.output.root
    reuse_store: ReusableSnapshotStore | None = None
    analysis_manifest: AnalysisSnapshotManifest | None = None
    signal_manifest: SignalSnapshotManifest | None = None
    analysis_cache_hit = False
    signal_cache_hit = False
    analysis_id = analysis_digest = signal_id = signal_digest = ""
    if (
        config.backtest.config_version == "v2"
        and performance.reuse_mode != "off"
    ):
        assert performance.reuse_root is not None
        reuse_store = ReusableSnapshotStore(performance.reuse_root)
        analysis_id, analysis_digest = analysis_identity(
            config, snapshot, factor_name=factor_name
        )
        signal_id, signal_digest = signal_identity(
            config, analysis_id=analysis_id
        )
        if performance.reuse_mode != "refresh":
            analysis_manifest = reuse_store.load_analysis(analysis_id)
            signal_manifest = reuse_store.load_signal(signal_id)
            analysis_cache_hit = analysis_manifest is not None
            signal_cache_hit = signal_manifest is not None
            if signal_manifest is not None:
                if analysis_manifest is None:
                    raise BacktestError(
                        "signal snapshot exists without its analysis parent"
                    )
                if signal_manifest.analysis_manifest_sha256 != reuse_manifest_sha256(
                    analysis_manifest
                ):
                    raise BacktestError("signal snapshot parent hash mismatch")
    with ChunkWorkspace(output_root) as workspace:
        spool = workspace.spool
        history_counts: dict[str, int] = {}
        first_bars: dict[str, datetime] = {}
        factor_version = ""
        universe_version = ""
        portfolio_version = ""
        factor_state: pl.DataFrame | None = None
        rank_state: RankHistoryBuffer | RankDescentTracker | FactorCrossoverTracker | None = None
        has_rank_diagnostics = False
        if signal_manifest is not None and analysis_manifest is not None:
            assert reuse_store is not None
            factor_version = signal_manifest.factor_version
            universe_version = signal_manifest.universe_version
            spool.attach(
                "factor_values",
                _snapshot_paths(
                    reuse_store,
                    kind="analysis",
                    identity=analysis_id,
                    manifest=analysis_manifest,
                    table="factor_values",
                ),
            )
            spool.attach(
                "universe",
                _snapshot_paths(
                    reuse_store,
                    kind="analysis",
                    identity=analysis_id,
                    manifest=analysis_manifest,
                    table="universe",
                ),
            )
            spool.attach(
                "selections",
                _snapshot_paths(
                    reuse_store,
                    kind="signal",
                    identity=signal_id,
                    manifest=signal_manifest,
                    table="selections",
                ),
            )
            spool.attach(
                "rankings_full",
                _snapshot_paths(
                    reuse_store,
                    kind="signal",
                    identity=signal_id,
                    manifest=signal_manifest,
                    table="rankings_full",
                ),
            )
            rankings = spool.scan("rankings_full", schema=RANKING_SCHEMA)
            audit_top_n = config.backtest.portfolio.selection.audit_top_n
            if audit_top_n is not None:
                rankings = rankings.filter(pl.col("ordinal_rank") <= audit_top_n)
            spool.append_lazy("rankings", rankings)
            targets, portfolio_version = finalize_v2_selections(
                spool.scan("selections"),
                config.backtest.portfolio,
                factor_version=factor_version,
                universe_version=universe_version,
            )
            spool.append_lazy("targets", targets)

        elif analysis_manifest is not None:
            assert reuse_store is not None
            factor_version = analysis_manifest.factor_version
            universe_version = analysis_manifest.universe_version
            factor_paths = _snapshot_paths(
                reuse_store,
                kind="analysis",
                identity=analysis_id,
                manifest=analysis_manifest,
                table="factor_values",
            )
            if len(factor_paths) != len(analysis_chunks):
                raise BacktestError(
                    "analysis snapshot factor parts do not match the chunk plan"
                )
            spool.attach("factor_values", factor_paths)
            spool.attach(
                "universe",
                _snapshot_paths(
                    reuse_store,
                    kind="analysis",
                    identity=analysis_id,
                    manifest=analysis_manifest,
                    table="universe",
                ),
            )
            for chunk, factor_path in zip(analysis_chunks, factor_paths, strict=True):
                started = monitor.start()
                factor_part = pl.scan_parquet(
                    factor_path, hive_partitioning=False
                )
                rebalance = build_schedule(
                    start=chunk.start,
                    end=chunk.end,
                    interval=config.backtest.schedule.rebalance_interval,
                )
                scores = factor_part.join(
                    rebalance.select("timestamp"), on="timestamp", how="inner"
                )
                portfolio = construct_portfolio(
                    scores,
                    config.backtest.portfolio,
                    factor_name=factor_name,
                    rank_scores=(
                        factor_part
                        if config.backtest.portfolio.selection.clock == "factor"
                        else scores
                    ),
                    rank_state=rank_state,
                    max_rank_lag=performance.max_rank_lag,
                    max_rank_state_rows=performance.max_rank_state_rows,
                    factor_version=factor_version,
                    universe_version=universe_version,
                )
                rank_state = portfolio.rank_state
                if portfolio_version and portfolio.portfolio_version != portfolio_version:
                    raise RuntimeError("portfolio version changed between chunks")
                portfolio_version = portfolio.portfolio_version
                target_rows = spool.append_lazy("targets", portfolio.frame)
                ranking_rows = spool.append_lazy("rankings", portfolio.rankings)
                spool.append_lazy("rankings_full", portfolio.full_rankings)
                selection_rows = spool.append_lazy(
                    "selections", portfolio.selections
                )
                diagnostic_rows = 0
                if portfolio.selection_diagnostics is not None:
                    diagnostic_rows = spool.append_lazy(
                        "rank_selection_diagnostics",
                        portfolio.selection_diagnostics,
                    )
                    has_rank_diagnostics = True
                monitor.checkpoint(
                    phase="signal_rebuild",
                    ordinal=chunk.ordinal,
                    start=chunk.start,
                    end=chunk.end,
                    input_rows={"factor_values": _count(factor_part)},
                    output_rows={
                        "targets": target_rows,
                        "rankings": ranking_rows,
                        "selections": selection_rows,
                        "rank_selection_diagnostics": diagnostic_rows,
                        "rank_state_rows": (
                            rank_state.stats.state_rows if rank_state else 0
                        ),
                    },
                    started_at=started,
                )

        for index, chunk in enumerate(
            ()
            if signal_manifest is not None or analysis_manifest is not None
            else analysis_chunks
        ):
            started = monitor.start()
            bars = store.scan_bars(
                dataset_version=bars_member.dataset_version,
                start=chunk.input_start,
                end=chunk.end,
                interval=base_interval,
            )
            input_rows = {"bars": _count(bars), "contracts": _count(contracts)}
            monitor.check_rows(input_rows)
            schedule = build_schedule(
                start=chunk.start,
                end=chunk.end,
                interval=config.universe.schedule.interval,
            )
            universe = build_point_in_time_universe(
                bars,
                contracts,
                schedule,
                config=config.universe,
                base_interval=base_interval,
                bars_dataset_version=bars_member.dataset_version,
                contracts_dataset_version=contracts_member.dataset_version,
                history_offsets=_history_offsets(history_counts, first_bars),
            )
            if universe_version and universe.universe_version != universe_version:
                raise RuntimeError("universe version changed between chunks")
            universe_version = universe.universe_version
            universe_rows = spool.append_lazy("universe", universe.frame)
            universe_part = _part_scan(spool, "universe")
            factor = compute_factor(
                bars,
                universe_part,
                factor_definition,
                base_interval=base_interval,
                bars_dataset_version=bars_member.dataset_version,
                universe_version=universe_version,
                initial_state=factor_state,
                state_start=(chunk.start if factor_state is not None else None),
            )
            factor_state = factor.state
            if factor_version and factor.factor_version != factor_version:
                raise RuntimeError("factor version changed between chunks")
            factor_version = factor.factor_version
            factor_rows = spool.append_lazy("factor_values", factor.frame)
            factor_part = _part_scan(spool, "factor_values")
            rebalance = build_schedule(
                start=chunk.start,
                end=chunk.end,
                interval=config.backtest.schedule.rebalance_interval,
            )
            scores = factor_part.join(
                rebalance.select("timestamp"), on="timestamp", how="inner"
            )
            portfolio = construct_portfolio(
                scores,
                config.backtest.portfolio,
                factor_name=factor.factor_name,
                rank_scores=(
                    factor_part
                    if (
                        config.backtest.config_version == "v2"
                        and config.backtest.portfolio.selection.clock == "factor"
                    )
                    else scores
                ),
                rank_state=rank_state,
                max_rank_lag=getattr(performance, "max_rank_lag", 0),
                max_rank_state_rows=getattr(
                    performance, "max_rank_state_rows", 1
                ),
                factor_version=factor_version,
                universe_version=universe_version,
            )
            rank_state_rows = 0
            diagnostic_rows = 0
            if portfolio.rank_state is not None:
                state_path = workspace.path / "rank-state.parquet"
                state_temp = workspace.path / "rank-state.parquet.tmp"
                portfolio.rank_state.export_state().write_parquet(
                    state_temp, compression="zstd", statistics=True
                )
                state_temp.replace(state_path)
                if config.backtest.portfolio.selection.mode == "factor_crossover":
                    crossover = config.backtest.portfolio.selection.crossover
                    assert crossover is not None
                    rank_state = FactorCrossoverTracker(
                        config=crossover,
                        max_state_rows=(
                            config.backtest.performance.max_rank_state_rows
                        ),
                        restored_state=pl.read_parquet(state_path),
                    )
                elif config.backtest.portfolio.selection.mode == "rank_descent":
                    descent = config.backtest.portfolio.selection.descent
                    assert descent is not None
                    rank_state = RankDescentTracker(
                        config=descent,
                        max_state_rows=(
                            config.backtest.performance.max_rank_state_rows
                        ),
                        restored_state=pl.read_parquet(state_path),
                    )
                else:
                    rank_state = RankHistoryBuffer(
                        lag=config.backtest.portfolio.selection.lag,
                        max_rank_lag=config.backtest.performance.max_rank_lag,
                        max_state_rows=(
                            config.backtest.performance.max_rank_state_rows
                        ),
                        restored_state=pl.read_parquet(state_path),
                    )
                rank_state_rows = rank_state.stats.state_rows
            if portfolio.selection_diagnostics is not None:
                diagnostic_rows = spool.append_lazy(
                    "rank_selection_diagnostics",
                    portfolio.selection_diagnostics,
                )
                has_rank_diagnostics = True

            if portfolio_version and portfolio.portfolio_version != portfolio_version:
                raise RuntimeError("portfolio version changed between chunks")
            portfolio_version = portfolio.portfolio_version
            target_rows = spool.append_lazy("targets", portfolio.frame)
            ranking_rows = (
                spool.append_lazy("rankings", portfolio.rankings)
                if portfolio.rankings is not None else 0
            )
            if portfolio.full_rankings is not None:
                spool.append_lazy("rankings_full", portfolio.full_rankings)
            selection_rows = (
                spool.append_lazy("selections", portfolio.selections)
                if portfolio.selections is not None else 0
            )
            monitor.checkpoint(
                phase="analysis",
                ordinal=chunk.ordinal,
                start=chunk.start,
                end=chunk.end,
                input_rows=input_rows,
                output_rows={
                    "universe": universe_rows,
                    "factor_values": factor_rows,
                    "targets": target_rows,
                    "rankings": ranking_rows,
                    "selections": selection_rows,
                    "rank_selection_diagnostics": diagnostic_rows,
                    "rank_state_rows": rank_state_rows,
                },
                started_at=started,
            )
            if index + 1 < len(analysis_chunks):
                next_input = analysis_chunks[index + 1].input_start
                _advance_history(
                    bars,
                    start=chunk.input_start,
                    end=next_input,
                    counts=history_counts,
                    first_bars=first_bars,
                )

        if reuse_store is not None and signal_manifest is None:
            if analysis_manifest is None:
                analysis_manifest = reuse_store.publish_analysis(
                    analysis_id=analysis_id,
                    dataset_manifest_sha256=manifest_sha256(snapshot),
                    dependency_sha256=analysis_digest,
                    start=run.start,
                    end=run.end,
                    factor_name=factor_name,
                    factor_version=factor_version,
                    universe_version=universe_version,
                    tables={
                        "factor_values": spool.files("factor_values"),
                        "universe": spool.files("universe"),
                    },
                )
            signal_manifest = reuse_store.publish_signal(
                signal_id=signal_id,
                analysis_id=analysis_id,
                analysis_manifest_sha256=reuse_manifest_sha256(analysis_manifest),
                dependency_sha256=signal_digest,
                start=run.start,
                end=run.end,
                factor_version=factor_version,
                universe_version=universe_version,
                tables={
                    "selections": spool.files("selections"),
                    "rankings_full": spool.files("rankings_full"),
                },
            )

        if spool.row_count("targets") == 0:
            raise BacktestError("target input has no rows in the requested run range")
        targets_all = spool.scan("targets")
        execution_end = future_end(config)
        if config.backtest.config_version == "v2":
            if not isinstance(artifacts, V2RunArtifactStore):
                raise BacktestError("V2 chunked execution requires V2 artifacts")
            if (
                config.backtest.portfolio.selection.mode != "factor_crossover"
                and spool.row_count("rankings") == 0
            ):
                raise BacktestError("V2 chunked execution requires rankings")
            if spool.row_count("selections") == 0:
                raise BacktestError("V2 chunked execution requires selected ranks")
            rankings_all = spool.scan("rankings")
            selections_all = spool.scan("selections")
            if config.backtest.portfolio.sizing.mode == "target_weight":
                rank_sources = selections_all.select(
                    "signal_time",
                    "symbol",
                    "rank_source_time",
                )
                strategy_all = targets_all.join(
                    rank_sources,
                    on=["signal_time", "symbol"],
                    how="left",
                )
            else:
                strategy_all = selections_all
            trade_all = store.scan_bars(
                dataset_version=bars_member.dataset_version,
                start=run.start,
                end=execution_end,
                interval=base_interval,
            )
            mark_all = (
                store.scan_mark_bars(
                    dataset_version=mark_member.dataset_version,
                    start=run.start,
                    end=execution_end,
                    interval=base_interval,
                )
                if mark_member is not None
                else None
            )
            funding_all = (
                store.scan_funding(
                    dataset_version=funding_member.dataset_version,
                    start=run.start,
                    end=execution_end,
                )
                if funding_member is not None
                else None
            )
            v2_result = run_v2_backtest_chunked(
                strategy_all,
                targets_all,
                rankings_all,
                trade_all,
                mark_all,
                funding_all,
                config=config.backtest,
                base_interval=base_interval,
                portfolio_version=portfolio_version,
                bars_dataset_version=bars_member.dataset_version,
                mark_dataset_version=(
                    mark_member.dataset_version if mark_member is not None else None
                ),
                funding_dataset_version=(
                    funding_member.dataset_version
                    if funding_member is not None else None
                ),
                execution_start=run.start,
                execution_end=execution_end,
                output_root=config.backtest.output.root,
            )
            if reuse_store is not None:
                diagnostics = dict(v2_result.result.diagnostics or {})
                diagnostics["reuse"] = {
                    "analysis_id": analysis_id,
                    "signal_id": signal_id,
                    "analysis_cache_hit": analysis_cache_hit,
                    "signal_cache_hit": signal_cache_hit,
                    "analysis_manifest_sha256": (
                        reuse_manifest_sha256(analysis_manifest)
                        if analysis_manifest is not None else None
                    ),
                    "signal_manifest_sha256": (
                        reuse_manifest_sha256(signal_manifest)
                        if signal_manifest is not None else None
                    ),
                    "sparse_execution": performance.sparse_execution,
                }
                v2_result = replace(
                    v2_result,
                    result=replace(v2_result.result, diagnostics=diagnostics),
                )
            return artifacts.publish_success_v2(
                v2_result.result,
                audit=V2AuditArtifacts(
                    rankings=rankings_all,
                    position_instructions=v2_result.position_instructions,
                    risk_events=v2_result.risk_events,
                    linked_trades=v2_result.linked_trades,
                    arbitration_trace=v2_result.arbitration_trace,
                    audit_result_hash=v2_result.audit_result_hash,
                ),
                snapshot=snapshot,
                resolved_config=config,
                resolved_config_payload=resolved_config_payload,
                resolved_config_hash=resolved_config_hash,
                factor_versions=(
                    FactorVersionReference(
                        factor_name=factor_name,
                        factor_version=factor_version,
                    ),
                ),
                environment=environment,
                base_interval=base_interval,
                output=config.backtest.output,
                factor_values=spool.scan("factor_values"),
                universe=spool.scan("universe"),
            )
        funding_all = (
            store.scan_funding(
                dataset_version=funding_member.dataset_version,
                start=run.start,
                end=execution_end,
            )
            if funding_member is not None
            else None
        )
        funding_warnings: set[str] = set()
        if config.backtest.execution.funding.enabled and funding_all is not None:
            if _count(funding_all) == 0:
                policy = config.backtest.execution.funding.missing_policy
                if policy == "error":
                    raise BacktestError(
                        "funding input has no rows for dataset_version"
                    )
                funding_warnings.add(f"funding_{policy}:empty_dataset")

        excluded = _funding_exclusions(
            targets_all,
            funding_all,
            enabled=config.backtest.execution.funding.enabled,
            policy=config.backtest.execution.funding.missing_policy,
        )
        ledger = StreamingLedger(
            config=config.backtest,
            base_interval=base_interval,
            portfolio_version=portfolio_version,
            bars_dataset_version=bars_member.dataset_version,
            mark_dataset_version=(
                mark_member.dataset_version if mark_member is not None else None
            ),
            funding_dataset_version=(
                funding_member.dataset_version if funding_member is not None else None
            ),
            excluded_funding_symbols=excluded,
            initial_warnings=frozenset(funding_warnings),
        )
        delay = timedelta(
            seconds=(
                config.backtest.schedule.signal_delay_bars
                * duration_seconds(base_interval)
            )
        )
        execution_chunks = plan_time_chunks(
            start=run.start,
            end=execution_end,
            chunk_interval=performance.chunk_interval,
        )
        for chunk in execution_chunks:
            started = monitor.start()
            targets = targets_all.filter(
                (pl.col("signal_time") >= pl.lit(chunk.start - delay))
                & (pl.col("signal_time") < pl.lit(chunk.end - delay))
            )
            trade_bars = store.scan_bars(
                dataset_version=bars_member.dataset_version,
                start=chunk.start,
                end=chunk.end,
                interval=base_interval,
            )
            mark_bars = (
                store.scan_mark_bars(
                    dataset_version=mark_member.dataset_version,
                    start=chunk.start,
                    end=chunk.end,
                    interval=base_interval,
                )
                if mark_member is not None
                else None
            )
            funding = (
                funding_all.filter(
                    (pl.col("funding_time") >= pl.lit(chunk.start))
                    & (pl.col("funding_time") <= pl.lit(chunk.end))
                )
                if funding_all is not None
                else None
            )
            input_rows = {
                "targets": _count(targets),
                "trade_bars": _count(trade_bars),
                "mark_bars": _count(mark_bars) if mark_bars is not None else 0,
                "funding": _count(funding) if funding is not None else 0,
            }
            monitor.check_rows(input_rows)
            result = ledger.process(targets, trade_bars, mark_bars, funding)
            for table in ("targets", "trades", "positions", "costs", "returns"):
                spool.append_frame(f"ledger_{table}", getattr(result, table))
            monitor.checkpoint(
                phase="execution",
                ordinal=chunk.ordinal,
                start=chunk.start,
                end=chunk.end,
                input_rows=input_rows,
                output_rows=result.row_counts,
                started_at=started,
            )
        diagnostics = monitor.result()
        ledger_tables = ("targets", "trades", "positions", "costs", "returns")
        result_identity = {
            "engine_run_id": ledger.run_id,
            "parts": _spool_identity(
                spool,
                tuple(f"ledger_{name}" for name in ledger_tables),
            ),
            "warnings": sorted(ledger.state.warnings),
        }
        result_hash = f"a10-{content_sha256(result_identity)[:24]}"
        backtest_result = BacktestResult(
            run_id=ledger.run_id,
            result_hash=result_hash,
            targets=spool.scan("ledger_targets", schema=TARGET_SCHEMA),
            trades=spool.scan("ledger_trades", schema=TRADE_SCHEMA),
            positions=spool.scan("ledger_positions", schema=POSITION_SCHEMA),
            costs=spool.scan("ledger_costs", schema=COST_SCHEMA),
            returns=spool.scan("ledger_returns", schema=RETURN_SCHEMA),
            warnings=tuple(sorted(ledger.state.warnings)),
            diagnostics=(
                diagnostics.to_artifact_dict()
                if performance.collect_diagnostics
                else None
            ),
            presorted=True,
            execution_mode="chunked",
        )
        return artifacts.publish_success(
            backtest_result,
            snapshot=snapshot,
            resolved_config=config,
            resolved_config_payload=resolved_config_payload,
            resolved_config_hash=resolved_config_hash,
            factor_versions=(
                FactorVersionReference(
                    factor_name=factor_name,
                    factor_version=factor_version,
                ),
            ),
            environment=environment,
            base_interval=base_interval,
            output=config.backtest.output,
            factor_values=spool.scan("factor_values"),
            universe=spool.scan("universe"),
            rankings=spool.scan("rankings"),
            rank_selection_diagnostics=(
                spool.scan("rank_selection_diagnostics")
                if has_rank_diagnostics else None
            ),
        )
