"""Run the R5/R6 July 2026 Event parameter studies and render parent indexes."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from bfbt.application.run import RunExecutionError, execute_formal_run
from bfbt.artifacts.environment import capture_environment
from bfbt.artifacts.store import RunArtifactStore
from bfbt.config.bundle import ResolvedConfig
from bfbt.data.catalog import DuckDBCatalog
from bfbt.data.hashing import content_sha256
from bfbt.data.manifests import manifest_sha256
from bfbt.reports.event_study import render_event_parameter_study
from bfbt.reports.renderer import render_report_from_artifacts


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = PROJECT_ROOT / "data/backtest"
DATASET_ROOT = DATA_ROOT / "datasets/binance_usdm_perpetual_1m"
CATALOG_PATH = DATA_ROOT / "catalogs/binance_usdm_perpetual_1m.duckdb"
RUNS_ROOT = DATA_ROOT / "runs"
REUSE_ROOT = DATA_ROOT / "reuse"
STUDIES_ROOT = DATA_ROOT / "event_studies"

DATASET_ID = "binance-usdm-full-market-rank-descent-2026-07"
DATASET_VERSION = "live-fd672b8e69b458d1c0076d74"
START = "2026-07-01T00:00:00+00:00"
END = "2026-08-01T00:00:00+00:00"

PROFILES: dict[str, tuple[float | None, float | None]] = {
    "BASE": (None, None),
    "F1": (0.024, 0.012),
    "F2": (0.036, 0.020),
    "F3": (0.048, 0.024),
    "F4": (0.058, 0.028),
    "F5": (0.072, 0.036),
}

STRATEGIES: dict[str, dict[str, str]] = {
    "R5": {
        "factor_name": "sampled_mean_ratio",
        "direction": "POS",
        "variant_id": "rdl_sampled_mean_ratio_15m12_pos_fixed_margin_third",
        "study_id": "r5_sampled_mean_ratio_pos_2026_07",
        "title": "R5 相位采样均值比正向 · 2026-07 Event 参数研究",
    },
    "R6": {
        "factor_name": "sampled_mean_ratio_inverse",
        "direction": "NEG",
        "variant_id": "rdl_sampled_mean_ratio_15m12_neg_fixed_margin_third",
        "study_id": "r6_sampled_mean_ratio_neg_2026_07",
        "title": "R6 相位采样均值比反向 · 2026-07 Event 参数研究",
    },
}


def _data_config() -> dict[str, Any]:
    return {
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
            "start": "2026-06-30T00:00:00+00:00",
            "end": "2026-08-02T00:00:00+00:00",
        },
        "storage": {
            "root": str(DATASET_ROOT),
            "raw": str(DATASET_ROOT / "raw"),
            "normalized": str(DATASET_ROOT / "normalized"),
            "metadata": str(DATASET_ROOT / "metadata"),
        },
    }


def _universe_config() -> dict[str, Any]:
    return {
        "schedule": {"interval": "1m"},
        "market": _data_config()["market"],
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
    }


def _factor_config(factor_name: str) -> dict[str, Any]:
    return {
        "factors": [
            {
                "name": factor_name,
                "version": "v1",
                "parameters": {
                    "sample_interval": "15m",
                    "sample_count": 12,
                },
                "compute_interval": "1m",
                "preprocess": [],
            }
        ],
        "labels": [],
        "cache": {"enabled": False},
    }


def _exit_rule(distance: float | None) -> dict[str, Any]:
    if distance is None:
        return {"enabled": False}
    return {"enabled": True, "distance": distance, "action": "close"}


def _backtest_config(
    strategy: str, profile_id: str, *, take_profit: float | None, stop_loss: float | None
) -> dict[str, Any]:
    return {
        "config_version": "v2",
        "run": {
            "name": f"{strategy}-{profile_id}-202607-r01",
            "start": START,
            "end": END,
            "dataset_version": DATASET_VERSION,
            "random_seed": 42,
        },
        "schedule": {
            "factor_interval": "1m",
            "rebalance_interval": "1m",
            "signal_delay_bars": 1,
        },
        "capital": {
            "currency": "USDT",
            "initial_equity": 10_000.0,
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
                "mode": "fixed_margin",
                "margin_amount": 10_000.0 / 3.0,
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
            "funding": {"enabled": True, "missing_policy": "assume_zero"},
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
                "stop_loss": _exit_rule(stop_loss),
                "take_profit": _exit_rule(take_profit),
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
            "root": str(RUNS_ROOT),
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
            "max_input_rows_per_chunk": 3_000_000,
            "max_incremental_rss_mib": 5_120,
            "max_process_rss_mib": 5_632,
            "collect_diagnostics": True,
            "resume_policy": "resume",
            "max_rank_lag": 0,
            "max_rank_state_rows": 2_000,
            "max_position_state_rows": 10,
            "max_pending_instructions": 100,
            "max_risk_state_rows": 2_000,
            "max_pending_risk_intents": 100,
            "reuse_mode": "read_write",
            "reuse_root": str(REUSE_ROOT),
            "sparse_execution": True,
        },
        "engine": {
            "backend": "event",
            "purpose": "formal",
            "equivalence_audit": False,
        },
    }


def _resolved(strategy: str, profile_id: str) -> ResolvedConfig:
    info = STRATEGIES[strategy]
    take_profit, stop_loss = PROFILES[profile_id]
    return ResolvedConfig.model_validate(
        {
            "data": _data_config(),
            "universe": _universe_config(),
            "factor": _factor_config(info["factor_name"]),
            "backtest": _backtest_config(
                strategy,
                profile_id,
                take_profit=take_profit,
                stop_loss=stop_loss,
            ),
        }
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f".{path.name}.partial")
    partial.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial.replace(path)


def _write_configs(
    strategy: str, profile_id: str, config: ResolvedConfig
) -> None:
    variant = STRATEGIES[strategy]["variant_id"]
    root = DATA_ROOT / "workspaces" / variant / "configs/2026-07" / profile_id
    for name, model in (
        ("data", config.data),
        ("universe", config.universe),
        ("factor", config.factor),
        ("backtest", config.backtest),
    ):
        _write_json(root / f"{name}.json", model.model_dump(mode="json"))


def _event_counts(run_path: Path) -> dict[str, int]:
    risk_path = run_path / "tables/risk_events.parquet"
    if not risk_path.exists():
        return {}
    rows = (
        pl.scan_parquet(risk_path, hive_partitioning=False)
        .group_by("event_type")
        .len()
        .collect(engine="streaming")
        .to_dicts()
    )
    return {str(row["event_type"]): int(row["len"]) for row in rows}


def _candidate(
    strategy: str,
    profile_id: str,
    *,
    published,
    report_href: str,
) -> dict[str, Any]:
    take_profit, stop_loss = PROFILES[profile_id]
    counts = _event_counts(published.path)
    trades = int(
        pl.scan_parquet(
            published.path / "tables/trades.parquet", hive_partitioning=False
        )
        .select(pl.len())
        .collect(engine="streaming")
        .item()
    )
    return {
        "profile_id": profile_id,
        "run_alias": f"{strategy}-{profile_id}-202607-r01",
        "run_id": published.manifest.run_id,
        "manifest_sha256": manifest_sha256(published.manifest),
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "status": published.manifest.status,
        "already_published": published.already_published,
        "report_href": report_href,
        "trade_count": trades,
        "stop_loss_count": counts.get("stop_loss", 0),
        "take_profit_count": counts.get("take_profit", 0),
        "metrics": published.metrics,
    }


def _failed_candidate(
    strategy: str,
    profile_id: str,
    *,
    failed,
    error: str,
    report_href: str,
) -> dict[str, Any]:
    take_profit, stop_loss = PROFILES[profile_id]
    return {
        "profile_id": profile_id,
        "run_alias": f"{strategy}-{profile_id}-202607-r01",
        "run_id": failed.manifest.run_id,
        "manifest_sha256": manifest_sha256(failed.manifest),
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "status": "failed",
        "already_published": failed.already_published,
        "report_href": report_href,
        "trade_count": None,
        "stop_loss_count": None,
        "take_profit_count": None,
        "metrics": {},
        "error": error,
    }


def _render_failed_child(*, candidate: dict[str, Any], output_path: Path) -> None:
    profile = html.escape(str(candidate["profile_id"]))
    run_id = html.escape(str(candidate["run_id"]))
    error = html.escape(str(candidate["error"]))
    page = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>{profile} failed</title>
<style>body{{margin:0;background:#f1f6f3;color:#10231f;font:15px/1.6 system-ui,sans-serif}}
main{{max-width:1100px;margin:24px auto;padding:24px;background:white;border:1px solid #d7e3de;border-radius:14px}}
.status{{color:#a43f2d;font-weight:800}}code,pre{{background:#f4f7f5;border-radius:8px}}code{{padding:2px 5px}}
pre{{padding:14px;white-space:pre-wrap;overflow-wrap:anywhere}}</style></head><body><main>
<div class='status'>FAILED / 未完成</div><h1>{profile} Event 子回测</h1>
<p>该配置没有成功完成，因此不提供收益、回撤或成交统计，也不把失败伪装成 -100% 收益。</p>
<p>失败 run：<code>{run_id}</code></p><h2>失败原因</h2><pre>{error}</pre>
</main></body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(page, encoding="utf-8")


def _contract(strategy: str) -> dict[str, Any]:
    info = STRATEGIES[strategy]
    return {
        "strategy": strategy,
        "variant_id": info["variant_id"],
        "factor_name": info["factor_name"],
        "factor_formula": (
            "close(t)/mean(close(t-k*15m), k=0..11)-1"
            if info["direction"] == "POS"
            else "-(close(t)/mean(close(t-k*15m), k=0..11)-1)"
        ),
        "sample_lags_minutes": list(range(0, 166, 15)),
        "factor_interval": "1m",
        "rebalance_interval": "1m",
        "rank_descent": {
            "start_rank_at_least": 5,
            "entry_rank": 1,
            "equal_policy": "keep",
            "increase_policy": "reset",
        },
        "holding": "single_position_replace_long",
        "fixed_margin": 10_000.0 / 3.0,
        "leverage": 5.0,
        "fee_bps": 4.0,
        "slippage_bps": 1.0,
        "funding": "real_assume_zero_when_missing_with_warning",
        "range": {"start": START, "end": END},
        "dataset_id": DATASET_ID,
        "dataset_version": DATASET_VERSION,
        "risk_profiles": {
            key: {"take_profit": value[0], "stop_loss": value[1]}
            for key, value in PROFILES.items()
        },
        "dynamic_take_profit": False,
    }


def _study_summary(strategy: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    info = STRATEGIES[strategy]
    contract = _contract(strategy)
    return {
        "study_version": "event-parameter-study/v1",
        "study_id": info["study_id"],
        "title": info["title"],
        "strategy_id": strategy,
        "variant_id": info["variant_id"],
        "factor_name": info["factor_name"],
        "direction": info["direction"],
        "status": (
            "running"
            if len(candidates) != len(PROFILES)
            else (
                "succeeded"
                if all(item.get("status") == "succeeded" for item in candidates)
                else "completed_with_failures"
            )
        ),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "contract": contract,
        "contract_sha256": content_sha256(contract),
        "candidates": candidates,
    }


def run(strategy: str, profiles: tuple[str, ...]) -> None:
    info = STRATEGIES[strategy]
    study_root = STUDIES_ROOT / info["study_id"]
    children = study_root / "children"
    summary_path = study_root / "summary.json"
    existing: dict[str, dict[str, Any]] = {}
    if summary_path.exists():
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        existing = {
            str(item["profile_id"]): item
            for item in payload.get("candidates", [])
            if isinstance(item, dict) and "profile_id" in item
        }
    catalog = DuckDBCatalog(CATALOG_PATH)
    snapshot = catalog.resolve_dataset(DATASET_ID, DATASET_VERSION)
    environment = capture_environment(PROJECT_ROOT)
    for profile_id in profiles:
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] {strategy} {profile_id} start",
            flush=True,
        )
        config = _resolved(strategy, profile_id)
        _write_configs(strategy, profile_id, config)
        child = children / f"{profile_id}.html"
        try:
            published = execute_formal_run(
                config,
                snapshot,
                factor_name=info["factor_name"],
                catalog=catalog,
                project_root=PROJECT_ROOT,
                verify_hashes=True,
                environment=environment,
            )
        except RunExecutionError as exc:
            if exc.failed_run is None:
                raise
            existing[profile_id] = _failed_candidate(
                strategy,
                profile_id,
                failed=exc.failed_run,
                error=str(exc),
                report_href=f"children/{profile_id}.html",
            )
            _render_failed_child(candidate=existing[profile_id], output_path=child)
        else:
            render_report_from_artifacts(published.path, output_path=child)
            existing[profile_id] = _candidate(
                strategy,
                profile_id,
                published=published,
                report_href=f"children/{profile_id}.html",
            )
        ordered = [existing[key] for key in PROFILES if key in existing]
        summary = _study_summary(strategy, ordered)
        _write_json(summary_path, summary)
        render_event_parameter_study(summary, output_path=study_root / "report.html")
        candidate = existing[profile_id]
        if candidate["status"] == "succeeded":
            result = candidate["metrics"]["performance"]
            detail = f"return={result['total_return']:.6%}"
        else:
            detail = "failed (see child report)"
        print(
            f"[{datetime.now(timezone.utc).isoformat()}] {strategy} {profile_id} "
            f"run_id={candidate['run_id']} {detail}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=("R5", "R6", "all"), default="all")
    parser.add_argument("--profile", choices=tuple(PROFILES), action="append")
    args = parser.parse_args()
    profiles = tuple(args.profile) if args.profile else tuple(PROFILES)
    strategies = tuple(STRATEGIES) if args.strategy == "all" else (args.strategy,)
    for strategy in strategies:
        run(strategy, profiles)


if __name__ == "__main__":
    main()
