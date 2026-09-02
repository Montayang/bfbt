"""Segmented May-July GTJA191 quick research with a sealed July holdout."""

from __future__ import annotations

import argparse
import glob
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import polars as pl

from bfbt.config.factor import LabelDefinition
from bfbt.config.universe import UniverseConfig
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import DatasetSnapshotManifest, load_manifest
from bfbt.data.resample import resample_bars
from bfbt.factors.registry import compute_factor
from bfbt.labels.forward_returns import compute_forward_returns
from bfbt.reports.research_study import render_quick_only_study_report
from bfbt.research.ic import information_coefficient
from bfbt.research.quantiles import quantile_returns
from bfbt.research.turnover import factor_rank_turnover
from bfbt.universe.point_in_time import build_point_in_time_universe, build_schedule

from run_gtja191_quick_research import FORMULAS, definition, sink_cache


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/backtest"
MARKET = DATA / "datasets/binance_usdm_perpetual_1m"
STUDY = DATA / "research_studies/gtja191_segmented_dev_holdout_2026_05_07"
WORK = STUDY / "working"
UTC = timezone.utc

MAY_1 = datetime(2026, 5, 1, tzinfo=UTC)
JUNE_1 = datetime(2026, 6, 1, tzinfo=UTC)
JULY_1 = datetime(2026, 7, 1, tzinfo=UTC)
AUGUST_1 = datetime(2026, 8, 1, tzinfo=UTC)

DEV_WINDOWS = (
    ("DEV-01", datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 9, tzinfo=UTC)),
    ("DEV-02", datetime(2026, 5, 9, tzinfo=UTC), datetime(2026, 5, 17, tzinfo=UTC)),
    ("DEV-03", datetime(2026, 5, 17, tzinfo=UTC), datetime(2026, 5, 25, tzinfo=UTC)),
    ("DEV-04", datetime(2026, 5, 25, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)),
    ("DEV-05", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 6, 9, tzinfo=UTC)),
    ("DEV-06", datetime(2026, 6, 9, tzinfo=UTC), datetime(2026, 6, 17, tzinfo=UTC)),
    ("DEV-07", datetime(2026, 6, 17, tzinfo=UTC), datetime(2026, 6, 24, tzinfo=UTC)),
    ("DEV-08", datetime(2026, 6, 24, tzinfo=UTC), datetime(2026, 7, 1, tzinfo=UTC)),
)
HOLDOUT_WINDOWS = (
    ("HOLDOUT-01", datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 9, tzinfo=UTC)),
    ("HOLDOUT-02", datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 17, tzinfo=UTC)),
    ("HOLDOUT-03", datetime(2026, 7, 17, tzinfo=UTC), datetime(2026, 7, 25, tzinfo=UTC)),
    ("HOLDOUT-04", datetime(2026, 7, 25, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC)),
)

CONTRACT = {
    "source": "GTJA report SHA256 863f62c2e23bd87ddb42b8338c8fe2b0276d94260ac985e1a0edfff318693c6c",
    "factors": FORMULAS,
    "bar_intervals": ["1m", "5m", "15m"],
    "forward_horizon_bars": [1, 5, 20],
    "factor_history": "continuous within each calendar-month source with at least one day warmup; no cold-start duplicate",
    "window_labels": "future-return exit_time must be at or before each window end",
    "development_windows": [(name, start.isoformat(), end.isoformat()) for name, start, end in DEV_WINDOWS],
    "holdout_windows": [(name, start.isoformat(), end.isoformat()) for name, start, end in HOLDOUT_WINDOWS],
    "label_timing": "signal at bar close; enter next bar open; exit open after N bars",
    "preprocess": "cross-sectional winsorize 1%-99%, then z-score",
    "volume_policy": "quote_volume primary; base volume Alpha40 control only",
    "screening": "none; retain every result for user review",
}
CONTRACT_SHA256 = content_sha256(CONTRACT)


@dataclass(frozen=True)
class Pool:
    name: str
    role: str
    start: datetime
    end: datetime
    windows: tuple[tuple[str, datetime, datetime], ...]
    bars: pl.LazyFrame
    bars_version: str
    universe: pl.LazyFrame
    universe_version: str


def announce(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", flush=True)


def scan_normalized(dataset: str, version: str) -> pl.LazyFrame:
    pattern = str(
        MARKET / "normalized" / dataset / "schema=v1"
        / f"dataset_version={version}/**/*.parquet"
    )
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        raise RuntimeError(f"missing normalized {dataset} {version}")
    return pl.scan_parquet(paths, hive_partitioning=False)


def existing_universe(analysis_id: str) -> pl.LazyFrame:
    root = DATA / "reuse/analysis" / analysis_id
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    paths = [root / item["path"] for item in manifest["tables"]["universe"]]
    return pl.scan_parquet(paths, hive_partitioning=False)


def snapshot(path: Path) -> DatasetSnapshotManifest:
    value = load_manifest(path, "dataset")
    if not isinstance(value, DatasetSnapshotManifest):
        raise TypeError(f"not a dataset snapshot: {path}")
    return value


def reference_version(value: DatasetSnapshotManifest, name: str) -> str:
    return next(item.dataset_version for item in value.datasets if item.dataset_name == name)


def may_universe(
    bars: pl.LazyFrame, bars_version: str, contracts_version: str
) -> tuple[pl.LazyFrame, str]:
    cache = WORK / "may_universe.parquet"
    config = UniverseConfig.model_validate({
        "schedule": {"interval": "1m"},
        "point_in_time": {
            "enabled": True, "use_contract_snapshots": False,
            "use_first_last_valid_bar": True,
        },
        "filters": {
            "trading_status_only": False, "min_listing_age_days": 0,
            "min_history_bars": 1440,
            "rolling_quote_volume": {"window": "24h", "minimum": 0},
            "max_missing_ratio": {"window": "24h", "maximum": 0.0},
            "exclude_symbols": [],
        },
    })
    contracts = scan_normalized("contracts", contracts_version)
    result = build_point_in_time_universe(
        bars, contracts, build_schedule(start=MAY_1, end=JUNE_1, interval="1m"),
        config=config, base_interval="1m", bars_dataset_version=bars_version,
        contracts_dataset_version=contracts_version,
    )
    if not cache.exists():
        announce("build May point-in-time universe")
        sink_cache(result.frame, cache)
    return pl.scan_parquet(cache), result.universe_version


def build_pools() -> tuple[Pool, Pool, Pool]:
    may_snapshot = snapshot(MARKET / "dataset-snapshot-2026-05-research.json")
    june_snapshot = snapshot(MARKET / "dataset-snapshot-2026-06.json")
    july_snapshot = snapshot(MARKET / "dataset-snapshot-2026-07.json")
    may_version = reference_version(may_snapshot, "bars")
    june_version = reference_version(june_snapshot, "bars")
    july_version = reference_version(july_snapshot, "bars")
    contracts_version = reference_version(may_snapshot, "contracts")
    may_bars = scan_normalized("bars", may_version)
    june_bars = scan_normalized("bars", june_version)
    july_bars = scan_normalized("bars", july_version)
    may_u, may_u_version = may_universe(may_bars, may_version, contracts_version)
    june_u = existing_universe("analysis-c09cba7b63809ffb6a408d49")
    july_u = existing_universe("analysis-0495ebb9842e093bd8890a0c")

    return (
        Pool("MAY", "开发", MAY_1, JUNE_1, DEV_WINDOWS[:4], may_bars, may_version, may_u, may_u_version),
        Pool("JUNE", "开发", JUNE_1, JULY_1, DEV_WINDOWS[4:], june_bars, june_version, june_u, "a06-90cc53319555a74b4d251bac"),
        Pool("HOLDOUT", "未见", JULY_1, AUGUST_1, HOLDOUT_WINDOWS, july_bars, july_version, july_u, "a06-de2fa6299f92d9d49dc1def2"),
    )


def interval_minutes(interval: str) -> int:
    return int(interval.removesuffix("m"))


def tagged(frame: pl.LazyFrame, windows) -> pl.LazyFrame:
    period = None
    end_value = None
    for name, start, end in windows:
        condition = (pl.col("timestamp") >= start) & (pl.col("timestamp") < end)
        period = pl.when(condition).then(pl.lit(name)) if period is None else period.when(condition).then(pl.lit(name))
        end_value = pl.when(condition).then(pl.lit(end)) if end_value is None else end_value.when(condition).then(pl.lit(end))
    assert period is not None and end_value is not None
    return frame.with_columns(
        period.otherwise(None).alias("_period"),
        end_value.otherwise(None).alias("_period_end"),
    ).filter(pl.col("_period").is_not_null())


def factor_diagnostics(factors: pl.LazyFrame, pool: Pool) -> dict[str, dict[str, float]]:
    values = tagged(factors, pool.windows)
    counts = values.group_by("_period").agg(
        pl.len().alias("eligible"), pl.col("is_valid").sum().alias("valid")
    )
    turnover = tagged(factor_rank_turnover(factors), pool.windows).group_by("_period").agg(
        pl.col("rank_turnover").mean().alias("turnover"),
        pl.col("rank_turnover").count().alias("turnover_timestamps"),
    )
    overall_counts = factors.select(
        pl.lit(f"{pool.name}-ALL").alias("_period"),
        pl.len().alias("eligible"), pl.col("is_valid").sum().alias("valid"),
    )
    overall_turnover = factor_rank_turnover(factors).select(
        pl.lit(f"{pool.name}-ALL").alias("_period"),
        pl.col("rank_turnover").mean().alias("turnover"),
        pl.col("rank_turnover").count().alias("turnover_timestamps"),
    )
    combined = pl.concat([counts, overall_counts]).join(
        pl.concat([turnover, overall_turnover]), on="_period"
    ).collect(engine="streaming")
    return {
        row["_period"]: {
            "factor_coverage": row["valid"] / row["eligible"],
            "mean_rank_turnover": row["turnover"],
            "_eligible_observations": int(row["eligible"]),
            "_valid_factor_observations": int(row["valid"]),
            "_turnover_timestamps": int(row["turnover_timestamps"]),
        }
        for row in combined.to_dicts()
    }


def predictive_summaries(
    factors: pl.LazyFrame, labels: pl.LazyFrame, pool: Pool, horizon_minutes: int
) -> dict[str, dict[str, float | int]]:
    boundary = pl.col("timestamp") + pl.duration(minutes=horizon_minutes) <= pl.col("_period_end")
    ic = information_coefficient(factors, labels)
    q = quantile_returns(factors, labels, quantiles=5)
    window_ic = tagged(ic, pool.windows).filter(boundary).group_by("_period").agg(
        pl.len().alias("timestamps"),
        pl.col("rank_ic").mean().alias("mean_rank_ic"),
        pl.col("rank_ic").std(ddof=0).alias("rank_ic_std"),
        (pl.col("rank_ic") > 0).mean().alias("positive_fraction"),
        pl.col("sample_count").mean().alias("average_sample_count"),
    )
    all_ic = ic.filter(pl.col("timestamp") + pl.duration(minutes=horizon_minutes) <= pool.end).select(
        pl.lit(f"{pool.name}-ALL").alias("_period"),
        pl.len().alias("timestamps"),
        pl.col("rank_ic").mean().alias("mean_rank_ic"),
        pl.col("rank_ic").std(ddof=0).alias("rank_ic_std"),
        (pl.col("rank_ic") > 0).mean().alias("positive_fraction"),
        pl.col("sample_count").mean().alias("average_sample_count"),
    )
    window_q = tagged(q, pool.windows).filter(boundary).group_by("_period", "quantile").agg(
        pl.col("mean_forward_return").mean().alias("mean")
    )
    all_q = q.filter(pl.col("timestamp") + pl.duration(minutes=horizon_minutes) <= pool.end).group_by("quantile").agg(
        pl.col("mean_forward_return").mean().alias("mean")
    ).with_columns(pl.lit(f"{pool.name}-ALL").alias("_period")).select("_period", "quantile", "mean")
    ic_frame, q_frame = pl.collect_all(
        [pl.concat([window_ic, all_ic]), pl.concat([window_q, all_q])], engine="streaming"
    )
    qmaps: dict[str, dict[int, float]] = {}
    for row in q_frame.to_dicts():
        qmaps.setdefault(row["_period"], {})[int(row["quantile"])] = float(row["mean"])
    output = {}
    for row in ic_frame.to_dicts():
        mean, std = float(row["mean_rank_ic"]), float(row["rank_ic_std"])
        qmap = qmaps[row["_period"]]
        output[row["_period"]] = {
            "timestamps": int(row["timestamps"]), "mean_rank_ic": mean,
            "rank_ic_ir": mean / std if std > 0 else float("nan"),
            "rank_ic_positive_fraction": float(row["positive_fraction"]),
            "q5_minus_q1": qmap.get(5, float("nan")) - qmap.get(1, float("nan")),
            "average_sample_count": float(row["average_sample_count"]),
        }
    return output


def source_for_interval(pool: Pool, interval: str) -> tuple[pl.LazyFrame, str]:
    if interval == "1m":
        return pool.bars, pool.bars_version
    cache = WORK / f"{pool.name}_{interval}_bars.parquet"
    result = resample_bars(
        pool.bars, dataset_name="bars", source_interval="1m", target_interval=interval,
        source_dataset_version=pool.bars_version,
    )
    if not cache.exists():
        announce(f"resample {pool.name} {interval}")
        sink_cache(result.frame, cache)
    return pl.scan_parquet(cache), result.dataset_version


def run_pool(pool: Pool) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for interval in CONTRACT["bar_intervals"]:
        bars, bars_version = source_for_interval(pool, str(interval))
        every_ms = interval_minutes(str(interval)) * 60_000
        universe = pool.universe.filter(pl.col("timestamp").cast(pl.Int64) % every_ms == 0)
        label_paths = {}
        for horizon_bars in CONTRACT["forward_horizon_bars"]:
            horizon_minutes = interval_minutes(str(interval)) * int(horizon_bars)
            label = LabelDefinition(
                name=f"forward_{horizon_bars}_bars", signal_delay_bars=1,
                horizon=f"{horizon_minutes}m", entry_field="open", exit_field="open",
            )
            path = WORK / f"{pool.name}_{interval}_label_{horizon_bars}.parquet"
            announce(f"label {pool.name} {interval} +{horizon_bars} bars")
            sink_cache(compute_forward_returns(
                bars, universe, label, base_interval=str(interval),
                bars_dataset_version=bars_version, universe_version=pool.universe_version,
            ).frame, path)
            label_paths[int(horizon_bars)] = path
        for code in FORMULAS:
            announce(f"factor {pool.name} {interval} {code}")
            path = WORK / f"{pool.name}_{interval}_{code}.parquet"
            sink_cache(compute_factor(
                bars, universe, definition(code, str(interval)), base_interval=str(interval),
                bars_dataset_version=bars_version, universe_version=pool.universe_version,
            ).frame, path)
            factors = pl.scan_parquet(path).filter(
                (pl.col("timestamp") >= pool.start) & (pl.col("timestamp") < pool.end)
            )
            diagnostics = factor_diagnostics(factors, pool)
            for horizon_bars, label_path in label_paths.items():
                horizon_minutes = interval_minutes(str(interval)) * horizon_bars
                predictions = predictive_summaries(
                    factors, pl.scan_parquet(label_path), pool, horizon_minutes
                )
                for period, metrics in predictions.items():
                    role = pool.role if period.endswith("-ALL") else f"{pool.role}分段"
                    output.append({
                        "period": period, "role": role, "bar_interval": interval,
                        "factor_code": code, "horizon_bars": horizon_bars,
                        "horizon_natural": f"{horizon_minutes}m",
                        **diagnostics[period], **metrics,
                    })
            path.unlink()
        for path in label_paths.values():
            path.unlink()
    return output


def combine_dev_all(
    may_results: list[dict[str, object]], june_results: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Pool monthly sufficient statistics without reloading full minute panels."""

    def keyed(rows, period):
        return {
            (row["bar_interval"], row["factor_code"], row["horizon_bars"]): row
            for row in rows if row["period"] == period
        }

    may = keyed(may_results, "MAY-ALL")
    june = keyed(june_results, "JUNE-ALL")
    if may.keys() != june.keys():
        raise RuntimeError("May and June result identities differ")
    output = []
    for key in sorted(may):
        left, right = may[key], june[key]
        n1, n2 = int(left["timestamps"]), int(right["timestamps"])
        total = n1 + n2
        m1, m2 = float(left["mean_rank_ic"]), float(right["mean_rank_ic"])
        mean = (n1 * m1 + n2 * m2) / total
        s1 = abs(m1 / float(left["rank_ic_ir"])) if float(left["rank_ic_ir"]) != 0 else 0.0
        s2 = abs(m2 / float(right["rank_ic_ir"])) if float(right["rank_ic_ir"]) != 0 else 0.0
        variance = (
            n1 * (s1 * s1 + (m1 - mean) ** 2)
            + n2 * (s2 * s2 + (m2 - mean) ** 2)
        ) / total
        std = variance ** 0.5
        eligible = int(left["_eligible_observations"]) + int(right["_eligible_observations"])
        valid = int(left["_valid_factor_observations"]) + int(right["_valid_factor_observations"])
        t1, t2 = int(left["_turnover_timestamps"]), int(right["_turnover_timestamps"])
        output.append({
            "period": "DEV-ALL", "role": "开发总体",
            "bar_interval": key[0], "factor_code": key[1],
            "horizon_bars": key[2], "horizon_natural": left["horizon_natural"],
            "timestamps": total, "mean_rank_ic": mean,
            "rank_ic_ir": mean / std if std > 0 else float("nan"),
            "rank_ic_positive_fraction": (
                n1 * float(left["rank_ic_positive_fraction"])
                + n2 * float(right["rank_ic_positive_fraction"])
            ) / total,
            "q5_minus_q1": (
                n1 * float(left["q5_minus_q1"]) + n2 * float(right["q5_minus_q1"])
            ) / total,
            "average_sample_count": (
                n1 * float(left["average_sample_count"])
                + n2 * float(right["average_sample_count"])
            ) / total,
            "factor_coverage": valid / eligible,
            "mean_rank_turnover": (
                t1 * float(left["mean_rank_turnover"])
                + t2 * float(right["mean_rank_turnover"])
            ) / (t1 + t2),
            "_eligible_observations": eligible,
            "_valid_factor_observations": valid,
            "_turnover_timestamps": t1 + t2,
        })
    return output


def state_path() -> Path:
    return STUDY / "study_state.json"


def write_state(payload: dict[str, object]) -> None:
    STUDY.mkdir(parents=True, exist_ok=True)
    state_path().write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("dev", "holdout", "render"))
    args = parser.parse_args()
    STUDY.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    may, june, holdout = build_pools()
    if args.stage == "dev":
        started = perf_counter()
        may_results = run_pool(may)
        june_results = run_pool(june)
        results = (
            [row for row in may_results if row["period"] != "MAY-ALL"]
            + [row for row in june_results if row["period"] != "JUNE-ALL"]
            + combine_dev_all(may_results, june_results)
        )
        write_state({
            "status": "dev_complete", "study_id": STUDY.name,
            "contract": CONTRACT, "contract_sha256": CONTRACT_SHA256,
            "dev_results": results, "dev_wall_seconds": perf_counter() - started,
        })
        announce(f"DEV complete: {len(results)} results; contract {CONTRACT_SHA256}")
        return
    if not state_path().exists():
        raise RuntimeError("DEV state is missing")
    state = json.loads(state_path().read_text(encoding="utf-8"))
    if state.get("contract_sha256") != CONTRACT_SHA256:
        raise RuntimeError("frozen contract changed; holdout remains locked")
    if args.stage == "holdout":
        if state.get("status") != "dev_complete":
            raise RuntimeError("holdout is locked until DEV succeeds")
        started = perf_counter()
        state["holdout_results"] = run_pool(holdout)
        state["holdout_wall_seconds"] = perf_counter() - started
        state["status"] = "holdout_complete"
        write_state(state)
        announce(f"HOLDOUT complete: {len(state['holdout_results'])} results")
        return
    if state.get("status") != "holdout_complete":
        raise RuntimeError("report is locked until holdout succeeds")
    summary = {
        "status": "succeeded", "study_id": STUDY.name,
        "title": "国泰君安 Alpha191 趋势与动量：分段开发与七月未见验证",
        "contract": CONTRACT, "contract_sha256": CONTRACT_SHA256,
        "results": state["dev_results"] + state["holdout_results"],
        "runtime": {
            "dev_wall_seconds": state["dev_wall_seconds"],
            "holdout_wall_seconds": state["holdout_wall_seconds"],
        },
    }
    (STUDY / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    announce(f"report: {render_quick_only_study_report(STUDY)}")


if __name__ == "__main__":
    main()
