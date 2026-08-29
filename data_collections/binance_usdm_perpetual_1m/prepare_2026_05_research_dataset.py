"""Normalize immutable May 2026 bars and funding used by research."""

from __future__ import annotations

import argparse
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
from bianbt.data.schemas import get_schema_definition

from prepare_2026_06_dataset import _normalize


UTC = timezone.utc
APRIL_30 = datetime(2026, 4, 30, tzinfo=UTC)
MAY_1 = datetime(2026, 5, 1, tzinfo=UTC)
JUNE_1 = datetime(2026, 6, 1, tzinfo=UTC)
DATASET_ID = "binance-usdm-perpetual-1m-2026-05-research"


def selected_objects(
    root: Path,
) -> dict[str, list[tuple[Path, RawObjectManifest]]]:
    values: list[tuple[Path, RawObjectManifest]] = []
    for path in sorted((root / "manifests/raw").glob("*.json")):
        manifest = load_manifest(path, "raw")
        if not isinstance(manifest, RawObjectManifest):
            continue
        if manifest.dataset_name == "bars" and manifest.interval != "1m":
            continue
        if manifest.dataset_name == "bars" and (
            manifest.available_from, manifest.available_to
        ) in {(APRIL_30, MAY_1), (MAY_1, JUNE_1)}:
            values.append((path, manifest))
        elif manifest.dataset_name == "funding" and (
            manifest.available_from, manifest.available_to
        ) == (MAY_1, JUNE_1):
            values.append((path, manifest))
    monthly_symbols = {
        manifest.symbol
        for _, manifest in values
        if manifest.dataset_name == "bars"
        and manifest.available_from == MAY_1
        and manifest.available_to == JUNE_1
    }
    if not monthly_symbols:
        raise RuntimeError("no verified May monthly archives found")
    selected = {
        dataset: [
            item
            for item in values
            if item[1].dataset_name == dataset
            and item[1].symbol in monthly_symbols
        ]
        for dataset in ("bars", "funding")
    }
    warmup_symbols = {
        manifest.symbol
        for _, manifest in selected["bars"]
        if manifest.available_from == APRIL_30 and manifest.available_to == MAY_1
    }
    funding_symbols = {manifest.symbol for _, manifest in selected["funding"]}
    if not funding_symbols:
        raise RuntimeError("no verified May funding archives found")
    # Binance does not publish a monthly funding archive for every symbol that has
    # a monthly K-line archive. This is also true in the frozen June dataset. Keep
    # every observed settlement and make the archive-level coverage explicit;
    # execution treats the absence of a row as no settlement event at that time.
    missing = sorted(monthly_symbols - funding_symbols)
    print(
        f"monthly_symbols={len(monthly_symbols)} warmup_symbols={len(warmup_symbols)} "
        f"bar_objects={len(selected['bars'])} funding_objects={len(selected['funding'])} "
        f"funding_symbols={len(funding_symbols)} funding_archive_absent={len(missing)} "
        f"absent_examples={missing[:10]}",
        flush=True,
    )
    return selected


def dataset_reference(dataset: str, version: str, parts) -> DatasetReference:
    minimum = min(part.min_time for part in parts if part.min_time is not None)
    maximum = max(part.max_time for part in parts if part.max_time is not None)
    return DatasetReference(
        dataset_name=dataset,
        dataset_version=version,
        schema_version="v1",
        schema_fingerprint=get_schema_definition(dataset, "v1").fingerprint,
        available_from=minimum,
        available_to=maximum + timedelta(milliseconds=1),
        partition_manifest_ids=tuple(part.partition_id for part in parts),
        quality_report_ids=tuple(part.quality_report_id for part in parts),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--batch-symbols", type=int, default=8)
    args = parser.parse_args()

    root = args.root.resolve()
    catalog = DuckDBCatalog(args.database.resolve())
    catalog.initialize()
    items = selected_objects(root)
    bars_version, bars_parts = _normalize(
        root, "bars", items["bars"], catalog, args.batch_symbols
    )
    funding_version, funding_parts = _normalize(
        root, "funding", items["funding"], catalog, args.batch_symbols
    )
    june_snapshot = load_manifest(root / "dataset-snapshot-2026-06.json", "dataset")
    if not isinstance(june_snapshot, DatasetSnapshotManifest):
        raise TypeError("June snapshot is not a dataset snapshot")
    contracts = next(
        reference
        for reference in june_snapshot.datasets
        if reference.dataset_name == "contracts"
    )
    sources = {
        dataset: sorted(
            (manifest.object_id, manifest.checksum_sha256)
            for _, manifest in selected
        )
        for dataset, selected in items.items()
    }
    identity = {
        "sources": sources,
        "contracts_dataset_version": contracts.dataset_version,
    }
    snapshot = DatasetSnapshotManifest(
        dataset_id=DATASET_ID,
        dataset_version=f"live-{content_sha256(identity)[:24]}",
        created_at=max(
            manifest.retrieved_at
            for selected in items.values()
            for _, manifest in selected
        ),
        datasets=(
            dataset_reference("bars", bars_version, bars_parts),
            dataset_reference("funding", funding_version, funding_parts),
            contracts,
        ),
        source_manifest_hash=content_sha256(sources),
        normalizer_code_version=NORMALIZER_CODE_VERSION,
        normalizer_parameters_hash=content_sha256(
            {
                "bars": bars_version,
                "funding": funding_version,
                "contracts": contracts.dataset_version,
            }
        ),
    )
    catalog.register_dataset(snapshot)
    destination = root / "dataset-snapshot-2026-05-research.json"
    destination.write_text(manifest_json(snapshot), encoding="utf-8")
    print(f"dataset_id={snapshot.dataset_id}")
    print(f"dataset_version={snapshot.dataset_version}")
    print(f"bars_version={bars_version}")
    print(f"funding_version={funding_version}")
    print(f"snapshot={destination}")


if __name__ == "__main__":
    main()
