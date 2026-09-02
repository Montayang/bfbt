"""Build a real annual DatasetSnapshot from previously downloaded Binance Raw data."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    PartitionManifest,
    RawObjectManifest,
    load_manifest,
    manifest_json,
)
from bfbt.data.normalize import (
    NORMALIZER_CODE_VERSION,
    build_normalization_release,
)
from bfbt.data.normalize.service import NormalizationService
from bfbt.data.schemas import get_schema_definition
from bfbt.data.validation.reports import QualityPolicy

UTC = timezone.utc
RUN_START = datetime(2025, 1, 1, tzinfo=UTC)
RUN_END = datetime(2026, 1, 1, tzinfo=UTC)


def _discover(root: Path) -> dict[str, list[tuple[Path, RawObjectManifest]]]:
    result: dict[str, list[tuple[Path, RawObjectManifest]]] = defaultdict(list)
    for path in sorted((root / "manifests" / "raw").glob("*.json")):
        manifest = load_manifest(path, "raw")
        if not isinstance(manifest, RawObjectManifest):
            raise TypeError(f"not a Raw manifest: {path}")
        if (
            manifest.source == "binance_rest"
            and manifest.dataset_name in {"bars", "mark_bars"}
            and manifest.available_from is not None
            and manifest.available_to is not None
            and (
                manifest.available_from.year,
                manifest.available_from.month,
            )
            != (
                (manifest.available_to - timedelta(milliseconds=1)).year,
                (manifest.available_to - timedelta(milliseconds=1)).month,
            )
        ):
            print(f"quarantine cross_month_rest={path.name}", flush=True)
            continue
        result[manifest.dataset_name].append((path, manifest))
    return result


def _batches(
    items: list[tuple[Path, RawObjectManifest]], size: int
) -> list[tuple[Path, ...]]:
    months: dict[tuple[int, int], list[Path]] = defaultdict(list)
    for path, manifest in items:
        value = manifest.available_from or manifest.retrieved_at
        months[(value.year, value.month)].append(path)
    return [
        tuple(paths[index : index + size])
        for month in sorted(months)
        for paths in [sorted(months[month])]
        for index in range(0, len(paths), size)
    ]


def _resume_partitions(
    root: Path,
    dataset: str,
    version: str,
    by_object: dict[str, RawObjectManifest],
    catalog: DuckDBCatalog,
) -> tuple[list[PartitionManifest], set[str]]:
    parts = []
    covered: set[str] = set()
    manifest_root = root / "manifests" / "partitions"
    for path in sorted(manifest_root.glob("*.json")):
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
            raise ValueError(
                f"resume partition has unknown Raw sources: {sorted(unknown)}"
            )
        if overlap := covered & sources:
            raise ValueError(
                f"resume partitions overlap Raw sources: {sorted(overlap)}"
            )
        for object_id in manifest.source_object_ids:
            catalog.register_raw(by_object[object_id])
        catalog.register_partition(manifest)
        covered.update(sources)
        parts.append(manifest)
    if parts:
        print(
            f"resume dataset={dataset} partitions={len(parts)} "
            f"sources={len(covered)}",
            flush=True,
        )
    return parts, covered


def _normalize(
    dataset: str,
    items: list[tuple[Path, RawObjectManifest]],
    root: Path,
    catalog: DuckDBCatalog,
    size: int,
) -> tuple[str, tuple[PartitionManifest, ...]]:
    release = build_normalization_release(
        dataset, (manifest for _, manifest in items)
    )
    by_object = {manifest.object_id: manifest for _, manifest in items}
    parts, covered = _resume_partitions(
        root, dataset, release.dataset_version, by_object, catalog
    )
    remaining = [
        item for item in items if item[1].object_id not in covered
    ]
    batches = _batches(remaining, size)
    for number, paths in enumerate(batches, 1):
        published = NormalizationService().run(
            dataset,
            paths,
            raw_root=root / "raw",
            normalized_root=root / "normalized",
            partition_manifest_root=root / "manifests" / "partitions",
            quality_root=root / "quality",
            catalog=catalog,
            policy=QualityPolicy(max_missing_ratio=1.0),
            row_group_rows=262_144,
            release=release,
        )
        parts.append(published.partition_manifest)
        if number == 1 or number % 100 == 0 or number == len(batches):
            print(
                f"normalize dataset={dataset} batch={number}/{len(batches)} "
                f"rows={published.partition_manifest.row_count}",
                flush=True,
            )
    return release.dataset_version, tuple(parts)


def _reference(
    dataset: str,
    version: str,
    parts: tuple[PartitionManifest, ...],
    start: datetime | None = None,
    end: datetime | None = None,
) -> DatasetReference:
    minimum = min(part.min_time for part in parts if part.min_time is not None)
    maximum = max(part.max_time for part in parts if part.max_time is not None)
    return DatasetReference(
        dataset_name=dataset,
        dataset_version=version,
        schema_version="v1",
        schema_fingerprint=get_schema_definition(dataset, "v1").fingerprint,
        available_from=start or minimum,
        available_to=end or maximum + timedelta(milliseconds=1),
        partition_manifest_ids=tuple(part.partition_id for part in parts),
        quality_report_ids=tuple(part.quality_report_id for part in parts),
    )


def _configs(
    root: Path,
    snapshot: DatasetSnapshotManifest,
    *,
    config_root: Path,
    runs_root: Path,
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    configs = {
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
                "start": RUN_START.isoformat(),
                "end": RUN_END.isoformat(),
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
                "min_listing_age_days": 30,
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
                "name": "a10_real_usdm_full_market_2025",
                "start": RUN_START.isoformat(),
                "end": RUN_END.isoformat(),
                "dataset_version": snapshot.dataset_version,
                "random_seed": 42,
            },
            "schedule": {
                "factor_interval": "1h",
                "rebalance_interval": "4h",
                "signal_delay_bars": 1,
            },
            "portfolio": {
                "construction": "long_short_quantile",
                "long_quantile": 0.2,
                "short_quantile": 0.2,
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
                "funding": {"enabled": True, "missing_policy": "assume_zero"},
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
                "max_input_rows_per_chunk": 5_000_000,
                "max_incremental_rss_mib": 1536,
                "collect_diagnostics": True,
            },
        },
    }
    for name, payload in configs.items():
        (config_root / f"{name}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--bar-batch-symbols", type=int, default=4)
    parser.add_argument("--funding-batch-symbols", type=int, default=128)
    args = parser.parse_args()
    root = args.root.resolve()
    database = (args.database or root / "catalog.duckdb").resolve()
    config_root = (args.config_root or root / "configs").resolve()
    runs_root = (args.runs_root or root / "runs").resolve()
    catalog = DuckDBCatalog(database)
    catalog.initialize()
    raw = _discover(root)
    required = {"bars", "mark_bars", "funding", "contracts"}
    if missing := required - raw.keys():
        raise ValueError(f"missing Raw datasets: {sorted(missing)}")

    normalized = {}
    for dataset in ("bars", "mark_bars", "funding", "contracts"):
        size = (
            args.bar_batch_symbols
            if dataset in {"bars", "mark_bars"}
            else args.funding_batch_symbols
        )
        normalized[dataset] = _normalize(dataset, raw[dataset], root, catalog, size)

    references = []
    for dataset, (version, parts) in normalized.items():
        if dataset == "contracts":
            references.append(
                _reference(
                    dataset,
                    version,
                    parts,
                    datetime(2024, 12, 1, tzinfo=UTC),
                    datetime(2026, 8, 1, tzinfo=UTC),
                )
            )
        else:
            references.append(_reference(dataset, version, parts))
    sources = sorted(
        (manifest.object_id, manifest.checksum_sha256)
        for items in raw.values()
        for _, manifest in items
    )
    snapshot = DatasetSnapshotManifest(
        dataset_id="binance-usdm-full-market-2025",
        dataset_version=f"a10-live-{content_sha256(sources)[:24]}",
        created_at=max(
            manifest.retrieved_at
            for items in raw.values()
            for _, manifest in items
        ),
        datasets=tuple(references),
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
    _configs(
        root, snapshot, config_root=config_root, runs_root=runs_root
    )
    print(f"dataset_id={snapshot.dataset_id}")
    print(f"dataset_version={snapshot.dataset_version}")
    print(f"database={database}")
    print(f"config_root={config_root}")
    print(f"runs_root={runs_root}")


if __name__ == "__main__":
    main()
