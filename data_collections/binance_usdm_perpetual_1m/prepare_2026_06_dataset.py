"""Prepare the June 2026 immutable market dataset and initial strategy configs."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bianbt.data.catalog import DuckDBCatalog
from bianbt.data.hashing import content_sha256
from bianbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    PartitionManifest,
    RawObjectManifest,
    load_manifest,
    manifest_json,
)
from bianbt.data.normalize import (
    NORMALIZER_CODE_VERSION,
    build_normalization_release,
)
from bianbt.data.normalize.service import NormalizationService
from bianbt.data.schemas import get_schema_definition
from bianbt.data.validation.reports import QualityPolicy

UTC = timezone.utc
HISTORY_START = datetime(2026, 5, 31, tzinfo=UTC)
RUN_START = datetime(2026, 6, 1, tzinfo=UTC)
RUN_END = datetime(2026, 7, 1, tzinfo=UTC)
FUTURE_END = datetime(2026, 7, 2, tzinfo=UTC)
DATASET_ID = "binance-usdm-full-market-rank-descent-2026-06"


def _raw_manifests(root: Path) -> list[tuple[Path, RawObjectManifest]]:
    values: list[tuple[Path, RawObjectManifest]] = []
    for path in sorted((root / "manifests" / "raw").glob("*.json")):
        manifest = load_manifest(path, "raw")
        if not isinstance(manifest, RawObjectManifest):
            raise TypeError(f"not a Raw manifest: {path}")
        values.append((path, manifest))
    return values


def _select(root: Path) -> dict[str, list[tuple[Path, RawObjectManifest]]]:
    values = _raw_manifests(root)
    monthly_june = {
        manifest.symbol
        for _, manifest in values
        if manifest.dataset_name == "bars"
        and manifest.source == "binance_public_archive"
        and manifest.available_from == RUN_START
        and manifest.available_to == RUN_END
        and manifest.symbol is not None
    }
    if not monthly_june:
        raise ValueError("no June 2026 monthly bar archives were discovered")

    selected: dict[str, list[tuple[Path, RawObjectManifest]]] = defaultdict(list)
    for path, manifest in values:
        if manifest.dataset_name == "contracts":
            selected["contracts"].append((path, manifest))
            continue
        if manifest.symbol not in monthly_june:
            continue
        if manifest.dataset_name == "bars":
            if (
                manifest.available_from is not None
                and manifest.available_to is not None
                and manifest.available_from < FUTURE_END
                and manifest.available_to > HISTORY_START
            ):
                selected["bars"].append((path, manifest))
        elif manifest.dataset_name == "funding":
            if (
                manifest.available_from is not None
                and manifest.available_to is not None
                and manifest.available_from < FUTURE_END
                and manifest.available_to > RUN_START
            ):
                selected["funding"].append((path, manifest))

    if selected["contracts"]:
        selected["contracts"] = [
            max(selected["contracts"], key=lambda item: item[1].retrieved_at)
        ]
    required = {"bars", "funding", "contracts"}
    if missing := required - selected.keys():
        raise ValueError(f"missing selected Raw datasets: {sorted(missing)}")
    print(f"eligible_symbols={len(monthly_june)}", flush=True)
    for dataset in ("bars", "funding", "contracts"):
        print(f"raw_{dataset}={len(selected[dataset])}", flush=True)
    return selected


def _batches(
    items: list[tuple[Path, RawObjectManifest]],
    size: int,
) -> list[tuple[Path, ...]]:
    periods: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for path, manifest in items:
        value = manifest.available_from or manifest.retrieved_at
        periods[(value.year, value.month)].append(path)
    return [
        tuple(paths[index : index + size])
        for period in sorted(periods)
        for paths in [sorted(periods[period])]
        for index in range(0, len(paths), size)
    ]


def _resume(
    root: Path,
    dataset: str,
    version: str,
    by_object: dict[str, RawObjectManifest],
    catalog: DuckDBCatalog,
) -> tuple[list[PartitionManifest], set[str]]:
    parts: list[PartitionManifest] = []
    covered: set[str] = set()
    for path in sorted((root / "manifests" / "partitions").glob("*.json")):
        manifest = load_manifest(path, "partition")
        if not isinstance(manifest, PartitionManifest):
            raise TypeError(f"not a Partition manifest: {path}")
        if (
            manifest.dataset_name != dataset
            or manifest.dataset_version != version
        ):
            continue
        sources = set(manifest.source_object_ids)
        if unknown := sources - by_object.keys():
            raise ValueError(f"partition has unknown sources: {sorted(unknown)}")
        if overlap := sources & covered:
            raise ValueError(f"partition sources overlap: {sorted(overlap)}")
        for object_id in sources:
            catalog.register_raw(by_object[object_id])
        catalog.register_partition(manifest)
        covered.update(sources)
        parts.append(manifest)
    if parts:
        print(
            f"resume dataset={dataset} parts={len(parts)} sources={len(covered)}",
            flush=True,
        )
    return parts, covered


def _normalize(
    root: Path,
    dataset: str,
    items: list[tuple[Path, RawObjectManifest]],
    catalog: DuckDBCatalog,
    batch_size: int,
) -> tuple[str, tuple[PartitionManifest, ...]]:
    release = build_normalization_release(
        dataset, (manifest for _, manifest in items)
    )
    by_object = {manifest.object_id: manifest for _, manifest in items}
    parts, covered = _resume(
        root, dataset, release.dataset_version, by_object, catalog
    )
    remaining = [
        item for item in items if item[1].object_id not in covered
    ]
    batches = _batches(remaining, batch_size)
    for index, paths in enumerate(batches, start=1):
        result = NormalizationService().run(
            dataset,
            paths,
            raw_root=root / "raw",
            normalized_root=root / "normalized",
            partition_manifest_root=root / "manifests" / "partitions",
            quality_root=root / "quality",
            catalog=catalog,
            policy=QualityPolicy(max_missing_ratio=1.0),
            compression="zstd",
            row_group_rows=131_072,
            release=release,
        )
        parts.append(result.partition_manifest)
        if index == 1 or index % 25 == 0 or index == len(batches):
            print(
                f"normalize dataset={dataset} batch={index}/{len(batches)} "
                f"rows={result.partition_manifest.row_count}",
                flush=True,
            )
    return release.dataset_version, tuple(parts)


def _reference(
    dataset: str,
    version: str,
    parts: tuple[PartitionManifest, ...],
) -> DatasetReference:
    minimum = min(part.min_time for part in parts if part.min_time is not None)
    maximum = max(part.max_time for part in parts if part.max_time is not None)
    available_from = minimum
    available_to = maximum + timedelta(milliseconds=1)
    if dataset == "bars":
        available_from = min(available_from, HISTORY_START)
        available_to = max(available_to, FUTURE_END)
    elif dataset == "funding":
        available_from = min(available_from, RUN_START)
        available_to = max(available_to, FUTURE_END)
    return DatasetReference(
        dataset_name=dataset,
        dataset_version=version,
        schema_version="v1",
        schema_fingerprint=get_schema_definition(dataset, "v1").fingerprint,
        available_from=available_from,
        available_to=available_to,
        partition_manifest_ids=tuple(part.partition_id for part in parts),
        quality_report_ids=tuple(part.quality_report_id for part in parts),
    )


def _write_configs(
    root: Path,
    snapshot: DatasetSnapshotManifest,
    *,
    config_root: Path,
    runs_root: Path,
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    payloads: dict[str, dict[str, object]] = {
        "data": {
            "market": {
                "venue": "binance",
                "segment": "usd_m_futures",
                "contract_type": "perpetual",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
            },
            "datasets": {
                "bars": {"enabled": True, "base_interval": "1m"},
                "mark_bars": {"enabled": False, "base_interval": "1m"},
                "funding": {"enabled": True},
                "contracts": {"enabled": True},
                "index_bars": {"enabled": False, "base_interval": "1m"},
            },
            "source": {"allow_authenticated_endpoints": False},
            "time": {
                "base_interval": "1m",
                "derived_intervals": [],
                "start": HISTORY_START.isoformat(),
                "end": FUTURE_END.isoformat(),
            },
            "storage": {
                "root": str(root),
                "raw": str(root / "raw"),
                "normalized": str(root / "normalized"),
                "metadata": str(root / "metadata"),
            },
        },
        "universe": {
            "schedule": {"interval": "1m"},
            "market": {
                "venue": "binance",
                "segment": "usd_m_futures",
                "contract_type": "perpetual",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
            },
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
                "max_missing_ratio": {"window": "24h", "maximum": 0.0},
                "exclude_symbols": [],
            },
        },
        "factor": {
            "factors": [
                {
                    "name": "momentum",
                    "version": "v1",
                    "parameters": {"lookback": "24h"},
                    "compute_interval": "1m",
                    "preprocess": [],
                }
            ],
            "labels": [],
            "cache": {"enabled": False},
        },
        "backtest": {
            "config_version": "v2",
            "run": {
                "name": "full_market_rank_descent_momentum_2026_06",
                "start": RUN_START.isoformat(),
                "end": RUN_END.isoformat(),
                "dataset_version": snapshot.dataset_version,
                "random_seed": 42,
            },
            "schedule": {
                "factor_interval": "1m",
                "rebalance_interval": "1m",
                "signal_delay_bars": 1,
            },
            "capital": {
                "currency": "USDT",
                "initial_equity": 10000.0,
                "margin_model": "simple_cross",
                "reserved_cost_buffer": 0.0,
            },
            "portfolio": {
                "selection": {
                    "mode": "rank_descent",
                    "rank_order": "descending",
                    "clock": "factor",
                    "lag": 0,
                    "long": {"ranks": [], "ranges": []},
                    "short": {"ranks": [], "ranges": []},
                    "descent": {
                        "start_rank_at_least": 5,
                        "entry_rank": 1,
                        "equal_policy": "keep",
                        "increase_policy": "reset",
                    },
                    "audit_top_n": 5,
                },
                "sizing": {
                    "mode": "equity_margin_fraction",
                    "fraction": 1.0,
                    "reverse_policy": "net_delta",
                },
                "constraints": {
                    "max_gross_exposure": 5.0,
                    "max_net_exposure": 5.0,
                    "max_symbol_weight": 5.0,
                    "max_symbol_notional": None,
                    "max_consecutive_adds": 1,
                    "max_turnover": 10.0,
                },
                "holding": {
                    "mode": "single_position_replace",
                    "existing_signal": "ignore",
                },
            },
            "execution": {
                "fill_price": "next_bar_open",
                "partial_fill": False,
                "fee": {"model": "fixed_bps", "taker_bps": 4.0},
                "slippage": {"model": "fixed_bps", "bps": 1.0},
                "funding": {
                    "enabled": True,
                    "missing_policy": "assume_zero",
                },
            },
            "valuation": {"price": "trade_close"},
            "risk": {
                "leverage": 5.0,
                "enforce_liquidation": False,
                "evaluation_interval": "1m",
                "trigger_price": "trade",
                "fill_model": "same_bar_trigger",
                "gap_policy": "worse_executable",
                "intrabar_conflict": "worst_case",
                "symbol_exits": {
                    "stop_loss": {
                        "enabled": True,
                        "distance": 0.02,
                        "action": "close",
                    },
                    "take_profit": {
                        "enabled": True,
                        "distance": 0.036,
                        "action": "close",
                    },
                    "trailing_stop": {"enabled": False},
                },
                "portfolio_exits": {
                    "stop_loss": None,
                    "take_profit": None,
                    "max_drawdown": None,
                },
                "cooldown_bars": 0,
                "reentry_policy": "next_scheduled_rebalance",
                "max_triggers_per_symbol": None,
            },
            "output": {
                "root": str(runs_root),
                "save_factor_values": False,
                "save_universe": False,
                "save_positions": True,
                "save_trades": True,
                "save_costs": True,
                "render_html": True,
            },
            "performance": {
                "mode": "chunked",
                "chunk_interval": "1d",
                "max_input_rows_per_chunk": 3000000,
                "max_incremental_rss_mib": 4096,
                "max_process_rss_mib": 5632,
                "collect_diagnostics": True,
                "resume_policy": "resume",
                "max_rank_lag": 0,
                "max_rank_state_rows": 2000,
                "max_position_state_rows": 10,
                "max_pending_instructions": 100,
                "max_risk_state_rows": 2000,
                "max_pending_risk_intents": 100,
            },
        },
    }
    for name, payload in payloads.items():
        (config_root / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--bar-batch-symbols", type=int, default=8)
    parser.add_argument("--funding-batch-symbols", type=int, default=128)
    args = parser.parse_args()

    root = args.root.resolve()
    database = args.database.resolve()
    config_root = args.config_root.resolve()
    runs_root = args.runs_root.resolve()
    catalog = DuckDBCatalog(database)
    catalog.initialize()
    selected = _select(root)

    normalized: dict[str, tuple[str, tuple[PartitionManifest, ...]]] = {}
    for dataset in ("bars", "funding", "contracts"):
        size = (
            args.bar_batch_symbols
            if dataset == "bars"
            else args.funding_batch_symbols
        )
        normalized[dataset] = _normalize(
            root, dataset, selected[dataset], catalog, size
        )

    references = tuple(
        _reference(dataset, version, parts)
        for dataset, (version, parts) in normalized.items()
    )
    sources = sorted(
        (manifest.object_id, manifest.checksum_sha256)
        for items in selected.values()
        for _, manifest in items
    )
    snapshot = DatasetSnapshotManifest(
        dataset_id=DATASET_ID,
        dataset_version=f"live-{content_sha256(sources)[:24]}",
        created_at=max(
            manifest.retrieved_at
            for items in selected.values()
            for _, manifest in items
        ),
        datasets=references,
        source_manifest_hash=content_sha256(sources),
        normalizer_code_version=NORMALIZER_CODE_VERSION,
        normalizer_parameters_hash=content_sha256(
            {name: version for name, (version, _) in normalized.items()}
        ),
    )
    catalog.register_dataset(snapshot)
    (root / "dataset-snapshot.json").write_text(
        manifest_json(snapshot), encoding="utf-8"
    )
    _write_configs(
        root,
        snapshot,
        config_root=config_root,
        runs_root=runs_root,
    )
    print(f"dataset_id={snapshot.dataset_id}")
    print(f"dataset_version={snapshot.dataset_version}")
    print(f"database={database}")
    print(f"config_root={config_root}")
    print(f"runs_root={runs_root}")


if __name__ == "__main__":
    main()
