"""Prepare a bounded, genuinely online Binance USD-M A18 DatasetSnapshot.

The script uses only credential-free public archive/REST interfaces.  Its fixed
scope is four liquid contracts and roughly three days of 1m bars; it is not a
capacity or full-market test.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    RawObjectManifest,
    load_manifest,
    manifest_json,
)
from bfbt.data.normalize import NORMALIZER_CODE_VERSION
from bfbt.data.normalize.service import NormalizationService
from bfbt.data.schemas import get_schema_definition
from bfbt.data.validation.reports import QualityPolicy

UTC = timezone.utc
SYMBOLS = ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT")
DOWNLOAD_START = datetime(2025, 1, 7, tzinfo=UTC)
DOWNLOAD_END = datetime(2025, 1, 11, tzinfo=UTC)
RUN_START = datetime(2025, 1, 8, tzinfo=UTC)
RUN_END = datetime(2025, 1, 10, tzinfo=UTC)


def _command(*values: object) -> None:
    command = [str(value) for value in values]
    print("exec=" + " ".join(command), flush=True)
    subprocess.run(command, check=True)


def _download(root: Path, database: Path) -> None:
    raw = root / "raw"
    manifests = root / "manifests" / "raw"
    start = DOWNLOAD_START.isoformat()
    end = DOWNLOAD_END.isoformat()
    for symbol in SYMBOLS:
        for dataset in ("bars", "mark_bars"):
            _command(
                "bfbt",
                "data",
                "archive-sync",
                dataset,
                symbol,
                start,
                end,
                "--interval",
                "1m",
                "--frequency",
                "daily",
                "--raw-root",
                raw,
                "--manifest-root",
                manifests,
                "--database",
                database,
                "--workers",
                "2",
            )
        _command(
            "bfbt",
            "data",
            "rest-funding",
            symbol,
            start,
            end,
            "--raw-root",
            raw,
            "--manifest-root",
            manifests,
            "--database",
            database,
        )
    _command(
        "bfbt",
        "data",
        "snapshot",
        "exchange-info",
        "--raw-root",
        raw,
        "--manifest-root",
        manifests,
        "--database",
        database,
    )


def _load_raw(path: Path) -> RawObjectManifest:
    value = load_manifest(path, "raw")
    if not isinstance(value, RawObjectManifest):
        raise TypeError(f"not a Raw manifest: {path}")
    return value


def _select(root: Path) -> dict[str, tuple[Path, ...]]:
    selected: dict[str, list[Path]] = {
        "bars": [],
        "mark_bars": [],
        "funding": [],
        "contracts": [],
    }
    for path in sorted((root / "manifests" / "raw").glob("*.json")):
        manifest = _load_raw(path)
        if manifest.dataset_name == "contracts":
            selected["contracts"].append(path)
            continue
        if manifest.dataset_name not in selected or manifest.symbol not in SYMBOLS:
            continue
        if manifest.available_from is None or manifest.available_to is None:
            continue
        if (
            manifest.available_to <= DOWNLOAD_START
            or manifest.available_from >= DOWNLOAD_END
        ):
            continue
        selected[manifest.dataset_name].append(path)
    if selected["contracts"]:
        selected["contracts"] = [selected["contracts"][-1]]
    missing = [name for name, paths in selected.items() if not paths]
    if missing:
        raise ValueError(f"online preparation is missing datasets: {missing}")
    return {name: tuple(paths) for name, paths in selected.items()}


def _reference(dataset: str, result) -> DatasetReference:
    part = result.partition_manifest
    assert part.min_time is not None and part.max_time is not None
    return DatasetReference(
        dataset_name=dataset,
        dataset_version=part.dataset_version,
        schema_version="v1",
        schema_fingerprint=get_schema_definition(dataset, "v1").fingerprint,
        available_from=(
            DOWNLOAD_START if dataset == "funding" else part.min_time
        ),
        available_to=(
            DOWNLOAD_END
            if dataset == "funding"
            else part.max_time + timedelta(milliseconds=1)
        ),
        partition_manifest_ids=(part.partition_id,),
        quality_report_ids=(part.quality_report_id,),
    )


def _risk(enabled: bool) -> dict[str, object]:
    return {
        "leverage": 2.0,
        "enforce_liquidation": False,
        "evaluation_interval": "1m",
        "trigger_price": "trade",
        "fill_model": "next_bar_open",
        "intrabar_conflict": "worst_case",
        "symbol_exits": {
            "stop_loss": {
                "enabled": enabled,
                "distance": 0.0001 if enabled else None,
                "action": "close",
            },
            "take_profit": {
                "enabled": enabled,
                "distance": 0.0001 if enabled else None,
                "action": "close",
            },
            "trailing_stop": {"enabled": False},
        },
        "portfolio_exits": {
            "stop_loss": None,
            "take_profit": None,
            "max_drawdown": None,
        },
        "cooldown_bars": 2,
        "reentry_policy": "next_scheduled_rebalance",
        "max_triggers_per_symbol": 1 if enabled else None,
    }


def _backtest(
    snapshot: DatasetSnapshotManifest,
    *,
    name: str,
    lag: int,
    sizing_mode: str,
    risk_enabled: bool,
    runs_root: Path,
) -> dict[str, object]:
    if sizing_mode == "target_weight":
        sizing = {
            "mode": "target_weight",
            "weighting": "equal",
            "target_gross_exposure": 1.0,
            "target_net_exposure": 0.0,
        }
    else:
        sizing = {
            "mode": "fixed_margin",
            "margin_amount": 100.0,
            "reverse_policy": "flatten_then_open",
        }
    return {
        "config_version": "v2",
        "run": {
            "name": name,
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
        "capital": {
            "currency": "USDT",
            "initial_equity": 10000.0,
            "margin_model": "simple_cross",
            "reserved_cost_buffer": 100.0,
        },
        "portfolio": {
            "selection": {
                "mode": "rank_set",
                "rank_order": "descending",
                "clock": "rebalance",
                "lag": lag,
                "long": {"ranks": [2], "ranges": []},
                "short": {"ranks": [1], "ranges": []},
            },
            "sizing": sizing,
            "constraints": {
                "max_gross_exposure": 2.0,
                "max_net_exposure": 1.0,
                "max_symbol_weight": 0.6,
                "max_symbol_notional": 5000.0,
                "max_consecutive_adds": 3,
                "max_turnover": 1.0,
            },
        },
        "execution": {
            "fill_price": "next_bar_open",
            "partial_fill": False,
            "fee": {"model": "fixed_bps", "taker_bps": 4.0},
            "slippage": {"model": "fixed_bps", "bps": 2.0},
            "funding": {"enabled": True, "missing_policy": "assume_zero"},
        },
        "valuation": {"price": "mark_close"},
        "risk": _risk(risk_enabled),
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
            "mode": "in_memory",
            "chunk_interval": "1d",
            "max_input_rows_per_chunk": 100000,
            "max_incremental_rss_mib": 512,
            "collect_diagnostics": True,
            "max_rank_lag": 2,
            "max_rank_state_rows": 1000,
            "max_position_state_rows": 20,
            "max_pending_instructions": 100,
            "max_risk_state_rows": 20,
            "max_pending_risk_intents": 100,
        },
    }


def _write_configs(
    root: Path,
    snapshot: DatasetSnapshotManifest,
    *,
    config_root: Path,
    runs_root: Path,
) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    common = {
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
                "start": DOWNLOAD_START.isoformat(),
                "end": DOWNLOAD_END.isoformat(),
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
                "min_history_bars": 360,
                "rolling_quote_volume": {"window": "6h", "minimum": 0},
                "max_missing_ratio": {"window": "6h", "maximum": 0.01},
                "exclude_symbols": [],
            },
        },
        "factor": {
            "factors": [
                {
                    "name": "momentum",
                    "version": "v1",
                    "parameters": {"lookback": "6h", "skip_recent": "1h"},
                    "compute_interval": "1h",
                    "preprocess": [{"name": "rank"}],
                }
            ],
            "labels": [
                {
                    "name": "forward_return_4h",
                    "signal_delay_bars": 1,
                    "horizon": "4h",
                    "entry_field": "open",
                    "exit_field": "open",
                }
            ],
            "cache": {"enabled": True},
        },
    }
    scenarios = {
        "backtest_exact_rank": _backtest(
            snapshot,
            name="a18_live_exact_rank",
            lag=0,
            sizing_mode="target_weight",
            risk_enabled=False,
            runs_root=runs_root,
        ),
        "backtest_lag1_rank": _backtest(
            snapshot,
            name="a18_live_lag1_rank",
            lag=1,
            sizing_mode="target_weight",
            risk_enabled=False,
            runs_root=runs_root,
        ),
        "backtest_fixed_margin": _backtest(
            snapshot,
            name="a18_live_fixed_margin",
            lag=0,
            sizing_mode="fixed_margin",
            risk_enabled=False,
            runs_root=runs_root,
        ),
        "backtest_risk_conflict": _backtest(
            snapshot,
            name="a18_live_risk_conflict",
            lag=0,
            sizing_mode="fixed_margin",
            risk_enabled=True,
            runs_root=runs_root,
        ),
    }
    for name, payload in {**common, **scenarios}.items():
        (config_root / f"{name}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--runs-root", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    database = (args.database or root / "catalog.duckdb").resolve()
    config_root = (args.config_root or root / "configs").resolve()
    runs_root = (args.runs_root or root / "runs").resolve()
    root.mkdir(parents=True, exist_ok=True)
    catalog = DuckDBCatalog(database)
    catalog.initialize()

    _download(root, database)
    selected = _select(root)
    roots = {
        "raw_root": root / "raw",
        "normalized_root": root / "normalized",
        "partition_manifest_root": root / "manifests" / "partitions",
        "quality_root": root / "quality",
    }
    normalized = {}
    for dataset, paths in selected.items():
        normalized[dataset] = NormalizationService().run(
            dataset,
            paths,
            **roots,
            catalog=catalog,
            policy=QualityPolicy(max_missing_ratio=1.0),
            row_group_rows=131072,
        )
        print(
            f"normalized={dataset} rows="
            f"{normalized[dataset].partition_manifest.row_count}",
            flush=True,
        )
    sources = sorted(
        (_load_raw(path).object_id, _load_raw(path).checksum_sha256)
        for paths in selected.values()
        for path in paths
    )
    snapshot = DatasetSnapshotManifest(
        dataset_id="binance-usdm-a18-live-representative",
        dataset_version=f"a18-live-{content_sha256(sources)[:24]}",
        created_at=max(
            _load_raw(path).retrieved_at
            for paths in selected.values()
            for path in paths
        ),
        datasets=tuple(
            _reference(dataset, result)
            for dataset, result in normalized.items()
        ),
        source_manifest_hash=content_sha256(sources),
        normalizer_code_version=NORMALIZER_CODE_VERSION,
        normalizer_parameters_hash=content_sha256(
            {
                dataset: result.partition_manifest.dataset_version
                for dataset, result in normalized.items()
            }
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
    context = {
        "dataset_id": snapshot.dataset_id,
        "dataset_version": snapshot.dataset_version,
        "database": str(database),
        "config_root": str(config_root),
        "runs_root": str(runs_root),
        "symbols": list(SYMBOLS),
        "download_range": [DOWNLOAD_START.isoformat(), DOWNLOAD_END.isoformat()],
        "run_range": [RUN_START.isoformat(), RUN_END.isoformat()],
    }
    (root / "live-context.json").write_text(
        json.dumps(context, indent=2), encoding="utf-8"
    )
    print(json.dumps(context, indent=2), flush=True)


if __name__ == "__main__":
    main()
