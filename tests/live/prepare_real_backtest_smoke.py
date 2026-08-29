"""Prepare a small, real Binance USD-M dataset for an end-to-end backtest.

The input Raw objects were downloaded and checksum-verified by the A10 live
archive workflow.  This script deliberately selects one month and eight liquid
perpetuals so logical correctness can be tested without a capacity workload.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.hashing import content_sha256
from bianbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    RawObjectManifest,
    load_manifest,
    manifest_json,
)
from bianbt.data.normalize import NORMALIZER_CODE_VERSION
from bianbt.data.normalize.service import NormalizationService
from bianbt.data.schemas import get_schema_definition
from bianbt.data.validation.reports import QualityPolicy

UTC = timezone.utc
SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "LINKUSDT",
)


def _load(path: Path) -> RawObjectManifest:
    value = load_manifest(path, "raw")
    if not isinstance(value, RawObjectManifest):
        raise TypeError(f"not a Raw manifest: {path}")
    return value


def _complete_months(source: Path) -> tuple[str, ...]:
    root = source / "manifests" / "raw"
    coverage: dict[str, set[tuple[str, str]]] = {}
    for path in sorted(root.glob("archive-*-monthly-*.json")):
        manifest = _load(path)
        if (
            manifest.dataset_name not in {"bars", "mark_bars", "funding"}
            or manifest.symbol not in SYMBOLS
            or manifest.available_from is None
        ):
            continue
        month = manifest.available_from.strftime("%Y-%m")
        coverage.setdefault(month, set()).add(
            (manifest.dataset_name, manifest.symbol)
        )
    expected = {
        (dataset, symbol)
        for dataset in ("bars", "mark_bars", "funding")
        for symbol in SYMBOLS
    }
    return tuple(
        sorted(month for month, values in coverage.items() if values == expected)
    )


def _choose_month(source: Path, requested: str | None) -> str:
    complete = _complete_months(source)
    if requested is not None:
        try:
            datetime.strptime(requested, "%Y-%m")
        except ValueError as exc:
            raise ValueError("--month must use YYYY-MM") from exc
        if requested not in complete:
            raise ValueError(
                f"requested month is incomplete: {requested}; complete={list(complete)}"
            )
        return requested
    if len(complete) == 1:
        return complete[0]
    if "2025-01" in complete:
        return "2025-01"
    raise ValueError(
        "cannot choose one complete month automatically; "
        f"use --month YYYY-MM; complete={list(complete)}"
    )


def _select(source: Path, month: str) -> dict[str, tuple[Path, ...]]:
    root = source / "manifests" / "raw"
    selected: dict[str, tuple[Path, ...]] = {}
    for dataset in ("bars", "mark_bars", "funding"):
        paths = tuple(
            root / (
                f"archive-{dataset}-{symbol}"
                + ("-1m" if dataset != "funding" else "")
                + f"-monthly-{month}.json"
            )
            for symbol in SYMBOLS
        )
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"missing real Raw manifests: {missing}")
        selected[dataset] = paths
    contract_paths = sorted(root.glob("rest-contracts-exchangeInfo-*.json"))
    if not contract_paths:
        raise FileNotFoundError("missing real exchangeInfo Raw manifest")
    selected["contracts"] = (contract_paths[-1],)
    return selected


def _write_configs(
    root: Path,
    snapshot: DatasetSnapshotManifest,
    run_start: datetime,
    run_end: datetime,
    *,
    config_root: Path,
    runs_root: Path,
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    payloads = {
        "data": {
            "datasets": {
                "bars": {"enabled": True, "base_interval": "1m"},
                "mark_bars": {"enabled": True, "base_interval": "1m"},
                "funding": {"enabled": True},
                "contracts": {"enabled": True},
                "index_bars": {"enabled": False, "base_interval": "1m"},
            },
            "source": {"allow_authenticated_endpoints": False},
            "time": {
                "base_interval": "1m",
                "derived_intervals": ["1h", "4h"],
                "start": run_start.isoformat(),
                "end": run_end.isoformat(),
            },
            "storage": {
                "root": str(root),
                "normalized": str(root / "normalized"),
                "metadata": str(root / "metadata"),
            },
        },
        "universe": {
            "schedule": {"interval": "1h"},
            "point_in_time": {
                "enabled": True,
                "use_contract_snapshots": False,
                "use_first_last_valid_bar": True,
            },
            "filters": {
                "trading_status_only": False,
                "min_listing_age_days": 0,
                "min_history_bars": 1440,
                "rolling_quote_volume": {"window": "24h", "minimum": 0},
                "max_missing_ratio": {"window": "24h", "maximum": 0.01},
                "exclude_symbols": [],
            },
        },
        "factor": {
            "factors": [{
                "name": "momentum",
                "version": "v1",
                "parameters": {"lookback": "24h", "skip_recent": "1h"},
                "compute_interval": "1h",
                "preprocess": [{"name": "rank"}],
            }],
            "labels": [{
                "name": "forward_return_4h",
                "signal_delay_bars": 1,
                "horizon": "4h",
                "entry_field": "open",
                "exit_field": "open",
            }],
            "cache": {"enabled": True},
        },
        "backtest": {
            "run": {
                "name": f"real_usdm_e2e_smoke_{run_start:%Y_%m}",
                "start": run_start.isoformat(),
                "end": run_end.isoformat(),
                "dataset_version": snapshot.dataset_version,
                "random_seed": 42,
            },
            "schedule": {
                "factor_interval": "1h",
                "rebalance_interval": "4h",
                "signal_delay_bars": 1,
            },
            "portfolio": {
                "construction": "long_short_count",
                "long_count": 2,
                "short_count": 2,
                "weighting": "equal",
                "gross_exposure": 1.0,
                "net_exposure": 0.0,
                "max_symbol_weight": None,
                "max_turnover": None,
            },
            "execution": {
                "fill_price": "next_bar_open",
                "partial_fill": False,
                "fee": {"model": "fixed_bps", "taker_bps": 4.0},
                "slippage": {"model": "fixed_bps", "bps": 2.0},
                "funding": {"enabled": True, "missing_policy": "error"},
            },
            "valuation": {"price": "mark_close"},
            "risk": {"leverage": 1.0, "enforce_liquidation": False},
            "output": {
                "root": str(runs_root),
                "save_factor_values": True,
                "save_universe": True,
                "save_positions": True,
                "save_trades": True,
                "save_costs": True,
                "render_html": True,
            },
            "performance": {
                "mode": "chunked",
                "chunk_interval": "1d",
                "max_input_rows_per_chunk": 250000,
                "max_incremental_rss_mib": 512,
                "collect_diagnostics": True,
            },
        },
    }
    for name, payload in payloads.items():
        (config_root / f"{name}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--month", help="Complete archive month in YYYY-MM")
    args = parser.parse_args()
    source = args.source.resolve()
    target = args.target.resolve()
    database = (args.database or target / "catalog.duckdb").resolve()
    config_root = (args.config_root or target / "configs").resolve()
    runs_root = (args.runs_root or target / "runs").resolve()
    month = _choose_month(source, args.month)
    month_start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    run_start = month_start + timedelta(days=7)
    run_end = month_start + timedelta(days=14)
    selected = _select(source, month)
    print(f"selected_month={month}", flush=True)
    print(
        f"run_range=[{run_start.isoformat()}, {run_end.isoformat()})",
        flush=True,
    )

    target.mkdir(parents=True, exist_ok=True)
    catalog = DuckDBCatalog(database)
    catalog.initialize()
    roots = {
        "raw_root": source / "raw",
        "normalized_root": target / "normalized",
        "partition_manifest_root": target / "manifests" / "partitions",
        "quality_root": target / "quality",
    }
    results = {}
    for dataset, paths in selected.items():
        results[dataset] = NormalizationService().run(
            dataset,
            paths,
            **roots,
            catalog=catalog,
            policy=QualityPolicy(max_missing_ratio=1.0),
            row_group_rows=131_072,
        )
        part = results[dataset].partition_manifest
        print(f"normalized={dataset} rows={part.row_count}", flush=True)

    references = []
    for dataset, result in results.items():
        part = result.partition_manifest
        if dataset == "contracts":
            available_from = part.min_time
            available_to = part.max_time + timedelta(milliseconds=1)
        else:
            available_from = part.min_time
            available_to = part.max_time + timedelta(milliseconds=1)
        references.append(DatasetReference(
            dataset_name=dataset,
            dataset_version=part.dataset_version,
            schema_version="v1",
            schema_fingerprint=get_schema_definition(dataset, "v1").fingerprint,
            available_from=available_from,
            available_to=available_to,
            partition_manifest_ids=(part.partition_id,),
            quality_report_ids=(part.quality_report_id,),
        ))

    sources = sorted(
        (_load(path).object_id, _load(path).checksum_sha256)
        for paths in selected.values()
        for path in paths
    )
    snapshot = DatasetSnapshotManifest(
        dataset_id=f"binance-usdm-real-e2e-smoke-{month}",
        dataset_version=f"live-smoke-{content_sha256(sources)[:24]}",
        created_at=max(
            _load(path).retrieved_at
            for paths in selected.values()
            for path in paths
        ),
        datasets=tuple(references),
        source_manifest_hash=content_sha256(sources),
        normalizer_code_version=NORMALIZER_CODE_VERSION,
        normalizer_parameters_hash=content_sha256(
            {
                name: result.partition_manifest.dataset_version
                for name, result in results.items()
            }
        ),
    )
    catalog.register_dataset(snapshot)
    (target / "dataset-snapshot.json").write_text(
        manifest_json(snapshot), encoding="utf-8"
    )
    _write_configs(
        target, snapshot, run_start, run_end,
        config_root=config_root, runs_root=runs_root,
    )
    print(f"dataset_id={snapshot.dataset_id}")
    print(f"dataset_version={snapshot.dataset_version}")
    print(f"database={database}")
    print(f"config_root={config_root}")
    print(f"runs_root={runs_root}")


if __name__ == "__main__":
    main()
