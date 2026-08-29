"""Command-line entrypoint for the isolated backtest project."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated

import polars as pl
import typer
import yaml
from pydantic import ValidationError

from bianbt.application.run import RunExecutionError, execute_formal_run
from bianbt.artifacts.environment import EnvironmentError
from bianbt.artifacts.matrix import MatrixArtifactError, MatrixResearchStore
from bianbt.artifacts.store import ArtifactStoreError, RunArtifactStore

from bianbt.config.backtest import BacktestConfig
from bianbt.config.bundle import RunReadinessError
from bianbt.config.factor import FactorConfig
from bianbt.config.fingerprint import canonical_config, config_fingerprint
from bianbt.config.loader import (
    ConfigLoadError,
    ConfigPaths,
    load_config_bundle,
    project_root,
)
from bianbt.config.universe import UniverseConfig

from bianbt.data.catalog import (
    CatalogError,
    DuckDBCatalog,
    rebuild_catalog_from_directory,
)
from bianbt.data.ingest.raw_store import RawRestStore
from bianbt.data.ingest.service import ArchiveIngestService
from bianbt.data.manifests import (
    MANIFEST_MODELS,
    ManifestLoadError,
    RunManifest,
    load_manifest,
    load_manifest_auto,
    manifest_json,
    manifest_sha256,
)
from bianbt.data.normalize.service import NormalizationService
from bianbt.data.resample import resample_bars
from bianbt.data.storage import ParquetDataStore
from bianbt.data.validation.reports import QualityPolicy
from bianbt.engine.vectorized import run_vectorized_backtest
from bianbt.engine.fast_matrix.chunked import run_fast_matrix_chunked
from bianbt.engine.fast_matrix.kernel import MatrixExecutionError, run_fast_matrix
from bianbt.engine.fast_matrix.target_schedule import (
    TargetScheduleError,
    build_target_schedule,
)
from bianbt.factors.registry import compute_factor, list_factors
from bianbt.labels.forward_returns import compute_forward_returns
from bianbt.performance.chunks import plan_time_chunks
from bianbt.performance.spool import SpoolError, cleanup_stale_workspaces
from bianbt.portfolio.constraints import construct_portfolio
from bianbt.research.evaluator import evaluate_factor
from bianbt.reports.research_study import (
    ResearchStudyReportError,
    render_factor_study_reports,
)
from bianbt.reports.renderer import ReportError, render_report_from_artifacts
from bianbt.data.sources.base import ArchiveDiscoveryRequest, SourceError
from bianbt.data.sources.binance_archive import (
    BinanceArchiveSource,
    archive_candidates,
    local_archive_coverage,
)
from bianbt.data.sources.binance_rest import BinanceRestSource
from bianbt.data.sources.http import PublicHttpClient, RetryPolicy
from bianbt.data.schemas import (
    UnknownSchemaError,
    get_schema_definition,
    list_artifact_schema_definitions,
    list_schema_definitions,
)
from bianbt.universe.point_in_time import (
    build_point_in_time_universe,
    build_schedule,
)


app = typer.Typer(no_args_is_help=True, help="Binance perpetual backtest tools.")
config_app = typer.Typer(no_args_is_help=True, help="Validate and inspect config.")
schema_app = typer.Typer(no_args_is_help=True, help="Inspect Arrow data contracts.")
manifest_app = typer.Typer(no_args_is_help=True, help="Validate metadata manifests.")
catalog_app = typer.Typer(no_args_is_help=True, help="Manage the local DuckDB catalog.")
data_app = typer.Typer(no_args_is_help=True, help="Ingest public Binance market data.")
universe_app = typer.Typer(
    no_args_is_help=True, help="Build point-in-time contract universes."
)
research_app = typer.Typer(
    no_args_is_help=True, help="Compute factors, labels, and research diagnostics."
)
backtest_app = typer.Typer(
    no_args_is_help=True, help="Construct portfolios and preview execution ledgers."
)
performance_app = typer.Typer(
    no_args_is_help=True, help="Plan chunks and inspect bounded-run diagnostics."
)
app.add_typer(config_app, name="config")
app.add_typer(schema_app, name="schema")
app.add_typer(manifest_app, name="manifest")
app.add_typer(catalog_app, name="catalog")
app.add_typer(data_app, name="data")
app.add_typer(universe_app, name="universe")
app.add_typer(research_app, name="research")
app.add_typer(backtest_app, name="backtest")
app.add_typer(performance_app, name="performance")

PathOption = Annotated[Path | None, typer.Option()]


def _paths(
    data: Path | None,
    universe: Path | None,
    factor: Path | None,
    backtest: Path | None,
) -> ConfigPaths:
    defaults = ConfigPaths.defaults()
    return ConfigPaths(
        data=data or defaults.data,
        universe=universe or defaults.universe,
        factor=factor or defaults.factor,
        backtest=backtest or defaults.backtest,
    )


def _load_config_or_exit(paths: ConfigPaths, run_ready: bool):
    try:
        return load_config_bundle(paths, require_run_ready=run_ready)
    except (ConfigLoadError, ValidationError, RunReadinessError) as exc:
        typer.echo(f"Configuration error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc


@config_app.command("validate")
def validate_config(
    data: PathOption = None,
    universe: PathOption = None,
    factor: PathOption = None,
    backtest: PathOption = None,
    run_ready: Annotated[
        bool,
        typer.Option(
            "--run-ready/--draft",
            help="Require all values needed to start a backtest.",
        ),
    ] = False,
) -> None:
    """Validate four YAML files and print their resolved fingerprint."""

    config = _load_config_or_exit(_paths(data, universe, factor, backtest), run_ready)
    fingerprint = config_fingerprint(config, project_root=project_root())
    mode = "run-ready" if run_ready else "draft"
    typer.echo(f"Configuration is valid ({mode}).")
    typer.echo(f"resolved_config_hash={fingerprint}")


@config_app.command("show")
def show_config(
    data: PathOption = None,
    universe: PathOption = None,
    factor: PathOption = None,
    backtest: PathOption = None,
) -> None:
    """Print the fully expanded and path-stabilized configuration."""

    config = _load_config_or_exit(_paths(data, universe, factor, backtest), False)
    payload = canonical_config(config, project_root=project_root())
    typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=True))


@research_app.command("matrix-run")
def matrix_research_run(
    target_schedule: Annotated[Path, typer.Argument(help="TargetSchedule parquet.")],
    trade_bars: Annotated[Path, typer.Argument(help="Version-pinned trade bars parquet.")],
    rebalance_times: Annotated[
        Path, typer.Option(help="JSON array of complete rebalance timestamps.")
    ],
    parent_manifest_sha256: Annotated[
        str, typer.Option(help="Exact parent SignalSnapshot manifest SHA-256.")
    ],
    market_identity: Annotated[
        str, typer.Option(help="Exact dataset/market content identity.")
    ],
    backtest_config: Annotated[
        Path, typer.Option(help="V2 backtest YAML with engine=fast_matrix/research.")
    ],
    output_root: Annotated[
        Path, typer.Option(help="MatrixResearchRun root.")
    ] = Path("data/backtest/research_runs"),
    mark_bars: Annotated[Path | None, typer.Option()] = None,
    funding: Annotated[Path | None, typer.Option()] = None,
    research_context: Annotated[
        Path | None,
        typer.Option(help="Optional JSON factor/study context embedded in the report."),
    ] = None,
) -> None:
    """Execute and publish an fm-* research run; never a formal run."""

    try:
        payload = yaml.safe_load(backtest_config.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("backtest config must be a YAML mapping")
        config = BacktestConfig.model_validate(payload)
        raw_times = json.loads(rebalance_times.read_text(encoding="utf-8"))
        if not isinstance(raw_times, list):
            raise ValueError("rebalance times must be a JSON array")
        schedule = build_target_schedule(
            pl.read_parquet(target_schedule),
            rebalance_times=tuple(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in raw_times),
            parent_manifest_sha256=parent_manifest_sha256,
        )
        trade_scan = pl.scan_parquet(trade_bars, hive_partitioning=False)
        mark_scan = None if mark_bars is None else pl.scan_parquet(mark_bars, hive_partitioning=False)
        funding_scan = None if funding is None else pl.scan_parquet(funding, hive_partitioning=False)
        context: dict[str, object] = {}
        if research_context is not None:
            raw_context = json.loads(research_context.read_text(encoding="utf-8"))
            if not isinstance(raw_context, dict):
                raise ValueError("research context must be a JSON object")
            context = raw_context
        if config.performance.mode == "chunked":
            result = run_fast_matrix_chunked(
                schedule, trade_scan, config=config, market_identity=market_identity,
                mark_bars=mark_scan, funding=funding_scan,
            )
        else:
            result = run_fast_matrix(
                schedule, trade_scan, config=config, market_identity=market_identity,
                mark_bars=mark_scan, funding=funding_scan,
                max_market_rows=config.performance.max_input_rows_per_chunk,
            )
        root = output_root if output_root.is_absolute() else project_root() / output_root
        manifest = MatrixResearchStore(root).publish(
            result, schedule, resolved_config=config.model_dump(mode="json"),
            market_identity=market_identity, research_context=context,
        )
    except (
        OSError, json.JSONDecodeError, ValidationError, ValueError,
        MatrixExecutionError, TargetScheduleError, MatrixArtifactError,
    ) as exc:
        typer.echo(f"Matrix research error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"run_id={manifest.run_id}")
    typer.echo("status=research_published")
    typer.echo(f"run_path={MatrixResearchStore(root).directory(manifest.run_id)}")


@research_app.command("study-report")
def render_study_report(
    study_root: Annotated[
        Path, typer.Argument(help="Succeeded factor study directory containing summary.json.")
    ],
    matrix_runs_root: Annotated[
        Path, typer.Option(help="MatrixResearchRun root used to rebuild detailed reports.")
    ] = Path("data/backtest/research_runs"),
) -> None:
    """Build separate searchable quick-research and Fast Matrix reports."""
    try:
        root = study_root if study_root.is_absolute() else project_root() / study_root
        runs = (
            matrix_runs_root
            if matrix_runs_root.is_absolute()
            else project_root() / matrix_runs_root
        )
        rendered = render_factor_study_reports(root, matrix_runs_root=runs)
    except (OSError, MatrixArtifactError, ResearchStudyReportError, ValueError) as exc:
        typer.echo(f"Research study report error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    for name, path in rendered.items():
        typer.echo(f"{name}={path}")


@schema_app.command("list")
def list_schemas() -> None:
    """List exact registered schema versions and fingerprints."""

    definitions = (
        *list_schema_definitions(),
        *list_artifact_schema_definitions(),
    )
    for definition in sorted(
        definitions, key=lambda item: (item.dataset, item.version)
    ):
        typer.echo(
            f"{definition.dataset}/{definition.version} "
            f"sha256={definition.fingerprint}"
        )


@schema_app.command("show")
def show_schema(
    dataset: Annotated[str, typer.Argument(help="Dataset name, for example bars.")],
    version: Annotated[str, typer.Argument(help="Exact schema version, for example v1.")],
) -> None:
    """Print one logical Arrow schema descriptor as YAML."""

    try:
        definition = get_schema_definition(dataset, version)
    except UnknownSchemaError as exc:
        typer.echo(f"Schema error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    payload = definition.descriptor()
    payload["fingerprint"] = definition.fingerprint
    typer.echo(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


def _load_manifest_or_exit(path: Path, kind: str):
    try:
        return load_manifest(path, kind)
    except (
        ManifestLoadError,
        ValidationError,
        UnknownSchemaError,
    ) as exc:
        choices = ", ".join(sorted(MANIFEST_MODELS))
        typer.echo(
            f"Manifest error (kind must be one of {choices}):\n{exc}",
            err=True,
        )
        raise typer.Exit(code=2) from exc


@manifest_app.command("validate")
def validate_manifest(
    kind: Annotated[
        str,
        typer.Argument(help="raw, partition, dataset, run, or run-v2."),
    ],
    path: Annotated[Path, typer.Argument(help="JSON manifest path.")],
) -> None:
    """Validate a JSON manifest and print its canonical content hash."""

    manifest = _load_manifest_or_exit(path, kind)
    typer.echo(f"Manifest is valid ({manifest.manifest_version}).")
    typer.echo(f"manifest_sha256={manifest_sha256(manifest)}")


@manifest_app.command("show")
def show_manifest(
    kind: Annotated[
        str,
        typer.Argument(help="raw, partition, dataset, run, or run-v2."),
    ],
    path: Annotated[Path, typer.Argument(help="JSON manifest path.")],
) -> None:
    """Print a validated manifest in normalized JSON form."""

    manifest = _load_manifest_or_exit(path, kind)
    typer.echo(manifest_json(manifest), nl=False)


def _catalog_or_exit(operation):
    try:
        return operation()
    except (CatalogError, ManifestLoadError, ValidationError) as exc:
        typer.echo(f"Catalog error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc


def _print_catalog_info(info) -> None:
    typer.echo(f"database={info.path}")
    typer.echo(f"catalog_schema_version={info.schema_version}")
    for count in info.counts:
        typer.echo(f"{count.table}={count.rows}")


DatabaseOption = Annotated[
    Path,
    typer.Option("--database", help="DuckDB catalog file path."),
]


@catalog_app.command("init")
def initialize_catalog(database: DatabaseOption) -> None:
    """Create or migrate a catalog without registering data manifests."""

    info = _catalog_or_exit(lambda: DuckDBCatalog(database).initialize())
    typer.echo("Catalog is initialized.")
    _print_catalog_info(info)


@catalog_app.command("info")
def catalog_info(database: DatabaseOption) -> None:
    """Show schema version and control-plane row counts."""

    info = _catalog_or_exit(lambda: DuckDBCatalog(database).info())
    _print_catalog_info(info)


@catalog_app.command("register")
def register_catalog_manifest(
    kind: Annotated[str, typer.Argument(help="raw, partition, dataset, or run.")],
    path: Annotated[Path, typer.Argument(help="JSON manifest path.")],
    database: DatabaseOption,
) -> None:
    """Validate and transactionally register one manifest."""

    def operation():
        manifest = load_manifest(path, kind)
        return DuckDBCatalog(database).register(manifest)

    result = _catalog_or_exit(operation)
    action = "inserted" if result.inserted else "already_registered"
    typer.echo(f"registration={action}")
    typer.echo(f"kind={result.kind}")
    typer.echo(f"identifier={result.identifier}")
    typer.echo(f"manifest_sha256={result.manifest_sha256}")


@catalog_app.command("resolve")
def resolve_catalog_dataset(
    dataset_id: Annotated[str, typer.Argument(help="Dataset snapshot identifier.")],
    dataset_version: Annotated[str, typer.Argument(help="Exact dataset version.")],
    database: DatabaseOption,
) -> None:
    """Resolve one exact dataset snapshot and print normalized JSON."""

    manifest = _catalog_or_exit(
        lambda: DuckDBCatalog(database).resolve_dataset(
            dataset_id, dataset_version
        )
    )
    typer.echo(manifest_json(manifest), nl=False)


@catalog_app.command("coverage")
def catalog_coverage(
    dataset_name: Annotated[str, typer.Argument(help="Fact dataset name.")],
    dataset_version: Annotated[str, typer.Argument(help="Exact data version.")],
    database: DatabaseOption,
) -> None:
    """Summarize registered partitions for one exact fact-data version."""

    summary = _catalog_or_exit(
        lambda: DuckDBCatalog(database).coverage(dataset_name, dataset_version)
    )
    typer.echo(f"dataset={summary.dataset_name}/{summary.dataset_version}")
    typer.echo(f"partition_count={summary.partition_count}")
    typer.echo(f"row_count={summary.row_count}")
    available_from = (
        summary.available_from.isoformat()
        if summary.available_from is not None
        else "null"
    )
    available_to = (
        summary.available_to.isoformat()
        if summary.available_to is not None
        else "null"
    )
    typer.echo(f"available_from={available_from}")
    typer.echo(f"available_to={available_to}")
    typer.echo(
        f"max_symbols_per_partition={summary.max_symbols_per_partition}"
    )
    typer.echo(f"quality_report_ids={','.join(summary.quality_report_ids)}")


@catalog_app.command("rebuild")
def rebuild_catalog_command(
    manifest_root: Annotated[
        Path, typer.Argument(help="Directory recursively containing JSON manifests.")
    ],
    database: DatabaseOption,
) -> None:
    """Atomically replace a catalog rebuilt from validated manifests."""

    info = _catalog_or_exit(
        lambda: rebuild_catalog_from_directory(database, manifest_root)
    )
    typer.echo("Catalog was rebuilt atomically.")
    _print_catalog_info(info)


def _source_or_exit(operation):
    try:
        return operation()
    except (SourceError, CatalogError, ValidationError, OSError, ValueError) as exc:
        typer.echo(f"Data source error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc


def _timestamp(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamps must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamps must be expressed in UTC")
    return parsed


def _archive_request(
    dataset: str,
    symbol: str,
    interval: str | None,
    frequency: str,
    start: str,
    end: str,
) -> ArchiveDiscoveryRequest:
    return ArchiveDiscoveryRequest.model_validate(
        {
            "dataset_name": dataset,
            "symbol": symbol,
            "interval": interval,
            "frequency": frequency,
            "start": _timestamp(start),
            "end": _timestamp(end),
        }
    )


def _catalog_optional(database: Path | None) -> DuckDBCatalog | None:
    return DuckDBCatalog(database) if database is not None else None


def _show_fetch_results(results) -> None:
    typer.echo(f"objects={len(results)}")
    for result in results:
        catalog_state = (
            "not_requested"
            if result.catalog_inserted is None
            else ("inserted" if result.catalog_inserted else "already_registered")
        )
        typer.echo(
            f"{result.status.value} object_id={result.object_id} "
            f"bytes={result.byte_size} catalog={catalog_state}"
        )


@data_app.command("archive-plan")
def archive_plan(
    dataset: Annotated[str, typer.Argument(help="bars, mark_bars, or funding.")],
    symbol: Annotated[str, typer.Argument(help="USD-M symbol, e.g. BTCUSDT.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    interval: Annotated[str | None, typer.Option(help="Required for bar data.")] = None,
    frequency: Annotated[str, typer.Option(help="monthly or daily.")] = "monthly",
) -> None:
    """Print deterministic archive candidates without network access."""

    request = _source_or_exit(
        lambda: _archive_request(dataset, symbol, interval, frequency, start, end)
    )
    objects = archive_candidates(request)
    typer.echo(f"candidates={len(objects)}")
    for item in objects:
        typer.echo(item.url)


@data_app.command("archive-coverage")
def archive_coverage(
    dataset: Annotated[str, typer.Argument(help="bars, mark_bars, or funding.")],
    symbol: Annotated[str, typer.Argument(help="USD-M symbol, e.g. BTCUSDT.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    raw_root: Annotated[Path, typer.Option(help="Isolated raw data root.")],
    manifest_root: Annotated[Path, typer.Option(help="Raw manifest directory.")],
    interval: Annotated[str | None, typer.Option(help="Required for bar data.")] = None,
    frequency: Annotated[str, typer.Option(help="monthly or daily.")] = "monthly",
) -> None:
    """Report verified, missing, partial, and conflicting local archives."""

    def operation():
        request = _archive_request(dataset, symbol, interval, frequency, start, end)
        return local_archive_coverage(
            request,
            raw_root=raw_root,
            manifest_root=manifest_root,
        )

    items = _source_or_exit(operation)
    typer.echo(f"candidates={len(items)}")
    for item in items:
        typer.echo(f"{item.remote.period} status={item.status.value} path={item.path}")


@data_app.command("archive-sync")
def archive_sync(
    dataset: Annotated[str, typer.Argument(help="bars, mark_bars, or funding.")],
    symbol: Annotated[str, typer.Argument(help="USD-M symbol, e.g. BTCUSDT.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    raw_root: Annotated[Path, typer.Option(help="Isolated raw data root.")],
    manifest_root: Annotated[Path, typer.Option(help="Raw manifest directory.")],
    interval: Annotated[str | None, typer.Option(help="Required for bar data.")] = None,
    frequency: Annotated[str, typer.Option(help="monthly or daily.")] = "monthly",
    database: Annotated[Path | None, typer.Option(help="Initialized Catalog.")] = None,
    timeout: Annotated[float, typer.Option(min=0.1, max=300)] = 20.0,
    retries: Annotated[int, typer.Option(min=0, max=20)] = 4,
    workers: Annotated[int, typer.Option(min=1, max=64)] = 4,
) -> None:
    """Probe, checksum-verify, atomically download, and register archives."""

    def operation():
        request = _archive_request(dataset, symbol, interval, frequency, start, end)
        with PublicHttpClient(
            timeout_seconds=timeout,
            retry_policy=RetryPolicy(max_retries=retries),
        ) as http:
            return ArchiveIngestService(BinanceArchiveSource(http)).sync(
                request,
                raw_root=raw_root,
                manifest_root=manifest_root,
                catalog=_catalog_optional(database),
                max_workers=workers,
            )

    _show_fetch_results(_source_or_exit(operation))


@data_app.command("rest-klines")
def rest_klines(
    dataset: Annotated[str, typer.Argument(help="bars or mark_bars.")],
    symbol: Annotated[str, typer.Argument(help="USD-M symbol.")],
    interval: Annotated[str, typer.Argument(help="Kline interval.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    raw_root: Annotated[Path, typer.Option(help="Isolated raw data root.")],
    manifest_root: Annotated[Path, typer.Option(help="Raw manifest directory.")],
    database: Annotated[Path | None, typer.Option(help="Initialized Catalog.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1500)] = 1500,
) -> None:
    """Fetch and publish paginated public trade or mark-price klines."""

    def operation():
        with PublicHttpClient() as http:
            source = BinanceRestSource(http)
            if dataset not in {"bars", "mark_bars"}:
                raise ValueError("dataset must be bars or mark_bars")
            pages = source.kline_pages(
                dataset_name=dataset,
                symbol=symbol,
                interval=interval,
                start=_timestamp(start),
                end=_timestamp(end),
                limit=limit,
            )
            return RawRestStore().publish_all(
                pages,
                raw_root=raw_root,
                manifest_root=manifest_root,
                catalog=_catalog_optional(database),
            )

    _show_fetch_results(_source_or_exit(operation))


@data_app.command("rest-funding")
def rest_funding(
    symbol: Annotated[str, typer.Argument(help="USD-M symbol.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    raw_root: Annotated[Path, typer.Option(help="Isolated raw data root.")],
    manifest_root: Annotated[Path, typer.Option(help="Raw manifest directory.")],
    database: Annotated[Path | None, typer.Option(help="Initialized Catalog.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 1000,
) -> None:
    """Fetch and publish paginated public funding-rate history."""

    def operation():
        with PublicHttpClient() as http:
            pages = BinanceRestSource(http).funding_pages(
                symbol=symbol,
                start=_timestamp(start),
                end=_timestamp(end),
                limit=limit,
            )
            return RawRestStore().publish_all(
                pages,
                raw_root=raw_root,
                manifest_root=manifest_root,
                catalog=_catalog_optional(database),
            )

    _show_fetch_results(_source_or_exit(operation))


@data_app.command("snapshot")
def public_snapshot(
    kind: Annotated[str, typer.Argument(help="exchange-info or funding-info.")],
    raw_root: Annotated[Path, typer.Option(help="Isolated raw data root.")],
    manifest_root: Annotated[Path, typer.Option(help="Raw manifest directory.")],
    database: Annotated[Path | None, typer.Option(help="Initialized Catalog.")] = None,
) -> None:
    """Capture one point-in-time public metadata response."""

    def operation():
        with PublicHttpClient() as http:
            source = BinanceRestSource(http)
            if kind == "exchange-info":
                page = source.exchange_info()
            elif kind == "funding-info":
                page = source.funding_info()
            else:
                raise ValueError("kind must be exchange-info or funding-info")
            result = RawRestStore().publish(
                page,
                raw_root=raw_root,
                manifest_root=manifest_root,
                catalog=_catalog_optional(database),
            )
            return (result,)

    _show_fetch_results(_source_or_exit(operation))


@data_app.command("normalize")
def normalize_raw_objects(
    dataset: Annotated[str, typer.Argument(help="bars, mark_bars, funding, or contracts.")],
    raw_manifests: Annotated[
        list[Path], typer.Argument(help="One or more A04 Raw manifest JSON files.")
    ],
    raw_root: Annotated[Path, typer.Option(help="A04 Raw data root.")],
    normalized_root: Annotated[Path, typer.Option(help="Normalized Parquet root.")],
    partition_manifest_root: Annotated[
        Path, typer.Option(help="Partition manifest directory.")
    ],
    quality_root: Annotated[Path, typer.Option(help="Quality report directory.")],
    database: DatabaseOption,
    max_missing_ratio: Annotated[float, typer.Option(min=0.0, max=1.0)] = 0.01,
    compression: Annotated[str, typer.Option(help="Parquet compression codec.")] = "zstd",
    row_group_rows: Annotated[int, typer.Option(min=1)] = 262_144,
) -> None:
    """Normalize verified Raw objects and atomically publish one Parquet part."""

    def operation():
        if dataset not in {"bars", "mark_bars", "funding", "contracts"}:
            raise ValueError("dataset must be bars, mark_bars, funding, or contracts")
        return NormalizationService().run(
            dataset,
            tuple(raw_manifests),
            raw_root=raw_root,
            normalized_root=normalized_root,
            partition_manifest_root=partition_manifest_root,
            quality_root=quality_root,
            catalog=DuckDBCatalog(database),
            policy=QualityPolicy(max_missing_ratio=max_missing_ratio),
            compression=compression,
            row_group_rows=row_group_rows,
        )

    result = _source_or_exit(operation)
    catalog_state = "inserted" if result.catalog_inserted else "already_registered"
    action = "published" if result.published else "already_published"
    typer.echo(f"publication={action}")
    typer.echo(f"dataset={result.partition_manifest.dataset_name}")
    typer.echo(f"dataset_version={result.partition_manifest.dataset_version}")
    typer.echo(f"partition_id={result.partition_manifest.partition_id}")
    typer.echo(f"rows={result.partition_manifest.row_count}")
    typer.echo(f"quality={result.quality_report.status}")
    typer.echo(f"catalog={catalog_state}")
    typer.echo(f"parquet={result.parquet_path}")


@data_app.command("normalized-scan")
def scan_normalized(
    dataset: Annotated[str, typer.Argument(help="bars, mark_bars, funding, or contracts.")],
    dataset_version: Annotated[str, typer.Argument(help="Exact normalized version.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    normalized_root: Annotated[Path, typer.Option(help="Normalized Parquet root.")],
    database: DatabaseOption,
    interval: Annotated[
        str | None, typer.Option(help="Required for bars and mark_bars.")
    ] = None,
    columns: Annotated[
        str | None, typer.Option(help="Comma-separated projection columns.")
    ] = None,
    symbols: Annotated[
        str | None, typer.Option(help="Comma-separated symbol filter.")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 20,
    verify_hashes: Annotated[
        bool, typer.Option("--verify-hashes/--no-verify-hashes")
    ] = False,
) -> None:
    """Collect a bounded preview from a lazy, version-pinned Polars scan."""

    def operation():
        store = ParquetDataStore(
            normalized_root=normalized_root,
            catalog=DuckDBCatalog(database),
            verify_hashes=verify_hashes,
        )
        selected_columns = (
            tuple(item.strip() for item in columns.split(",") if item.strip())
            if columns is not None
            else None
        )
        selected_symbols = (
            tuple(item.strip() for item in symbols.split(",") if item.strip())
            if symbols is not None
            else None
        )
        common = {
            "dataset_version": dataset_version,
            "start": _timestamp(start),
            "end": _timestamp(end),
            "columns": selected_columns,
            "symbols": selected_symbols,
        }
        if dataset == "bars":
            if interval is None:
                raise ValueError("bars scan requires --interval")
            frame = store.scan_bars(interval=interval, **common)
        elif dataset == "mark_bars":
            if interval is None:
                raise ValueError("mark_bars scan requires --interval")
            frame = store.scan_mark_bars(interval=interval, **common)
        elif dataset == "funding":
            if interval is not None:
                raise ValueError("funding scan does not accept --interval")
            frame = store.scan_funding(**common)
        elif dataset == "contracts":
            if interval is not None:
                raise ValueError("contracts scan does not accept --interval")
            frame = store.scan_contracts(**common)
        else:
            raise ValueError("dataset must be bars, mark_bars, funding, or contracts")
        return frame.head(limit).collect()

    result = _source_or_exit(operation)
    typer.echo(f"rows={result.height}")
    typer.echo(result.write_json())


@data_app.command("resample-preview")
def resample_preview(
    dataset: Annotated[str, typer.Argument(help="bars or mark_bars.")],
    dataset_version: Annotated[str, typer.Argument(help="Exact source version.")],
    start: Annotated[str, typer.Argument(help="Inclusive UTC timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC timestamp.")],
    source_interval: Annotated[str, typer.Option(help="Stored bar interval.")],
    target_interval: Annotated[str, typer.Option(help="Larger derived interval.")],
    normalized_root: Annotated[Path, typer.Option(help="Normalized Parquet root.")],
    database: DatabaseOption,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 20,
    verify_hashes: Annotated[
        bool, typer.Option("--verify-hashes/--no-verify-hashes")
    ] = False,
) -> None:
    """Collect a bounded preview of deterministic UTC-aligned resampling."""

    def operation():
        store = ParquetDataStore(
            normalized_root=normalized_root,
            catalog=DuckDBCatalog(database),
            verify_hashes=verify_hashes,
        )
        common = {
            "dataset_version": dataset_version,
            "start": _timestamp(start),
            "end": _timestamp(end),
            "interval": source_interval,
        }
        if dataset == "bars":
            source = store.scan_bars(**common)
        elif dataset == "mark_bars":
            source = store.scan_mark_bars(**common)
        else:
            raise ValueError("dataset must be bars or mark_bars")
        result = resample_bars(
            source,
            dataset_name=dataset,
            source_interval=source_interval,
            target_interval=target_interval,
            source_dataset_version=dataset_version,
        )
        return result, result.frame.head(limit).collect()

    resampled, preview = _source_or_exit(operation)
    typer.echo(f"dataset_version={resampled.dataset_version}")
    typer.echo(f"expected_source_bars={resampled.expected_source_bars}")
    typer.echo(f"rows={preview.height}")
    typer.echo(preview.write_json())


@universe_app.command("preview")
def universe_preview(
    bars_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized bars version.")
    ],
    contracts_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized contracts version.")
    ],
    start: Annotated[str, typer.Argument(help="First universe timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive universe timestamp.")],
    history_start: Annotated[
        str, typer.Option(help="Inclusive bar history/rolling overlap start.")
    ],
    base_interval: Annotated[str, typer.Option(help="Input bars interval.")],
    normalized_root: Annotated[Path, typer.Option(help="Normalized Parquet root.")],
    database: DatabaseOption,
    config: Annotated[
        Path, typer.Option(help="Universe YAML configuration.")
    ] = project_root() / "configs" / "universe.yaml",
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 20,
    verify_hashes: Annotated[
        bool, typer.Option("--verify-hashes/--no-verify-hashes")
    ] = False,
) -> None:
    """Collect an audited point-in-time universe preview."""

    def operation():
        run_start, run_end = _timestamp(start), _timestamp(end)
        loaded_from = _timestamp(history_start)
        if loaded_from > run_start:
            raise ValueError("history_start must not be later than start")
        loaded = yaml.safe_load(config.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("universe config must be a YAML mapping")
        universe_config = UniverseConfig.model_validate(loaded)
        store = ParquetDataStore(
            normalized_root=normalized_root,
            catalog=DuckDBCatalog(database),
            verify_hashes=verify_hashes,
        )
        bars = store.scan_bars(
            dataset_version=bars_dataset_version,
            start=loaded_from,
            end=run_end,
            interval=base_interval,
        )
        contracts = store.scan_contracts(
            dataset_version=contracts_dataset_version,
            start=_timestamp("1970-01-01T00:00:00Z"),
            end=run_end,
        )
        schedule = build_schedule(
            start=run_start,
            end=run_end,
            interval=universe_config.schedule.interval,
        )
        result = build_point_in_time_universe(
            bars,
            contracts,
            schedule,
            config=universe_config,
            base_interval=base_interval,
            bars_dataset_version=bars_dataset_version,
            contracts_dataset_version=contracts_dataset_version,
        )
        return result, result.frame.head(limit).collect()

    universe, preview = _source_or_exit(operation)
    typer.echo(f"universe_version={universe.universe_version}")
    typer.echo(f"rows={preview.height}")
    typer.echo(preview.write_json())


@research_app.command("list-factors")
def research_list_factors() -> None:
    """List the built-in name/version and required bar columns."""

    for factor in list_factors():
        typer.echo(
            f"{factor.name} {factor.version} "
            f"columns={','.join(factor.required_columns)}"
        )


@research_app.command("preview")
def research_preview(
    bars_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized bars version.")
    ],
    contracts_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized contracts version.")
    ],
    factor_name: Annotated[str, typer.Argument(help="Configured factor name.")],
    label_name: Annotated[str, typer.Argument(help="Configured label name.")],
    start: Annotated[str, typer.Argument(help="First signal timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive signal timestamp.")],
    history_start: Annotated[
        str, typer.Option(help="Inclusive factor/universe history start.")
    ],
    future_end: Annotated[
        str, typer.Option(help="Exclusive bar end covering all label exits.")
    ],
    base_interval: Annotated[str, typer.Option(help="Input bars interval.")],
    normalized_root: Annotated[Path, typer.Option(help="Normalized Parquet root.")],
    database: DatabaseOption,
    universe_config_path: Annotated[
        Path, typer.Option("--universe-config", help="Universe YAML configuration.")
    ] = project_root() / "configs" / "universe.yaml",
    factor_config_path: Annotated[
        Path, typer.Option("--factor-config", help="Factor YAML configuration.")
    ] = project_root() / "configs" / "factor.yaml",
    quantiles: Annotated[int, typer.Option(min=2, max=20)] = 5,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 20,
    verify_hashes: Annotated[
        bool, typer.Option("--verify-hashes/--no-verify-hashes")
    ] = False,
) -> None:
    """Collect bounded factor, label, and diagnostic previews."""

    def operation():
        run_start, run_end = _timestamp(start), _timestamp(end)
        loaded_from = _timestamp(history_start)
        loaded_until = _timestamp(future_end)
        if loaded_from > run_start:
            raise ValueError("history_start must not be later than start")
        if loaded_until <= run_end:
            raise ValueError("future_end must be later than end")
        universe_loaded = yaml.safe_load(
            universe_config_path.read_text(encoding="utf-8")
        )
        factor_loaded = yaml.safe_load(
            factor_config_path.read_text(encoding="utf-8")
        )
        if not isinstance(universe_loaded, dict):
            raise ValueError("universe config must be a YAML mapping")
        if not isinstance(factor_loaded, dict):
            raise ValueError("factor config must be a YAML mapping")
        universe_config = UniverseConfig.model_validate(universe_loaded)
        factor_config = FactorConfig.model_validate(factor_loaded)
        try:
            factor_definition = next(
                item for item in factor_config.factors if item.name == factor_name
            )
        except StopIteration as exc:
            raise ValueError(f"factor is not configured: {factor_name}") from exc
        try:
            label_definition = next(
                item for item in factor_config.labels if item.name == label_name
            )
        except StopIteration as exc:
            raise ValueError(f"label is not configured: {label_name}") from exc
        store = ParquetDataStore(
            normalized_root=normalized_root,
            catalog=DuckDBCatalog(database),
            verify_hashes=verify_hashes,
        )
        bars = store.scan_bars(
            dataset_version=bars_dataset_version,
            start=loaded_from,
            end=loaded_until,
            interval=base_interval,
        )
        universe_bars = store.scan_bars(
            dataset_version=bars_dataset_version,
            start=loaded_from,
            end=run_end,
            interval=base_interval,
        )
        contracts = store.scan_contracts(
            dataset_version=contracts_dataset_version,
            start=_timestamp("1970-01-01T00:00:00Z"),
            end=run_end,
        )
        schedule = build_schedule(
            start=run_start,
            end=run_end,
            interval=universe_config.schedule.interval,
        )
        universe = build_point_in_time_universe(
            universe_bars,
            contracts,
            schedule,
            config=universe_config,
            base_interval=base_interval,
            bars_dataset_version=bars_dataset_version,
            contracts_dataset_version=contracts_dataset_version,
        )
        factor = compute_factor(
            bars,
            universe.frame,
            factor_definition,
            base_interval=base_interval,
            bars_dataset_version=bars_dataset_version,
            universe_version=universe.universe_version,
        )
        label = compute_forward_returns(
            bars,
            universe.frame,
            label_definition,
            base_interval=base_interval,
            bars_dataset_version=bars_dataset_version,
            universe_version=universe.universe_version,
        )
        evaluation = evaluate_factor(
            factor.frame,
            label.frame,
            universe.frame,
            universe_version=universe.universe_version,
            quantiles=quantiles,
        )
        return {
            "factor_version": factor.factor_version,
            "label_version": label.label_version,
            "factor": factor.frame.head(limit).collect(),
            "label": label.frame.head(limit).collect(),
            "ic": evaluation.ic.head(limit).collect(),
            "quantiles": evaluation.quantile_returns.head(limit).collect(),
            "coverage": evaluation.coverage.head(limit).collect(),
            "turnover": evaluation.turnover.head(limit).collect(),
        }

    result = _source_or_exit(operation)
    typer.echo(f"factor_version={result['factor_version']}")
    typer.echo(f"label_version={result['label_version']}")
    for name in (
        "factor",
        "label",
        "ic",
        "quantiles",
        "coverage",
        "turnover",
    ):
        frame = result[name]
        typer.echo(f"{name}_rows={frame.height}")
        typer.echo(f"{name}={frame.write_json()}")


@backtest_app.command("preview")
def backtest_preview(
    bars_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized trade-bars version.")
    ],
    mark_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized mark-bars version.")
    ],
    funding_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized funding version.")
    ],
    contracts_dataset_version: Annotated[
        str, typer.Argument(help="Exact normalized contracts version.")
    ],
    factor_name: Annotated[str, typer.Argument(help="Configured factor name.")],
    start: Annotated[str, typer.Argument(help="First signal timestamp.")],
    end: Annotated[str, typer.Argument(help="Exclusive signal timestamp.")],
    history_start: Annotated[
        str, typer.Option(help="Inclusive factor/universe history start.")
    ],
    future_end: Annotated[
        str, typer.Option(help="Exclusive market-data end after final fill.")
    ],
    base_interval: Annotated[str, typer.Option(help="Input bars interval.")],
    normalized_root: Annotated[Path, typer.Option(help="Normalized Parquet root.")],
    database: DatabaseOption,
    universe_config_path: Annotated[
        Path, typer.Option("--universe-config", help="Universe YAML configuration.")
    ] = project_root() / "configs" / "universe.yaml",
    factor_config_path: Annotated[
        Path, typer.Option("--factor-config", help="Factor YAML configuration.")
    ] = project_root() / "configs" / "factor.yaml",
    backtest_config_path: Annotated[
        Path, typer.Option("--backtest-config", help="Backtest YAML configuration.")
    ] = project_root() / "configs" / "backtest.yaml",
    initial_equity: Annotated[float, typer.Option(min=0.000000001)] = 1.0,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 20,
    verify_hashes: Annotated[
        bool, typer.Option("--verify-hashes/--no-verify-hashes")
    ] = False,
) -> None:
    """Run an offline, version-pinned portfolio and ledger preview."""

    def operation():
        run_start, run_end = _timestamp(start), _timestamp(end)
        loaded_from = _timestamp(history_start)
        loaded_until = _timestamp(future_end)
        if loaded_from > run_start:
            raise ValueError("history_start must not be later than start")
        if loaded_until <= run_end:
            raise ValueError("future_end must be later than end")
        loaded_configs = []
        for path, name in (
            (universe_config_path, "universe"),
            (factor_config_path, "factor"),
            (backtest_config_path, "backtest"),
        ):
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError(f"{name} config must be a YAML mapping")
            loaded_configs.append(value)
        universe_config = UniverseConfig.model_validate(loaded_configs[0])
        factor_config = FactorConfig.model_validate(loaded_configs[1])
        backtest_config = BacktestConfig.model_validate(loaded_configs[2])
        backtest_config.assert_execution_supported()
        try:
            factor_definition = next(
                item for item in factor_config.factors if item.name == factor_name
            )
        except StopIteration as exc:
            raise ValueError(f"factor is not configured: {factor_name}") from exc
        store = ParquetDataStore(
            normalized_root=normalized_root,
            catalog=DuckDBCatalog(database),
            verify_hashes=verify_hashes,
        )
        bars = store.scan_bars(
            dataset_version=bars_dataset_version,
            start=loaded_from,
            end=loaded_until,
            interval=base_interval,
        )
        contracts = store.scan_contracts(
            dataset_version=contracts_dataset_version,
            start=_timestamp("1970-01-01T00:00:00Z"),
            end=run_end,
        )
        universe_schedule = build_schedule(
            start=run_start,
            end=run_end,
            interval=universe_config.schedule.interval,
        )
        universe = build_point_in_time_universe(
            bars.filter(pl.col("open_time") < run_end),
            contracts,
            universe_schedule,
            config=universe_config,
            base_interval=base_interval,
            bars_dataset_version=bars_dataset_version,
            contracts_dataset_version=contracts_dataset_version,
        )
        factor = compute_factor(
            bars,
            universe.frame,
            factor_definition,
            base_interval=base_interval,
            bars_dataset_version=bars_dataset_version,
            universe_version=universe.universe_version,
        )
        rebalance_schedule = build_schedule(
            start=run_start,
            end=run_end,
            interval=backtest_config.schedule.rebalance_interval,
        )
        rebalance_scores = factor.frame.join(
            rebalance_schedule.select("timestamp"),
            on="timestamp",
            how="inner",
        )
        portfolio = construct_portfolio(
            rebalance_scores,
            backtest_config.portfolio,
            factor_version=factor.factor_version,
            universe_version=universe.universe_version,
        )
        execution_bars = bars.filter(pl.col("open_time") >= run_start)
        mark_bars = store.scan_mark_bars(
            dataset_version=mark_dataset_version,
            start=run_start,
            end=loaded_until,
            interval=base_interval,
        )
        funding = store.scan_funding(
            dataset_version=funding_dataset_version,
            start=run_start,
            end=loaded_until,
        )
        return run_vectorized_backtest(
            portfolio.frame,
            execution_bars,
            mark_bars,
            funding,
            config=backtest_config,
            base_interval=base_interval,
            portfolio_version=portfolio.portfolio_version,
            bars_dataset_version=bars_dataset_version,
            mark_dataset_version=mark_dataset_version,
            funding_dataset_version=funding_dataset_version,
            initial_equity=initial_equity,
        )

    result = _source_or_exit(operation)
    typer.echo(f"run_id={result.run_id}")
    typer.echo(f"result_hash={result.result_hash}")
    typer.echo(f"warnings={len(result.warnings)}")
    for name in ("targets", "trades", "positions", "costs", "returns"):
        frame = getattr(result, name).head(limit).collect()
        typer.echo(f"{name}_rows={frame.height}")
        typer.echo(f"{name}={frame.write_json()}")


@app.command("run")
def formal_run(
    dataset_id: Annotated[
        str, typer.Argument(help="Exact registered DatasetSnapshot ID.")
    ],
    dataset_version: Annotated[
        str, typer.Argument(help="Exact registered DatasetSnapshot version.")
    ],
    factor_name: Annotated[str, typer.Argument(help="Configured factor name.")],
    database: DatabaseOption,
    data_config: Annotated[
        Path | None, typer.Option("--data-config", help="Data YAML path.")
    ] = None,
    universe_config: Annotated[
        Path | None, typer.Option("--universe-config", help="Universe YAML path.")
    ] = None,
    factor_config: Annotated[
        Path | None, typer.Option("--factor-config", help="Factor YAML path.")
    ] = None,
    backtest_config: Annotated[
        Path | None, typer.Option("--backtest-config", help="Backtest YAML path.")
    ] = None,
    verify_hashes: Annotated[
        bool, typer.Option("--verify-hashes/--no-verify-hashes")
    ] = True,
) -> None:
    """Execute and atomically publish one formal version-pinned run."""

    config = _load_config_or_exit(
        _paths(data_config, universe_config, factor_config, backtest_config),
        True,
    )
    catalog = DuckDBCatalog(database)
    snapshot = _catalog_or_exit(
        lambda: catalog.resolve_dataset(dataset_id, dataset_version)
    )
    try:
        published = execute_formal_run(
            config,
            snapshot,
            factor_name=factor_name,
            catalog=catalog,
            project_root=project_root(),
            verify_hashes=verify_hashes,
        )
    except RunExecutionError as exc:
        typer.echo(f"Run failed: {exc}", err=True)
        if exc.failed_run is not None:
            typer.echo(f"run_id={exc.failed_run.manifest.run_id}", err=True)
            typer.echo(f"status=failed", err=True)
            typer.echo(f"run_path={exc.failed_run.path}", err=True)
        raise typer.Exit(code=1) from exc
    except (ArtifactStoreError, EnvironmentError, ValueError) as exc:
        typer.echo(f"Run preflight error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"run_id={published.manifest.run_id}")
    typer.echo(f"status={published.manifest.status}")
    typer.echo(f"run_path={published.path}")
    typer.echo(f"manifest_sha256={manifest_sha256(published.manifest)}")
    typer.echo(
        "publication="
        + ("already_published" if published.already_published else "published")
    )
    registration = published.catalog_registration
    typer.echo(
        "catalog="
        + (
            "not_configured"
            if registration is None
            else ("inserted" if registration.inserted else "already_registered")
        )
    )


@app.command("report")
def rebuild_report(
    run_id: Annotated[str, typer.Argument(help="Published terminal run ID.")],
    output_root: Annotated[Path, typer.Option(help="Root containing run directories.")],
    output: Annotated[Path, typer.Option(help="New HTML path outside the run directory.")],
) -> None:
    """Verify a succeeded run and rebuild HTML without rerunning the backtest."""

    try:
        root = output_root.resolve()
        run_directory = (root / run_id).resolve()
        try:
            run_directory.relative_to(root)
        except ValueError as exc:
            raise ValueError("run_id must resolve below output_root") from exc
        destination = output.resolve()
        try:
            destination.relative_to(run_directory)
        except ValueError:
            pass
        else:
            raise ValueError("rebuilt report output must be outside the immutable run")
        manifest = load_manifest_auto(run_directory / "manifest.json")
        if not isinstance(manifest, RunManifest):
            raise ValueError(
                "reports can only be rebuilt from run/v1 or run/v2 manifests"
            )
        if manifest.run_id != run_id:
            raise ValueError("manifest run_id does not match the requested directory")
        if manifest.status != "succeeded":
            raise ValueError("reports can only be rebuilt from succeeded runs")
        RunArtifactStore.verify(run_directory, manifest)
        rendered = render_report_from_artifacts(
            run_directory,
            output_path=destination,
        )
    except (
        ArtifactStoreError,
        ManifestLoadError,
        OSError,
        ReportError,
        ValidationError,
        ValueError,
    ) as exc:
        typer.echo(f"Report error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"run_id={run_id}")
    typer.echo(f"report={rendered}")


@performance_app.command("plan")
def performance_plan(
    start: Annotated[str, typer.Argument(help="Inclusive UTC core start.")],
    end: Annotated[str, typer.Argument(help="Exclusive UTC core end.")],
    chunk_interval: Annotated[
        str, typer.Option(help="Duration of each bounded core chunk.")
    ] = "2d",
    overlap_seconds: Annotated[
        int, typer.Option(min=0, help="Backward history overlap in seconds.")
    ] = 0,
) -> None:
    """Print deterministic left-closed/right-open chunk boundaries."""

    try:
        chunks = plan_time_chunks(
            start=_timestamp(start),
            end=_timestamp(end),
            chunk_interval=chunk_interval,
            overlap_seconds=overlap_seconds,
        )
    except ValueError as exc:
        typer.echo(f"Performance plan error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"chunks={len(chunks)}")
    for chunk in chunks:
        typer.echo(
            f"{chunk.ordinal} "
            f"input_start={chunk.input_start.isoformat()} "
            f"start={chunk.start.isoformat()} end={chunk.end.isoformat()}"
        )


@performance_app.command("inspect")
def performance_inspect(
    run_id: Annotated[str, typer.Argument(help="Published succeeded run ID.")],
    output_root: Annotated[
        Path, typer.Option(help="Root containing immutable run directories.")
    ],
) -> None:
    """Verify and print one published deterministic performance artifact."""

    try:
        root = output_root.resolve()
        run_directory = (root / run_id).resolve()
        try:
            run_directory.relative_to(root)
        except ValueError as exc:
            raise ValueError("run_id must resolve below output_root") from exc
        manifest = load_manifest_auto(run_directory / "manifest.json")
        if not isinstance(manifest, RunManifest):
            raise ValueError("performance inspection requires a run manifest")
        if manifest.run_id != run_id or manifest.status != "succeeded":
            raise ValueError("performance inspection requires the requested succeeded run")
        RunArtifactStore.verify(run_directory, manifest)
        path = run_directory / "performance.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("performance.json must contain an object")
    except (
        ArtifactStoreError,
        ManifestLoadError,
        OSError,
        ValidationError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        typer.echo(f"Performance inspect error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


@performance_app.command("clean-work")
def performance_clean_work(
    output_root: Annotated[
        Path, typer.Option(help="Root whose .work/a10-* children may be cleaned.")
    ],
    older_than_hours: Annotated[
        int, typer.Option(min=0, help="Minimum dead-workspace age in hours.")
    ] = 24,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Delete listed dead workspaces or only print them.",
        ),
    ] = False,
) -> None:
    """Safely list or remove stale A10 temp workspaces, never published runs."""

    try:
        stale = cleanup_stale_workspaces(
            output_root,
            older_than_seconds=older_than_hours * 60 * 60,
            apply=apply,
        )
    except SpoolError as exc:
        typer.echo(f"Performance cleanup error:\n{exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"mode={'apply' if apply else 'dry_run'}")
    typer.echo(f"stale_workspaces={len(stale)}")
    for path in stale:
        typer.echo(str(path))
