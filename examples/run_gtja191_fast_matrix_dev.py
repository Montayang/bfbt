"""Run the QR-v1 promoted GTJA191 signals through the Fast Matrix round."""

from __future__ import annotations

import argparse
import fcntl
import glob
import html
import json
import os
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

import polars as pl

from bfbt.artifacts.matrix import MatrixResearchStore
from bfbt.config.backtest import BacktestConfig, PortfolioConfig
from bfbt.data.hashing import content_sha256, sha256_file
from bfbt.data.manifests import DatasetSnapshotManifest, load_manifest
from bfbt.engine.fast_matrix.chunked import run_fast_matrix_chunked
from bfbt.engine.fast_matrix.target_schedule import build_target_schedule
from bfbt.factors.registry import compute_factor
from bfbt.portfolio.constraints import construct_portfolio
from bfbt.reports.matrix import matrix_report_metrics

# The examples directory is deliberately executable without installing it as a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_gtja191_quick_research import FORMULAS, definition, sink_cache  # noqa: E402
from run_gtja191_segmented_quick_research import (  # noqa: E402
    MAY_1,
    JUNE_1,
    JULY_1,
    Pool,
    build_pools,
    source_for_interval,
)


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/backtest"
MARKET = DATA / "datasets/binance_usdm_perpetual_1m"
STUDY = DATA / "research_studies/gtja191_fast_matrix_dev_2026_05_06"
WORK = STUDY / "working"
RUNS = DATA / "research_runs"
STATE = STUDY / "state.json"
UTC = timezone.utc
UTC_MS = pl.Datetime("ms", "UTC")

STUDY_ID = "gtja191_fast_matrix_dev_2026_05_06"
QR_STUDY_ID = "gtja191_segmented_dev_holdout_2026_05_07"
QR_RULE = "QR-v1"

# A promoted signal remains factor x K-line. Each passed prediction horizon supplies
# one economically matching rebalance schedule, but does not create a new factor.
PROMOTED = {
    ("Alpha112", "1m"): (5, 20),
    ("Alpha18", "15m"): (5, 20),
    ("Alpha20", "1m"): (5, 20),
    ("Alpha31", "1m"): (1, 5, 20),
    ("Alpha31", "5m"): (1, 5, 20),
    ("Alpha31", "15m"): (1, 5, 20),
    ("Alpha66", "1m"): (1, 5, 20),
    ("Alpha66", "5m"): (1, 5),
    ("Alpha66", "15m"): (1, 5, 20),
    ("Alpha71", "1m"): (5, 20),
    ("Alpha71", "5m"): (5, 20),
    ("Alpha71", "15m"): (5, 20),
}
COSTS = {
    "zero": {
        "fee": {"model": "zero"},
        "slippage": {"model": "zero"},
    },
    "realistic": {
        "fee": {"model": "fixed_bps", "taker_bps": 4.0},
        "slippage": {"model": "fixed_bps", "bps": 1.0},
    },
}
CONTRACT = {
    "study_id": STUDY_ID,
    "source_quick_study": QR_STUDY_ID,
    "screening_rule": QR_RULE,
    "development_path": "one continuous account from 2026-05-01 through 2026-07-01",
    "signals": [
        {
            "factor_code": code,
            "bar_interval": interval,
            "direction": -1,
            "passed_horizon_bars": list(horizons),
        }
        for (code, interval), horizons in PROMOTED.items()
    ],
    "portfolio": "direction-adjusted top 20% long / bottom 20% short; equal weight; gross 1; net 0",
    "schedule": "one schedule per passed horizon; rebalance every horizon source bars",
    "timing": "source-bar close signal; next executable 1m open fill",
    "missing_trade_bar": "do not open without a real 1m open; carry an existing quantity and defer its adjustment",
    "valuation": "1m trade close",
    "costs": {
        "zero": "zero fee and slippage, observed funding retained",
        "realistic": "4 bps taker fee + 1 bp slippage, observed funding retained",
    },
    "funding_missing_policy": "assume_zero means no settlement event at that timestamp, not missing archives",
    "initial_equity_usdt": 10_000.0,
    "excluded": ["July", "cold-start segments", "quantile sensitivity", "Event engine"],
}
CONTRACT_SHA256 = content_sha256(CONTRACT)


def announce(message: str) -> None:
    print(f"[{datetime.now(UTC).isoformat()}] {message}", flush=True)


@contextmanager
def cache_lock(path: Path):
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def snapshot(path: Path) -> DatasetSnapshotManifest:
    value = load_manifest(path, "dataset")
    if not isinstance(value, DatasetSnapshotManifest):
        raise TypeError(f"not a dataset snapshot: {path}")
    return value


def reference_version(value: DatasetSnapshotManifest, name: str) -> str:
    return next(item.dataset_version for item in value.datasets if item.dataset_name == name)


def scan_normalized(dataset: str, version: str) -> pl.LazyFrame:
    pattern = str(
        MARKET / "normalized" / dataset / "schema=v1"
        / f"dataset_version={version}/**/*.parquet"
    )
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        raise RuntimeError(f"missing normalized {dataset} {version}")
    return pl.scan_parquet(paths, hive_partitioning=False)


def execution_inputs(versions: dict[str, str]) -> tuple[pl.LazyFrame, pl.LazyFrame]:
    """Build once-per-day execution caches shared by every matrix candidate."""

    market_root = WORK / "execution_market"
    market_root.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    cursor = MAY_1
    while cursor < JULY_1:
        end = cursor + timedelta(days=1)
        path = market_root / f"bars_{cursor:%Y_%m_%d}.parquet"
        if not path.exists():
            version = versions["may_bars"] if cursor < JUNE_1 else versions["june_bars"]
            announce(f"cache execution bars {cursor:%Y-%m-%d}")
            sink_cache(
                scan_normalized("bars", version)
                .filter((pl.col("open_time") >= cursor) & (pl.col("open_time") < end))
                .select("open_time", "close_time", "symbol", "open", "close")
                .sort(["open_time", "symbol"]),
                path,
            )
        paths.append(path)
        cursor = end
    funding_path = market_root / "funding_2026_05_06.parquet"
    if not funding_path.exists():
        announce("cache May-June funding events")
        sink_cache(
            pl.concat([
                scan_normalized("funding", versions["may_funding"]).filter(
                    (pl.col("funding_time") > MAY_1) & (pl.col("funding_time") < JUNE_1)
                ),
                scan_normalized("funding", versions["june_funding"]).filter(
                    (pl.col("funding_time") >= JUNE_1) & (pl.col("funding_time") <= JULY_1)
                ),
            ]).select("funding_time", "symbol", "funding_rate", "mark_price").sort(
                ["funding_time", "symbol"]
            ),
            funding_path,
        )
    return (
        pl.scan_parquet(paths, hive_partitioning=False),
        pl.scan_parquet(funding_path, hive_partitioning=False),
    )


def interval_minutes(value: str) -> int:
    return int(value.removesuffix("m"))


def duration_label(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def load_state() -> dict[str, object]:
    if not STATE.exists():
        return {"study_id": STUDY_ID, "contract_sha256": CONTRACT_SHA256, "attempts": {}}
    value = json.loads(STATE.read_text(encoding="utf-8"))
    if value.get("contract_sha256") != CONTRACT_SHA256:
        if all(
            attempt.get("status") != "succeeded"
            for attempt in value.get("attempts", {}).values()
        ):
            return {"study_id": STUDY_ID, "contract_sha256": CONTRACT_SHA256, "attempts": {}}
        raise RuntimeError("existing state belongs to a different frozen contract")
    return value


def save_state(value: dict[str, object]) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE.with_name("state.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        merged = dict(value)
        merged_attempts = dict(value.get("attempts", {}))
        if STATE.exists():
            disk = json.loads(STATE.read_text(encoding="utf-8"))
            if disk.get("contract_sha256") == CONTRACT_SHA256:
                disk_attempts = dict(disk.get("attempts", {}))
                for key, attempt in merged_attempts.items():
                    existing = disk_attempts.get(key, {})
                    if (
                        existing.get("status") == "succeeded"
                        and attempt.get("status") != "succeeded"
                    ):
                        continue
                    disk_attempts[key] = attempt
                merged_attempts = disk_attempts
        merged["attempts"] = merged_attempts
        value["attempts"] = merged_attempts
        temporary = STATE.with_name(f".state.{os.getpid()}.json.partial")
        temporary.write_text(
            json.dumps(merged, default=str, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(STATE)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def recover_published(state: dict[str, object]) -> None:
    """Recover a run published immediately before an interrupted state write."""

    attempts: dict[str, dict[str, object]] = state["attempts"]
    prefix = f"{STUDY_ID}|"
    for path in RUNS.glob("fm-*/manifest.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        index_key = str(manifest.get("research_context", {}).get("index_key", ""))
        if not index_key.startswith(prefix):
            continue
        key = index_key.removeprefix(prefix)
        if attempts.get(key, {}).get("status") == "succeeded":
            continue
        code, interval, horizon_text, cost = key.split("|")
        metrics_path = path.parent / manifest["files"]["metrics"]["path"]
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        attempts[key] = {
            **attempts.get(key, {}),
            "index_key": index_key, "factor_code": code,
            "bar_interval": interval, "direction": -1,
            "horizon_bars": int(horizon_text.removeprefix("h")),
            "rebalance_interval": duration_label(
                interval_minutes(interval) * int(horizon_text.removeprefix("h"))
            ),
            "cost_variant": cost, "status": "succeeded",
            "run_id": manifest["run_id"], "metrics": metrics,
            "recovered_from_published_run": True,
        }


def factor_cache(pool: Pool, code: str, interval: str) -> tuple[Path, str]:
    path = WORK / f"factor_{pool.name}_{interval}_{code}.parquet"
    bars, bars_version = source_for_interval(pool, interval)
    result = compute_factor(
        bars,
        pool.universe.filter(
            pl.col("timestamp").cast(pl.Int64)
            % (interval_minutes(interval) * 60_000)
            == 0
        ),
        definition(code, interval),
        base_interval=interval,
        bars_dataset_version=bars_version,
        universe_version=pool.universe_version,
    )
    with cache_lock(path):
        if not path.exists():
            announce(f"cache factor {pool.name} {code} {interval}")
            sink_cache(result.frame, path)
    return path, result.factor_version


def monthly_targets(
    pool: Pool, code: str, interval: str, horizon: int
) -> Path:
    path = WORK / f"targets_v2_{pool.name}_{interval}_{code}_h{horizon}.parquet"
    with cache_lock(path):
        if path.exists():
            return path
        factor_path, factor_version = factor_cache(pool, code, interval)
        rebalance_minutes = interval_minutes(interval) * horizon
        source_signal_id = f"{STUDY_ID}|{code}|{interval}|h{horizon}|direction=-1"
        portfolio_identity = {
            "study_contract": CONTRACT_SHA256,
            "source_signal_id": source_signal_id,
            "month": pool.name,
            "factor_version": factor_version,
            "universe_version": pool.universe_version,
            "portfolio": CONTRACT["portfolio"],
        }
        portfolio_version = f"fm-portfolio-{content_sha256(portfolio_identity)[:24]}"
        scores = (
            pl.scan_parquet(factor_path)
            .filter(
                (pl.col("timestamp") >= pool.start)
                & (pl.col("timestamp") < pool.end)
                & (pl.col("timestamp").cast(pl.Int64) % (rebalance_minutes * 60_000) == 0)
            )
            .with_columns((-pl.col("value")).alias("value"))
        )
        portfolio = construct_portfolio(
            scores,
            PortfolioConfig(
                construction="long_short_quantile",
                long_quantile=0.2,
                short_quantile=0.2,
                weighting="equal",
                gross_exposure=1.0,
                net_exposure=0.0,
            ),
            factor_version=factor_version,
            universe_version=pool.universe_version,
        )
        executable = pool.bars.filter(
            (pl.col("open_time") >= pool.start) & (pl.col("open_time") < pool.end)
        ).select(
            pl.col("open_time").alias("fill_time"), "symbol"
        )
        target = (
            portfolio.frame.with_columns(
                (pl.col("signal_time") + pl.duration(minutes=1)).alias("fill_time"),
                pl.lit(source_signal_id).alias("source_signal_id"),
                pl.lit(portfolio_version).alias("portfolio_version"),
            )
            .filter(pl.col("fill_time") < JULY_1)
            .join(executable, on=["fill_time", "symbol"], how="inner")
            .select(
                "signal_time", "fill_time", "symbol", "target_weight",
                "source_signal_id", "factor_version", "universe_version", "portfolio_version",
            )
        )
        announce(f"cache targets {pool.name} {code} {interval} h{horizon}")
        sink_cache(target, path)
    return path


def target_schedule(pools: tuple[Pool, Pool], code: str, interval: str, horizon: int, parent: str):
    paths = [monthly_targets(pool, code, interval, horizon) for pool in pools]
    frame = pl.concat([pl.scan_parquet(path) for path in paths]).with_columns(
        pl.col("symbol").cast(pl.Categorical),
        pl.col("source_signal_id").cast(pl.Categorical),
        pl.col("factor_version").cast(pl.Categorical),
        pl.col("universe_version").cast(pl.Categorical),
        pl.col("portfolio_version").cast(pl.Categorical),
    ).collect(engine="streaming")
    times = tuple(frame["fill_time"].unique().sort().to_list())
    return build_target_schedule(
        frame,
        rebalance_times=times,
        parent_manifest_sha256=parent,
    )


def config(interval: str, horizon: int, cost: str, dataset_identity: str) -> BacktestConfig:
    rebalance_minutes = interval_minutes(interval) * horizon
    values = COSTS[cost]
    return BacktestConfig.model_validate({
        "config_version": "v2",
        "engine": {"backend": "fast_matrix", "purpose": "research"},
        "run": {
            "name": f"{STUDY_ID}-{interval}-h{horizon}-{cost}",
            "start": MAY_1,
            "end": JULY_1,
            "dataset_version": dataset_identity,
        },
        "schedule": {
            "factor_interval": interval,
            "rebalance_interval": duration_label(rebalance_minutes),
            "signal_delay_bars": 1,
        },
        # The target schedule is already constructed from exact 20% tails. This
        # non-empty rank-set contract only identifies the compatible V2 capability.
        "portfolio": {
            "selection": {"long": {"ranks": [1]}, "short": {"ranks": [2]}},
            "sizing": {
                "mode": "target_weight", "weighting": "equal",
                "target_gross_exposure": 1.0, "target_net_exposure": 0.0,
            },
        },
        "execution": {
            "fee": values["fee"],
            "slippage": values["slippage"],
            "funding": {"enabled": True, "missing_policy": "assume_zero"},
        },
        "valuation": {"price": "trade_close"},
        "risk": {
            "leverage": 5.0, "enforce_liquidation": False,
            "evaluation_interval": "1m", "trigger_price": "trade",
            "fill_model": "next_bar_open", "gap_policy": "worse_executable",
            "intrabar_conflict": "worst_case", "cooldown_bars": 0,
            "reentry_policy": "next_scheduled_rebalance",
        },
        "capital": {"initial_equity": 10_000.0},
        "performance": {
            "mode": "chunked", "chunk_interval": "1d",
            "max_input_rows_per_chunk": 1_500_000,
            "max_process_rss_mib": 7_000,
            "resume_policy": "resume",
        },
    })


def _pct(value: object) -> str:
    return "—" if value is None else f"{float(value):.2%}"


def render_report(summary: dict[str, object]) -> Path:
    rows = []
    for item in summary["attempts"]:
        status = str(item["status"])
        run_id = str(item.get("run_id", "—"))
        link = (
            f"<a href='../../research_runs/{html.escape(run_id)}/report.html'>{html.escape(run_id)}</a>"
            if status == "succeeded" else "—"
        )
        metrics = item.get("metrics", {})
        key = html.escape(str(item["index_key"]))
        search = html.escape(" ".join(str(item.get(name, "")) for name in (
            "index_key", "factor_code", "bar_interval", "horizon_bars", "cost_variant", "run_id", "status"
        )).lower())
        rows.append(
            f"<tr data-search='{search}' data-factor='{item['factor_code']}' data-interval='{item['bar_interval']}' "
            f"data-cost='{item['cost_variant']}' data-status='{status}'>"
            f"<td class='key' title='{key}'><code>{key}</code></td>"
            f"<td>{item['factor_code']}</td><td>{item['bar_interval']}</td>"
            f"<td class='num'>{item['horizon_bars']}</td><td>{item['rebalance_interval']}</td>"
            f"<td>{item['cost_variant']}</td><td>{status}</td><td>{link}</td>"
            f"<td class='num'>{_pct(metrics.get('total_return'))}</td>"
            f"<td class='num'>{_pct(metrics.get('max_drawdown'))}</td>"
            f"<td class='num'>{float(metrics.get('cumulative_turnover', 0)):.1f}</td>"
            f"<td class='num'>{float(metrics.get('fee_amount', 0)):.2f}</td>"
            f"<td class='num'>{float(metrics.get('slippage_amount', 0)):.2f}</td>"
            f"<td class='num'>{float(metrics.get('funding_amount', 0)):.2f}</td>"
            f"<td title='{html.escape(str(item.get('error', '')))}'>{html.escape(str(item.get('error', ''))[:100])}</td></tr>"
        )
    filters = {
        "factor": sorted({str(item["factor_code"]) for item in summary["attempts"]}),
        "interval": sorted({str(item["bar_interval"]) for item in summary["attempts"]}),
        "cost": sorted({str(item["cost_variant"]) for item in summary["attempts"]}),
        "status": sorted({str(item["status"]) for item in summary["attempts"]}),
    }
    selects = "".join(
        f"<select id='{name}'><option value=''>{label}：全部</option>" + "".join(
            f"<option value='{html.escape(value)}'>{html.escape(value)}</option>" for value in values
        ) + "</select>"
        for name, label, values in (
            ("factor", "因子", filters["factor"]), ("interval", "K线", filters["interval"]),
            ("cost", "成本", filters["cost"]), ("status", "状态", filters["status"]),
        )
    )
    contract = html.escape(json.dumps(summary["contract"], ensure_ascii=False, sort_keys=True))
    page = f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>GTJA191 Fast Matrix</title>
<style>:root{{--ink:#10231d;--muted:#63756e;--line:#d6e3dd;--accent:#007c68;--panel:#f3f8f6}}*{{box-sizing:border-box}}body{{margin:0;background:#f6f8f7;color:var(--ink);font:14px/1.5 system-ui,sans-serif}}main{{max-width:1600px;margin:22px auto;padding:0 18px}}header,.panel{{background:white;border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}}h1{{margin:2px 0}}.eyebrow{{color:var(--accent);font-weight:800;letter-spacing:.12em}}.muted{{color:var(--muted)}}.toolbar{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}input,select{{min-height:38px;border:1px solid var(--line);border-radius:8px;background:white;padding:7px 10px}}input{{flex:1;min-width:280px}}.table-wrap{{overflow:auto;max-height:75vh;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:separate;border-spacing:0;width:100%;white-space:nowrap}}th,td{{border-bottom:1px solid var(--line);padding:8px 9px;text-align:left}}th{{position:sticky;top:0;background:#edf5f1;cursor:pointer;z-index:1}}td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}tbody tr:nth-child(even){{background:#f8fbfa}}td.key{{max-width:420px;overflow:hidden;text-overflow:ellipsis}}a{{color:var(--accent);font-weight:650;text-decoration:none}}</style>
<body><main><header><div class='eyebrow'>PORTFOLIO RESEARCH</div><h1>GTJA191 晋级信号 / Fast Matrix</h1><p>五月至六月连续账户路径；本页是研究结果，不是 Event 正式回测或实盘许可。</p></header>
<section class='panel'><p><strong>执行口径：</strong>方向调整后高 20% 做多、低 20% 做空，等权，总敞口 100%、净敞口 0%；下一分钟开盘成交，1m 收盘估值，真实资金费。每个通过预测根数映射为对应的调仓周期。</p><p class='muted'>冻结合同：<code>{contract}</code></p><div class='toolbar'><input id='q' placeholder='搜索索引键、因子、K线、成本、Run ID…'>{selects}</div>
<div class='table-wrap'><table id='results'><thead><tr><th>索引键</th><th>因子</th><th>K线</th><th class='num'>预测根数</th><th>调仓</th><th>成本</th><th>状态</th><th>Run ID</th><th class='num'>收益</th><th class='num'>最大回撤</th><th class='num'>累计换手</th><th class='num'>手续费</th><th class='num'>滑点</th><th class='num'>Funding</th><th>错误</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></section></main>
<script>const q=document.getElementById('q'),table=document.getElementById('results'),ids=['factor','interval','cost','status'];function apply(){{const query=q.value.trim().toLowerCase();const fs=Object.fromEntries(ids.map(id=>[id,document.getElementById(id).value]));for(const row of table.tBodies[0].rows){{let ok=!query||row.dataset.search.includes(query);for(const [id,v] of Object.entries(fs))if(v&&row.dataset[id]!==v)ok=false;row.hidden=!ok}}}}q.addEventListener('input',apply);for(const id of ids)document.getElementById(id).addEventListener('change',apply);for(const th of table.tHead.rows[0].cells)th.addEventListener('click',()=>{{const i=th.cellIndex,asc=th.dataset.order!=='asc',rows=[...table.tBodies[0].rows];rows.sort((a,b)=>a.cells[i].textContent.localeCompare(b.cells[i].textContent,undefined,{{numeric:true}})*(asc?1:-1));for(const row of rows)table.tBodies[0].appendChild(row);th.dataset.order=asc?'asc':'desc'}});</script></body></html>"""
    destination = STUDY / "fast_matrix.html"
    destination.write_text(page, encoding="utf-8")
    return destination


def run(
    *, limit: int | None = None, only_cost: str | None = None,
    minimum_rebalance_minutes: int | None = None,
) -> None:
    STUDY.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    state = load_state()
    recover_published(state)
    save_state(state)
    attempts: dict[str, dict[str, object]] = state["attempts"]

    may_snapshot_path = MARKET / "dataset-snapshot-2026-05-research.json"
    june_snapshot_path = MARKET / "dataset-snapshot-2026-06.json"
    may_snapshot, june_snapshot = snapshot(may_snapshot_path), snapshot(june_snapshot_path)
    versions = {
        "may_bars": reference_version(may_snapshot, "bars"),
        "may_funding": reference_version(may_snapshot, "funding"),
        "june_bars": reference_version(june_snapshot, "bars"),
        "june_funding": reference_version(june_snapshot, "funding"),
    }
    dataset_identity = f"fm-market-{content_sha256(versions)[:24]}"
    parent = content_sha256({
        "contract_sha256": CONTRACT_SHA256,
        "may_snapshot_sha256": sha256_file(may_snapshot_path),
        "june_snapshot_sha256": sha256_file(june_snapshot_path),
    })
    market_identity = content_sha256({"dataset_identity": dataset_identity, **versions})
    may, june, _ = build_pools()
    trade, funding = execution_inputs(versions)
    store = MatrixResearchStore(RUNS)

    schedules = [
        (code, interval, horizon)
        for (code, interval), horizons in PROMOTED.items()
        for horizon in horizons
    ]
    # Cheaper schedules establish broad evidence first; minute-by-minute schedules
    # run last because they create the largest immutable target artifacts.
    schedules.sort(key=lambda item: interval_minutes(item[1]) * item[2], reverse=True)
    if minimum_rebalance_minutes is not None:
        schedules = [
            item for item in schedules
            if interval_minutes(item[1]) * item[2] >= minimum_rebalance_minutes
        ]
    completed_now = 0
    for code, interval, horizon in schedules:
        outstanding = [
            cost for cost in COSTS
            if only_cost is None or cost == only_cost
        ]
        outstanding = [
            cost for cost in outstanding
            if attempts.get(f"{code}|{interval}|h{horizon}|{cost}", {}).get("status")
            not in {"succeeded", "failed"}
        ]
        if not outstanding:
            continue
        if limit is not None and completed_now >= limit:
            break
        announce(f"build schedule {code} {interval} h{horizon}")
        schedule = target_schedule((may, june), code, interval, horizon, parent)
        for cost in outstanding:
            if limit is not None and completed_now >= limit:
                break
            key = f"{code}|{interval}|h{horizon}|{cost}"
            started = perf_counter()
            announce(f"run {key} targets={schedule.frame.height:,} rebalances={len(schedule.rebalance_times):,}")
            item: dict[str, object] = {
                "index_key": f"{STUDY_ID}|{key}", "factor_code": code,
                "bar_interval": interval, "direction": -1,
                "horizon_bars": horizon,
                "rebalance_interval": duration_label(interval_minutes(interval) * horizon),
                "cost_variant": cost, "status": "running",
            }
            attempts[key] = item
            save_state(state)
            try:
                resolved = config(interval, horizon, cost, dataset_identity)
                result = run_fast_matrix_chunked(
                    schedule, trade, config=resolved, market_identity=market_identity,
                    funding=funding,
                )
                context = {
                    "study_id": STUDY_ID, "source_quick_study": QR_STUDY_ID,
                    "screening_rule": QR_RULE, "index_key": item["index_key"],
                    "factor_code": code, "factor_name": f"GTJA {code}",
                    "factor_description": FORMULAS[code],
                    "factor_parameters": {"bar_interval": interval, "horizon_bars": horizon},
                    "factor_direction": -1,
                    "portfolio": "direction-adjusted top 20% long / bottom 20% short, equal weight",
                    "cost_variant": cost,
                }
                manifest = store.publish(
                    result, schedule,
                    resolved_config=resolved.model_dump(mode="json"),
                    market_identity=market_identity,
                    research_context=context,
                )
                metrics = matrix_report_metrics(result, schedule, resolved.model_dump(mode="json"))
                item.update({
                    "status": "succeeded", "run_id": manifest.run_id,
                    "metrics": metrics, "elapsed_seconds": perf_counter() - started,
                })
            except Exception as exc:  # preserve the other candidates and a full audit trail
                item.update({
                    "status": "failed", "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                    "elapsed_seconds": perf_counter() - started,
                })
                announce(f"failed {key}: {item['error']}")
            attempts[key] = item
            save_state(state)
            completed_now += 1

    # Another cost worker may have published while this process was running.
    state = load_state()
    recover_published(state)
    save_state(state)
    attempts = state["attempts"]
    ordered = [attempts[key] for key in sorted(attempts)]
    expected = len(schedules) * len(COSTS)
    terminal = (
        len(ordered) == expected
        and all(item["status"] in {"succeeded", "failed"} for item in ordered)
    )
    study_status = "partial"
    if terminal:
        study_status = (
            "succeeded"
            if all(item["status"] == "succeeded" for item in ordered)
            else "completed_with_failures"
        )
    summary = {
        "study_id": STUDY_ID,
        "status": study_status,
        "created_at": datetime.now(UTC).isoformat(),
        "contract_sha256": CONTRACT_SHA256, "contract": CONTRACT,
        "dataset_versions": versions, "market_identity": market_identity,
        "expected_attempts": expected, "attempts": ordered,
    }
    (STUDY / "summary.json").write_text(
        json.dumps(summary, default=str, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = render_report(summary)
    announce(f"report={report} status={summary['status']} attempts={len(ordered)}/{expected}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="run at most N outstanding cost attempts")
    parser.add_argument("--only-cost", choices=tuple(COSTS), help="run one non-overlapping cost queue")
    parser.add_argument("--minimum-rebalance-minutes", type=int)
    args = parser.parse_args()
    run(
        limit=args.limit, only_cost=args.only_cost,
        minimum_rebalance_minutes=args.minimum_rebalance_minutes,
    )


if __name__ == "__main__":
    main()
