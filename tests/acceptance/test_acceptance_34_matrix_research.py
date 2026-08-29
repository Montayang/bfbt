"""A34 shared batch, immutable research artifacts, and promotion."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from bianbt.application.matrix import prepare_event_promotion
from bianbt.artifacts.matrix import MatrixArtifactError, MatrixResearchStore
from bianbt.config.backtest import BacktestConfig
from bianbt.engine.fast_matrix.batch import run_fast_matrix_batch
from bianbt.engine.fast_matrix.kernel import run_fast_matrix
from bianbt.engine.fast_matrix.target_schedule import build_target_schedule
from bianbt.reports.research_study import render_factor_study_reports

START = datetime(2026, 3, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")


def _config() -> BacktestConfig:
    return BacktestConfig.model_validate({
        "config_version": "v2", "engine": {"backend": "fast_matrix", "purpose": "research"},
        "schedule": {"factor_interval": "1m", "rebalance_interval": "2m", "signal_delay_bars": 1},
        "portfolio": {"selection": {"long": {"ranks": [1]}, "short": {"ranks": [2]}}, "sizing": {"mode": "target_weight", "weighting": "equal", "target_gross_exposure": 1.0, "target_net_exposure": 0.0}},
        "execution": {"fee": {"model": "zero"}, "slippage": {"model": "zero"}, "funding": {"enabled": False}},
        "valuation": {"price": "trade_close"},
        "risk": {"leverage": 2.0, "evaluation_interval": "1m", "trigger_price": "trade", "fill_model": "next_bar_open", "intrabar_conflict": "worst_case", "reentry_policy": "next_scheduled_rebalance"},
        "capital": {"initial_equity": 1000.0}, "performance": {"max_input_rows_per_chunk": 1000},
    })


def _bars() -> pl.DataFrame:
    rows = [
        {"open_time": START + timedelta(minutes=minute), "close_time": START + timedelta(minutes=minute + 1), "symbol": symbol, "open": price + minute, "close": price + minute + direction}
        for minute in range(5)
        for symbol, price, direction in (("BTCUSDT", 100.0, 1.0), ("ETHUSDT", 50.0, -0.5))
    ]
    return pl.DataFrame(rows).with_columns(pl.col("open_time").cast(UTC_MS), pl.col("close_time").cast(UTC_MS))


def _targets() -> pl.DataFrame:
    return pl.DataFrame([
        {"signal_time": START, "fill_time": START + timedelta(minutes=1), "symbol": symbol, "target_weight": weight, "source_signal_id": "signal-a34", "factor_version": "factor-a34", "universe_version": "universe-a34", "portfolio_version": "portfolio-a34"}
        for symbol, weight in (("BTCUSDT", 0.5), ("ETHUSDT", -0.5))
    ])


def _schedule(weight_scale: float = 1.0):
    frame = _targets().with_columns((pl.col("target_weight") * weight_scale).alias("target_weight"))
    return build_target_schedule(frame, rebalance_times=(START + timedelta(minutes=1),), parent_manifest_sha256="d" * 64)


def test_batch_reads_shared_market_once_and_keeps_candidate_state_isolated() -> None:
    config = _config()
    batch = run_fast_matrix_batch({"base": _schedule(), "half": _schedule(0.5)}, _bars().lazy(), config=config, market_identity="a34")
    assert batch.diagnostics["shared_market_loads"] == 1
    standalone = run_fast_matrix(_schedule(), _bars(), config=config, market_identity="a34:base")
    assert_frame_equal(batch.candidates["base"].returns, standalone.returns)
    assert batch.candidates["base"].checkpoint.previous_equity != batch.candidates["half"].checkpoint.previous_equity


def test_research_store_is_immutable_verified_and_promotes_to_event(tmp_path: Path) -> None:
    config, schedule = _config(), _schedule()
    result = run_fast_matrix(schedule, _bars(), config=config, market_identity="a34")
    store = MatrixResearchStore(tmp_path / "research_runs")
    manifest = store.publish(
        result,
        schedule,
        resolved_config=config.model_dump(mode="json"),
        market_identity="a34",
        research_context={
            "study_id": "classic", "factor_code": "REV4",
            "factor_description": "four-hour reversal",
            "index_key": "classic|2026-03|REV4|zero",
        },
    )
    assert manifest.run_id.startswith("fm-")
    assert store.load(manifest.run_id) == manifest
    report = (store.directory(manifest.run_id) / "report.html").read_text()
    assert "未经过 Event 正式确认" in report
    assert "four-hour reversal" in report
    assert "执行口径 / Execution" in report
    metrics = json.loads((store.directory(manifest.run_id) / "metrics.json").read_text())
    assert metrics["rebalance_count"] == 1
    assert metrics["cumulative_turnover"] > 0
    table = store.directory(manifest.run_id) / manifest.tables["returns"].path
    table.write_bytes(table.read_bytes() + b"tamper")
    with pytest.raises(MatrixArtifactError, match="tampered"):
        store.load(manifest.run_id)

    promotion = prepare_event_promotion(manifest.run_id, config)
    assert promotion.event_config.engine.backend == "event"
    assert promotion.event_config.engine.purpose == "formal"
    assert promotion.event_config.engine.source_matrix_run_id == manifest.run_id
    assert promotion.source_matrix_run_id == manifest.run_id


def test_factor_study_reports_separate_screening_and_matrix_indexes(tmp_path: Path) -> None:
    config, schedule = _config(), _schedule()
    result = run_fast_matrix(schedule, _bars(), config=config, market_identity="a34-study")
    runs = tmp_path / "research_runs"
    manifest = MatrixResearchStore(runs).publish(
        result, schedule, resolved_config=config.model_dump(mode="json"),
        market_identity="a34-study",
    )
    study = tmp_path / "research_studies" / "classic"
    study.mkdir(parents=True)
    summary = {
        "status": "succeeded", "study_id": "classic",
        "policy": {"factor_interval": "1m", "rebalance_interval": "2m"},
        "factors": {"REV4": {
            "name": "reversal", "description": "four-hour reversal",
            "parameters": {"lookback": "4h"}, "direction": 1,
        }},
        "months": {"2026-03": {"factors": {"REV4": {
            "quick_research": {"1h": {
                "mean_rank_ic_direction_adjusted": 0.04,
                "expected_direction_quantile_spread": 0.001,
                "factor_coverage": 0.99, "mean_rank_turnover": 0.08,
                "rank_ic_expected_sign_fraction": 0.60,
            }},
            "fast_matrix": {"zero": {
                "run_id": manifest.run_id, "total_return": 0.01,
                "max_drawdown": -0.02, "cumulative_turnover": 3.0,
                "fee_amount": 0.0, "slippage_amount": 0.0,
                "funding_amount": 0.0,
            }},
        }}}},
    }
    (study / "summary.json").write_text(json.dumps(summary))
    rendered = render_factor_study_reports(study, matrix_runs_root=runs)
    landing = rendered["landing"].read_text()
    quick = rendered["quick_research"].read_text()
    matrix = rendered["fast_matrix"].read_text()
    assert "本页不再混放两阶段明细" in landing
    assert "classic|2026-03|REV4|1h" in quick
    assert "策略方向 Rank IC" in quick
    assert "方向调整 Rank IC" not in quick
    assert f"classic|2026-03|REV4|zero|{manifest.run_id}" in matrix
    rebuilt = (study / "fast_matrix_reports" / f"{manifest.run_id}.html").read_text()
    assert "four-hour reversal" in rebuilt
