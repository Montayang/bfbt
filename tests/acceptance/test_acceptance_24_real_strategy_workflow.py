"""Offline A24 acceptance for the formal full-market strategy workflow."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from bfbt.config import ConfigPaths, load_config_bundle

BACKTEST_ROOT = Path(__file__).resolve().parents[2]
PREPARE = (
    BACKTEST_ROOT
    / "data_collections"
    / "binance_usdm_perpetual_1m"
    / "prepare_2026_06_dataset.py"
)


def test_strategy_generator_writes_run_ready_v2_chunked_configs(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PREPARE))
    write_configs = namespace["_write_configs"]
    config_root = tmp_path / "configs"
    runs_root = tmp_path / "runs"
    write_configs(
        tmp_path / "dataset",
        SimpleNamespace(dataset_version="snapshot-a24"),
        config_root=config_root,
        runs_root=runs_root,
    )

    resolved = load_config_bundle(
        ConfigPaths(
            data=config_root / "data.json",
            universe=config_root / "universe.json",
            factor=config_root / "factor.json",
            backtest=config_root / "backtest.json",
        ),
        root=BACKTEST_ROOT,
        environment={},
        require_run_ready=True,
    )
    config = resolved.backtest
    config.assert_execution_supported()

    assert config.config_version == "v2"
    assert config.performance.mode == "chunked"
    assert config.performance.max_process_rss_mib == 5632
    assert config.performance.max_risk_state_rows == 2000
    assert config.performance.max_position_state_rows == 10
    assert config.schedule.factor_interval == "1m"
    assert config.schedule.rebalance_interval == "1m"
    assert config.portfolio.selection.mode == "rank_descent"
    assert config.portfolio.selection.audit_top_n == 5
    assert config.portfolio.selection.descent.start_rank_at_least == 5
    assert config.portfolio.sizing.mode == "equity_margin_fraction"
    assert config.portfolio.sizing.fraction == 1.0
    assert config.portfolio.holding.mode == "single_position_replace"
    assert config.risk.leverage == 5.0
    assert config.risk.fill_model == "same_bar_trigger"
    assert config.risk.symbol_exits.take_profit.distance == 0.036
    assert config.risk.symbol_exits.stop_loss.distance == 0.02
    assert config.execution.fee.taker_bps == 4.0
    assert config.execution.slippage.bps == 1.0
    assert config.execution.funding.enabled is True
    assert resolved.data.datasets.mark_bars.enabled is False
    assert resolved.factor.factors[0].parameters == {"lookback": "24h"}


def test_report_recovers_ranked_symbol_range_without_full_universe(
    tmp_path: Path,
) -> None:
    tables = tmp_path / "tables"
    tables.mkdir()
    pl.DataFrame({"sample_count": [586, 598]}).write_parquet(
        tables / "rankings.parquet"
    )

    from bfbt.reports.renderer import _panel_stats

    assert _panel_stats(tmp_path) == {
        "ranked_min": 586,
        "ranked_max": 598,
    }


def test_report_explains_rank_descent_and_exact_momentum_formula(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(PREPARE))
    config_root = tmp_path / "configs"
    namespace["_write_configs"](
        tmp_path / "dataset",
        SimpleNamespace(dataset_version="snapshot-a24"),
        config_root=config_root,
        runs_root=tmp_path / "runs",
    )
    payload = {
        name: __import__("json").loads(
            (config_root / f"{name}.json").read_text(encoding="utf-8")
        )
        for name in ("data", "universe", "factor", "backtest")
    }

    from bfbt.reports.renderer import _factor_html, _v2_audit_html

    factor_html = _factor_html("momentum", payload["factor"]["factors"][0])
    audit_html = _v2_audit_html(payload)
    assert "close(t) / close(t-24h) - 1" in factor_html
    assert "Rank ≥ 5" in audit_html
    assert "持平保留、上升重置" in audit_html
    assert "Top 5" in audit_html
    assert "Stop Loss 2.00%" in audit_html
    assert "Take Profit 3.60%" in audit_html
    assert "Same-bar conservative trigger" in audit_html
