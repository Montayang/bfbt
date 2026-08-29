"""Run the frozen June/July GTJA191 quick-research study (no portfolio engine)."""

from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import polars as pl

from bianbt.config.factor import FactorDefinition, LabelDefinition
from bianbt.data.hashing import content_sha256
from bianbt.data.resample import resample_bars
from bianbt.factors.registry import compute_factor
from bianbt.labels.forward_returns import compute_forward_returns
from bianbt.reports.research_study import render_quick_only_study_report
from bianbt.research.ic import information_coefficient
from bianbt.research.quantiles import quantile_returns
from bianbt.research.turnover import factor_rank_turnover


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/backtest"
STUDY = DATA / "research_studies/gtja191_trend_momentum_quick_2026_06_07"
WORK = STUDY / "working"
UTC = timezone.utc

MONTHS = {
    "2026-06": {
        "start": datetime(2026, 6, 1, tzinfo=UTC),
        "end": datetime(2026, 7, 1, tzinfo=UTC),
        "bars_version": "a05-d2b005eb468289e20e149672",
        "analysis_id": "analysis-c09cba7b63809ffb6a408d49",
        "universe_version": "a06-90cc53319555a74b4d251bac",
        "dataset_manifest_sha256": "4de74fc0701ae196f44bfe9cfddbd4044c4e8f03ecc13772edadefeb61853b95",
    },
    "2026-07": {
        "start": datetime(2026, 7, 1, tzinfo=UTC),
        "end": datetime(2026, 8, 1, tzinfo=UTC),
        "bars_version": "a05-adf9e699b3ead5c15f6ae597",
        "analysis_id": "analysis-0495ebb9842e093bd8890a0c",
        "universe_version": "a06-de2fa6299f92d9d49dc1def2",
        "dataset_manifest_sha256": "32c72c0de315df3c74982e767cd558f39b37d4ad7de73f5feafa81972928faf5",
    },
}

PERIODS = {
    "2026-06-D1": {
        "month": "2026-06", "role": "发现",
        "start": datetime(2026, 6, 1, tzinfo=UTC),
        "end": datetime(2026, 6, 15, tzinfo=UTC),
    },
    "2026-06-S1": {
        "month": "2026-06", "role": "六月稳定性",
        "start": datetime(2026, 6, 15, tzinfo=UTC),
        "end": datetime(2026, 7, 1, tzinfo=UTC),
    },
    "2026-07-V1": {
        "month": "2026-07", "role": "纯验证",
        "start": datetime(2026, 7, 1, tzinfo=UTC),
        "end": datetime(2026, 8, 1, tzinfo=UTC),
    },
}

FORMULAS = {
    "Alpha18": "CLOSE / DELAY(CLOSE,5)",
    "Alpha20": "(CLOSE-DELAY(CLOSE,6))/DELAY(CLOSE,6)*100",
    "Alpha24": "SMA(CLOSE-DELAY(CLOSE,5),5,1)",
    "Alpha31": "(CLOSE-MEAN(CLOSE,12))/MEAN(CLOSE,12)*100",
    "Alpha40_quote": "SUM(up quote_volume,26)/SUM(non-up quote_volume,26)*100",
    "Alpha40_base": "SUM(up base_volume,26)/SUM(non-up base_volume,26)*100",
    "Alpha53": "COUNT(CLOSE>DELAY(CLOSE,1),12)/12*100",
    "Alpha66": "(CLOSE-MEAN(CLOSE,6))/MEAN(CLOSE,6)*100",
    "Alpha71": "(CLOSE-MEAN(CLOSE,24))/MEAN(CLOSE,24)*100",
    "Alpha88": "(CLOSE-DELAY(CLOSE,20))/DELAY(CLOSE,20)*100",
    "Alpha89": "2*(SMA(CLOSE,13,2)-SMA(CLOSE,27,2)-SMA(diff,10,2))",
    "Alpha112": "(SUM(up change,12)-SUM(abs down change,12))/(sum both)*100",
    "Alpha151": "SMA(CLOSE-DELAY(CLOSE,20),20,1)",
}

CONTRACT = {
    "source": "GTJA short-cycle price-volume report, SHA256 863f62c2e23bd87ddb42b8338c8fe2b0276d94260ac985e1a0edfff318693c6c",
    "factors": FORMULAS,
    "bar_intervals": ["1m", "5m", "15m"],
    "forward_horizon_bars": [1, 5, 20],
    "label_timing": "signal at bar close; enter next bar open; exit open after N bars",
    "preprocess": "cross-sectional winsorize 1%-99%, then z-score",
    "quantiles": 5,
    "volume_policy": "quote_volume primary; base volume Alpha40 control only",
    "window_semantics": "all formula windows and labels are literal source-bar counts",
    "validation": "2026-07-01 <= signal < 2026-08-01; frozen before July read",
}
CONTRACT_SHA256 = content_sha256(CONTRACT)


def announce(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", flush=True)


def sink_cache(frame: pl.LazyFrame, path: Path) -> None:
    """Publish a parquet cache atomically so interrupted writes are never reused."""

    partial = path.with_name(f".{path.name}.partial")
    if partial.exists():
        partial.unlink()
    frame.sink_parquet(partial, mkdir=True)
    partial.replace(path)


def scan_bars(version: str) -> pl.LazyFrame:
    pattern = str(
        DATA / "datasets/binance_usdm_perpetual_1m/normalized/bars/schema=v1"
        / f"dataset_version={version}/**/*.parquet"
    )
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        raise RuntimeError(f"missing bars dataset {version}")
    return pl.scan_parquet(paths, hive_partitioning=False)


def scan_universe(meta: dict[str, object]) -> pl.LazyFrame:
    root = DATA / "reuse/analysis" / str(meta["analysis_id"])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    paths = [root / item["path"] for item in manifest["tables"]["universe"]]
    return pl.scan_parquet(paths, hive_partitioning=False).filter(
        (pl.col("timestamp") >= meta["start"]) & (pl.col("timestamp") < meta["end"])
    )


def interval_minutes(interval: str) -> int:
    return int(interval.removesuffix("m"))


def source_bars(month: str, interval: str) -> tuple[pl.LazyFrame, str]:
    meta = MONTHS[month]
    base = scan_bars(str(meta["bars_version"]))
    if interval == "1m":
        return base, str(meta["bars_version"])
    cache = WORK / f"{month}_{interval}_bars.parquet"
    result = resample_bars(
        base, dataset_name="bars", source_interval="1m", target_interval=interval,
        source_dataset_version=str(meta["bars_version"]),
    )
    if not cache.exists():
        announce(f"resample {month} {interval}")
        sink_cache(result.frame, cache)
    return pl.scan_parquet(cache), result.dataset_version


def definition(code: str, interval: str) -> FactorDefinition:
    number = int(code[5:7]) if code.startswith("Alpha40_") else int(code[5:])
    parameters = {}
    if code == "Alpha40_quote":
        parameters["volume_field"] = "quote_volume"
    elif code == "Alpha40_base":
        parameters["volume_field"] = "volume"
    return FactorDefinition.model_validate({
        "name": f"gtja_alpha{number:03d}", "version": "v1",
        "parameters": parameters, "compute_interval": interval,
        "preprocess": [
            {"name": "winsorize", "method": "quantile", "lower": 0.01, "upper": 0.99},
            {"name": "zscore"},
        ],
    })


def factor_diagnostics(factors: pl.LazyFrame) -> dict[str, float]:
    counts, turnover = pl.collect_all(
        [
            factors.select(
                pl.len().alias("eligible"),
                pl.col("is_valid").sum().alias("valid"),
            ),
            factor_rank_turnover(factors).select(
                pl.col("rank_turnover").mean().alias("mean_rank_turnover")
            ),
        ],
        engine="streaming",
    )
    eligible = int(counts[0, "eligible"])
    return {
        "factor_coverage": int(counts[0, "valid"]) / eligible if eligible else float("nan"),
        "mean_rank_turnover": float(turnover[0, "mean_rank_turnover"]),
    }


def predictive_summary(
    factors: pl.LazyFrame, labels: pl.LazyFrame
) -> dict[str, object]:
    ic, quantiles = pl.collect_all(
        [information_coefficient(factors, labels), quantile_returns(factors, labels, quantiles=5)],
        engine="streaming",
    )
    clean = ic.filter(pl.col("rank_ic").is_finite())
    qmeans = quantiles.group_by("quantile").agg(
        pl.col("mean_forward_return").mean().alias("mean")
    )
    qmap = {int(row["quantile"]): float(row["mean"]) for row in qmeans.to_dicts()}
    mean = float(clean["rank_ic"].mean()) if clean.height else float("nan")
    standard_deviation = float(clean["rank_ic"].std(ddof=0)) if clean.height else float("nan")
    return {
        "timestamps": clean.height,
        "mean_rank_ic": mean,
        "rank_ic_ir": mean / standard_deviation if standard_deviation > 0 else float("nan"),
        "rank_ic_positive_fraction": float((clean["rank_ic"] > 0).mean()) if clean.height else float("nan"),
        "q5_minus_q1": qmap.get(5, float("nan")) - qmap.get(1, float("nan")),
        "average_sample_count": float(clean["sample_count"].mean()) if clean.height else float("nan"),
    }


def run_month(month: str) -> list[dict[str, object]]:
    meta = MONTHS[month]
    output: list[dict[str, object]] = []
    for interval in CONTRACT["bar_intervals"]:
        bars, bars_version = source_bars(month, str(interval))
        every_ms = interval_minutes(str(interval)) * 60_000
        universe = scan_universe(meta).filter(pl.col("timestamp").cast(pl.Int64) % every_ms == 0)
        label_paths: dict[int, Path] = {}
        for horizon_bars in CONTRACT["forward_horizon_bars"]:
            horizon_minutes = interval_minutes(str(interval)) * int(horizon_bars)
            label = LabelDefinition(
                name=f"forward_{horizon_bars}_bars", signal_delay_bars=1,
                horizon=f"{horizon_minutes}m", entry_field="open", exit_field="open",
            )
            label_path = WORK / f"{month}_{interval}_label_{horizon_bars}.parquet"
            if not label_path.exists():
                announce(f"label {month} {interval} +{horizon_bars} bars")
                compute_forward_returns(
                    bars, universe, label, base_interval=str(interval),
                    bars_dataset_version=bars_version,
                    universe_version=str(meta["universe_version"]),
                ).frame.pipe(lambda frame: sink_cache(frame, label_path))
            label_paths[int(horizon_bars)] = label_path

        for code in FORMULAS:
            announce(f"factor {month} {interval} {code}")
            factor_path = WORK / f"{month}_{interval}_{code}.parquet"
            if not factor_path.exists():
                factor_result = compute_factor(
                    bars, universe, definition(code, str(interval)),
                    base_interval=str(interval), bars_dataset_version=bars_version,
                    universe_version=str(meta["universe_version"]),
                )
                sink_cache(factor_result.frame, factor_path)
            factor_scan = pl.scan_parquet(factor_path)
            for period, period_meta in PERIODS.items():
                if period_meta["month"] != month:
                    continue
                start, end = period_meta["start"], period_meta["end"]
                period_factors = factor_scan.filter(
                    (pl.col("timestamp") >= start) & (pl.col("timestamp") < end)
                )
                diagnostics = factor_diagnostics(period_factors)
                for horizon_bars, label_path in label_paths.items():
                    labels = pl.scan_parquet(label_path).filter(
                        (pl.col("timestamp") >= start)
                        & (pl.col("timestamp") < end)
                        & (pl.col("exit_time") <= end)
                    )
                    metrics = predictive_summary(period_factors, labels)
                    output.append({
                        "period": period, "role": period_meta["role"],
                        "bar_interval": interval, "factor_code": code,
                        "horizon_bars": horizon_bars,
                        "horizon_natural": f"{interval_minutes(str(interval)) * horizon_bars}m",
                        **diagnostics, **metrics,
                    })
            factor_path.unlink()
        for label_path in label_paths.values():
            label_path.unlink()
    return output


def read_state() -> dict[str, object] | None:
    path = STUDY / "study_state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_state(value: dict[str, object]) -> None:
    STUDY.mkdir(parents=True, exist_ok=True)
    (STUDY / "study_state.json").write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("june", "july", "render"))
    args = parser.parse_args()
    STUDY.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    if args.stage == "june":
        started = perf_counter()
        results = run_month("2026-06")
        write_state({
            "status": "june_complete", "study_id": STUDY.name,
            "contract": CONTRACT, "contract_sha256": CONTRACT_SHA256,
            "june_results": results, "june_wall_seconds": perf_counter() - started,
        })
        announce(f"June complete: {len(results)} results; contract {CONTRACT_SHA256}")
        return
    state = read_state()
    if state is None:
        raise RuntimeError("study state is missing")
    if state.get("contract_sha256") != CONTRACT_SHA256:
        raise RuntimeError("frozen contract changed after June; July remains locked")
    if args.stage == "july":
        if state.get("status") != "june_complete":
            raise RuntimeError("July is locked until the June stage succeeds")
        started = perf_counter()
        state["july_results"] = run_month("2026-07")
        state["july_wall_seconds"] = perf_counter() - started
        state["status"] = "july_complete"
        write_state(state)
        announce(f"July validation complete: {len(state['july_results'])} results")
        return
    if state.get("status") != "july_complete":
        raise RuntimeError("report is locked until July validation succeeds")
    summary = {
        "status": "succeeded", "study_id": STUDY.name,
        "title": "国泰君安 Alpha191 趋势与动量因子快速研究",
        "contract": CONTRACT, "contract_sha256": CONTRACT_SHA256,
        "periods": PERIODS, "datasets": MONTHS,
        "results": state["june_results"] + state["july_results"],
        "runtime": {
            "june_wall_seconds": state["june_wall_seconds"],
            "july_wall_seconds": state["july_wall_seconds"],
        },
    }
    (STUDY / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report = render_quick_only_study_report(STUDY)
    announce(f"report: {report}")


if __name__ == "__main__":
    main()
