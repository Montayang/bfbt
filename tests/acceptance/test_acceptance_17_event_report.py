"""User-run offline acceptance suite for A17 event arbitration and V2 reports."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import polars as pl
import pytest
from typer.testing import CliRunner
from pydantic import BaseModel

from bianbt.artifacts import (
    ArtifactStoreError,
    V2AuditArtifacts,
    V2RunArtifactStore,
)
from bianbt.cli import app
from bianbt.artifacts.environment import EnvironmentInfo
from bianbt.config.backtest import BacktestOutputConfig
from bianbt.data.hashing import sha256_file
from bianbt.data.manifests import (
    DatasetReference,
    DatasetSnapshotManifest,
    FactorVersionReference,
    RunManifestV2,
    load_manifest_auto,
)
from bianbt.data.schemas import get_schema_definition
from bianbt.data.v2_contracts import V2ReasonCode
from bianbt.engine.events import (
    EventArbitrationError,
    EventArbitrator,
    link_risk_event_fills,
)
from bianbt.engine.vectorized import BacktestResult
from bianbt.reports.renderer import render_report_from_artifacts

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "events" / "acceptance_17" / "intents.csv"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
RUN_ID = "engine-a17"
UTC_MS = pl.Datetime("ms", "UTC")


class _RunConfig(BaseModel):
    random_seed: int = 42


class _BacktestConfig(BaseModel):
    config_version: Literal["v2"] = "v2"
    run: _RunConfig = _RunConfig()


class _ResolvedConfig(BaseModel):
    backtest: _BacktestConfig = _BacktestConfig()


def _intents() -> pl.DataFrame:
    frame = pl.read_csv(FIXTURE, null_values=[""])
    return frame.with_columns(
        pl.col("decision_time").str.to_datetime(time_zone="UTC"),
        pl.col("fill_time").str.to_datetime(time_zone="UTC"),
        pl.col("rank_source_time").str.to_datetime(time_zone="UTC"),
    )


def _arbitration():
    return EventArbitrator(run_id=RUN_ID).arbitrate(_intents())


def _trades() -> pl.DataFrame:
    fill = START + timedelta(minutes=1)
    rows = []
    for sequence, (symbol, side, old_weight, filled_weight, notional) in enumerate(
        (
            ("BTCUSDT", "SELL", 1.0, 0.0, 100.0),
            ("ETHUSDT", "SELL", 0.5, 0.0, 50.0),
            ("SOLUSDT", "SELL", 0.0, -0.25, 25.0),
        ),
        start=1,
    ):
        rows.append(
            {
                "signal_time": START,
                "fill_time": fill,
                "symbol": symbol,
                "sequence": sequence,
                "side": side,
                "old_weight": old_weight,
                "target_weight": filled_weight,
                "filled_weight": filled_weight,
                "turnover": abs(filled_weight - old_weight),
                "reference_price": 100.0,
                "fill_price": 100.0,
                "notional": notional,
                "status": "FILLED",
                "constraint_flags": "",
                "run_id": RUN_ID,
            }
        )
    return pl.DataFrame(rows).with_columns(
        pl.col("signal_time").cast(UTC_MS),
        pl.col("fill_time").cast(UTC_MS),
    )


def _linked():
    batch = _arbitration()
    return batch, EventArbitrator(run_id=RUN_ID).link_trades(
        _trades(), batch.accepted_intents
    )


def _risk_events() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "event_id": ["risk-event-btc"],
            "evaluation_time": [START],
            "trigger_time": [START],
            "symbol": ["BTCUSDT"],
            "event_type": ["stop_loss"],
            "direction": ["LONG"],
            "entry_price": [105.0],
            "trigger_level": [100.0],
            "observed_price": [99.0],
            "conflict_policy": ["worst_case"],
            "action": ["close"],
            "fill_time": [None],
            "reason_code": [V2ReasonCode.STOP_LOSS_TRIGGERED.value],
            "run_id": [RUN_ID],
        }
    ).with_columns(
        pl.col("evaluation_time").cast(UTC_MS),
        pl.col("trigger_time").cast(UTC_MS),
        pl.col("fill_time").cast(UTC_MS),
    )


def _rankings() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "timestamp": [START] * 3,
            "rank_clock": ["rebalance"] * 3,
            "symbol": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
            "factor_name": ["momentum"] * 3,
            "raw_score": [3.0, 2.0, 1.0],
            "ordinal_rank": [1, 2, 3],
            "percentile_rank": [1.0, 0.5, 0.0],
            "sample_count": [3, 3, 3],
            "factor_version": ["momentum-v1"] * 3,
            "universe_version": ["universe-v1"] * 3,
            "run_id": [RUN_ID] * 3,
        }
    ).with_columns(pl.col("timestamp").cast(UTC_MS))


def _returns() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "timestamp": [
                START + timedelta(minutes=index) for index in (1, 2, 3)
            ],
            "gross_price_return": [0.01, -0.01, 0.005],
            "fee_cost": [0.0, 0.0, 0.0],
            "slippage_cost": [0.0, 0.0, 0.0],
            "funding_return": [0.0, 0.0, 0.0],
            "net_return": [0.01, -0.01, 0.005],
            "equity": [1.01, 0.9999, 1.0048995],
            "drawdown": [0.0, -0.01, -0.00505],
            "gross_exposure": [1.0, 0.5, 0.25],
            "net_exposure": [0.0, -0.5, -0.25],
            "turnover": [1.0, 0.5, 0.0],
            "run_id": [RUN_ID] * 3,
        }
    ).lazy()


def _result(linked_trades: pl.DataFrame) -> BacktestResult:
    targets = pl.DataFrame(
        {
            "signal_time": [START],
            "symbol": ["SOLUSDT"],
            "score": [1.0],
            "side": ["SHORT"],
            "unconstrained_weight": [-0.25],
            "target_weight": [-0.25],
            "constraint_flags": [""],
            "portfolio_version": ["portfolio-v2"],
            "run_id": [RUN_ID],
        }
    ).lazy()
    positions = pl.DataFrame(
        {
            "timestamp": [START + timedelta(minutes=1)],
            "symbol": ["SOLUSDT"],
            "quantity": [-0.25],
            "signed_notional": [-25.0],
            "target_weight": [-0.25],
            "actual_weight": [-0.25],
            "mark_price": [100.0],
            "unrealized_pnl": [0.0],
            "used_margin": [12.5],
            "available_margin": [87.5],
            "average_entry_price": [100.0],
            "consecutive_adds": [1],
            "run_id": [RUN_ID],
        }
    ).lazy()
    costs = pl.DataFrame(
        {
            "timestamp": [START + timedelta(minutes=1)],
            "symbol": ["SOLUSDT"],
            "fee_cost": [0.0],
            "slippage_cost": [0.0],
            "funding_cashflow": [0.0],
            "total_cost": [0.0],
            "run_id": [RUN_ID],
        }
    ).lazy()
    return BacktestResult(
        run_id=RUN_ID,
        result_hash=SHA_C,
        targets=targets,
        trades=linked_trades.lazy(),
        positions=positions,
        costs=costs,
        returns=_returns(),
        warnings=(),
    )


def _snapshot() -> DatasetSnapshotManifest:
    definition = get_schema_definition("bars", "v1")
    return DatasetSnapshotManifest(
        dataset_id="a17-fixture",
        dataset_version="snapshot-v1",
        created_at=START,
        datasets=(
            DatasetReference(
                dataset_name="bars",
                dataset_version="bars-v1",
                schema_version="v1",
                schema_fingerprint=definition.fingerprint,
                available_from=START,
                available_to=START + timedelta(days=1),
                partition_manifest_ids=("bars-partition",),
                quality_report_ids=("quality-bars",),
            ),
        ),
        source_manifest_hash=SHA_A,
        normalizer_code_version="normalizer-v1",
        normalizer_parameters_hash=SHA_B,
    )


def _environment() -> EnvironmentInfo:
    return EnvironmentInfo(
        git_commit="1" * 40,
        source_fingerprint=SHA_A,
        git_dirty=False,
        python_version="3.12.3",
        dependency_fingerprint=SHA_B,
        dependencies=("polars==1.43.1",),
    )


def _payload() -> dict[str, object]:
    return {
        "data": {
            "time": {
                "base_interval": "1m",
                "range_semantics": "left_closed_right_open",
                "timezone": "UTC",
            },
            "market": {
                "venue": "binance",
                "segment": "um_futures",
                "contract_type": "perpetual",
                "quote_asset": "USDT",
                "margin_asset": "USDT",
            },
        },
        "backtest": {
            "config_version": "v2",
            "run": {
                "name": "A17 audit fixture",
                "start": START.isoformat(),
                "end": (START + timedelta(hours=1)).isoformat(),
            },
            "schedule": {
                "factor_interval": "1h",
                "rebalance_interval": "4h",
                "signal_delay_bars": 1,
            },
            "portfolio": {
                "selection": {
                    "clock": "rebalance",
                    "lag": 1,
                    "long": {"ranks": [2]},
                    "short": {"ranks": [1]},
                },
                "sizing": {"mode": "fixed_notional"},
                "constraints": {"max_gross_exposure": 2.0},
            },
            "capital": {
                "initial_equity": 100.0,
                "margin_model": "simple_cross",
            },
            "risk": {
                "evaluation_interval": "1m",
                "leverage": 2.0,
                "trigger_price": "trade",
                "fill_model": "next_bar_open",
                "intrabar_conflict": "worst_case",
                "reentry_policy": "after_cooldown",
                "symbol_exits": {
                    "stop_loss": {"enabled": True},
                    "take_profit": {"enabled": True},
                    "trailing_stop": {"enabled": False},
                },
            },
            "execution": {
                "fill_price": "next_bar_open",
                "fee": {"model": "constant", "taker_bps": 0.0},
                "slippage": {"model": "constant_bps", "bps": 0.0},
                "funding": {"enabled": False},
            },
            "valuation": {"price": "trade_close"},
        },
        "factor": {
            "factors": [
                {
                    "name": "momentum",
                    "version": "v1",
                    "compute_interval": "1h",
                    "parameters": {"lookback": "24h"},
                }
            ]
        },
    }


def _publish(tmp_path: Path, rankings: pl.DataFrame | None = None):
    arbitration, links = _linked()
    risk_events = link_risk_event_fills(
        _risk_events(),
        links.trades,
        run_id=RUN_ID,
        position_instructions=arbitration.instructions,
    )
    store = V2RunArtifactStore(
        tmp_path / "runs", now=lambda: START + timedelta(days=2)
    )
    audit = V2AuditArtifacts(
        rankings=rankings if rankings is not None else _rankings(),
        position_instructions=arbitration.instructions,
        risk_events=risk_events,
        linked_trades=links.trades,
        arbitration_trace=arbitration.trace,
        audit_result_hash=SHA_C,
    )
    published = store.publish_success_v2(
        _result(links.trades),
        audit=audit,
        snapshot=_snapshot(),
        resolved_config=_ResolvedConfig(),
        resolved_config_payload=_payload(),
        resolved_config_hash=SHA_A,
        factor_versions=(
            FactorVersionReference(
                factor_name="momentum", factor_version="momentum-v1"
            ),
        ),
        environment=_environment(),
        base_interval="1m",
        output=BacktestOutputConfig(root=tmp_path / "runs"),
    )
    return store, published


def test_priority_arbitration_keeps_winner_and_suppressed_requests() -> None:
    batch = _arbitration()
    reasons = dict(
        batch.instructions.select("instruction_id", "reason_code").iter_rows()
    )
    deltas = dict(
        batch.instructions.select(
            "instruction_id", "constrained_delta_notional"
        ).iter_rows()
    )
    assert reasons["risk-btc"] == V2ReasonCode.STOP_LOSS_TRIGGERED.value
    assert reasons["strategy-btc"] == V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value
    assert reasons["universe-eth"] == V2ReasonCode.UNIVERSE_FORCED_EXIT.value
    assert deltas["strategy-btc"] == 0.0
    assert deltas["strategy-eth"] == 0.0
    assert batch.accepted_count == 3
    assert batch.suppressed_count == 2


def test_cooldown_blocks_scheduled_reentry_without_dropping_audit_row() -> None:
    scheduled = _intents().filter(pl.col("instruction_id") == "strategy-sol")
    batch = EventArbitrator(run_id=RUN_ID).arbitrate(
        scheduled, cooldown_symbols={"SOLUSDT"}
    )
    assert batch.accepted_count == 0
    assert batch.instructions.item(0, "reason_code") == V2ReasonCode.COOLDOWN_ACTIVE.value
    assert batch.instructions.item(0, "constrained_delta_notional") == 0.0


def test_equal_priority_tie_break_is_stable_across_input_order() -> None:
    engine = EventArbitrator(run_id=RUN_ID)
    base = engine.normalize(_intents()).filter(
        pl.col("instruction_id") == "strategy-sol"
    )
    other = base.with_columns(
        pl.lit("strategy-sol-a").alias("instruction_id"),
        pl.lit(10.0).alias("requested_delta_notional"),
        pl.lit(10.0).alias("constrained_delta_notional"),
    )
    combined = pl.concat([base, other])
    forward = engine.arbitrate(combined)
    reverse = engine.arbitrate(combined.reverse())
    assert forward.instructions.equals(reverse.instructions)
    assert forward.accepted_intents.item(0, "instruction_id") == "strategy-sol"


def test_pending_intent_budget_fails_before_partial_arbitration() -> None:
    with pytest.raises(EventArbitrationError, match="max_pending_intents"):
        EventArbitrator(
            run_id=RUN_ID, max_pending_intents=2
        ).arbitrate(_intents())


def test_historical_accepted_intents_are_not_counted_as_pending() -> None:
    batch = _arbitration()
    links = EventArbitrator(
        run_id=RUN_ID, max_pending_intents=2
    ).link_trades(_trades(), batch.accepted_intents)
    assert links.linked_instruction_count == 3


def test_trades_link_bidirectionally_to_only_accepted_instructions() -> None:
    batch, links = _linked()
    assert links.linked_instruction_count == 3
    assert links.linked_risk_event_count == 1
    assert links.trades["instruction_id"].null_count() == 0
    assert set(links.trades["instruction_id"]) == set(
        batch.accepted_intents["instruction_id"]
    )


def test_missing_or_duplicate_trade_links_fail_explicitly() -> None:
    batch = _arbitration()
    engine = EventArbitrator(run_id=RUN_ID)
    with pytest.raises(EventArbitrationError, match="must link"):
        engine.link_trades(_trades().head(2), batch.accepted_intents)
    duplicate = pl.concat([_trades(), _trades().head(1)])
    with pytest.raises(EventArbitrationError, match="partial or duplicate"):
        engine.link_trades(duplicate, batch.accepted_intents)


def test_risk_event_fill_time_comes_from_linked_trade() -> None:
    _, links = _linked()
    linked = link_risk_event_fills(
        _risk_events(), links.trades, run_id=RUN_ID
    )
    assert linked.item(0, "fill_time") == START + timedelta(minutes=1)
    broken = _risk_events().with_columns(
        pl.lit("unknown-event").alias("event_id")
    )
    with pytest.raises(EventArbitrationError, match="must link"):
        link_risk_event_fills(broken, links.trades, run_id=RUN_ID)


def test_v2_atomic_publish_writes_formal_tables_and_manifest(tmp_path: Path) -> None:
    _, published = _publish(tmp_path)
    assert isinstance(published.manifest, RunManifestV2)
    assert published.path.name.startswith("a17-")
    assert {
        "rankings",
        "position_instructions",
        "risk_events",
    } == {
        item.artifact_name
        for item in published.manifest.artifact_schema_versions
    }
    for table in ("rankings", "position_instructions", "risk_events"):
        assert (published.path / "tables" / f"{table}.parquet").is_file()
        assert pl.read_parquet(
            published.path / "tables" / f"{table}.parquet"
        )["run_id"].unique().to_list() == [published.manifest.run_id]
    restored = load_manifest_auto(published.path / "manifest.json")
    assert restored == published.manifest
    V2RunArtifactStore.verify(published.path, restored)


def test_v2_publish_is_idempotent_and_binds_audit_hash(tmp_path: Path) -> None:
    _, first = _publish(tmp_path)
    _, second = _publish(tmp_path)
    assert second.already_published is True
    assert second.path == first.path
    metadata = json.loads(
        (first.path / "run_metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["audit_result_hash"] == SHA_C


def test_v2_report_contains_bilingual_interactive_audit(tmp_path: Path) -> None:
    _, published = _publish(tmp_path)
    report = (published.path / "report.html").read_text(encoding="utf-8")
    for expected in (
        "第二版策略与风险执行 / V2 Strategy & Risk Execution",
        "Rank 来源 / Rank Sources",
        "仓位指令与抑制 / Position Instructions & Suppression",
        "风险事件 / Risk Events",
        "因子频率 / Factor",
        "调仓频率 / Rebalance",
        "风险检查 / Risk",
        "data-report-target=\"analysis\"",
        "data-report-view=\"strategy\"",
        "data-report-view=\"details\"",
        "chart-workbench",
        "nav-kpis",
            "a38-report-v18-complete-execution-audit",
        "手续费实际扣除 / Actual Fee Deduction",
        "滑点实际扣除 / Actual Slippage Deduction",
        "总盈利/亏损 / Total Profit &amp; Loss",
        "0.0000 USDT",
        "--bg:#f3f5f2",
        "账户权益 / Equity (USDT)",
        "data-snapshot-tab=\"rankings\"",
        "data-chart-action=\"next-trade\"",
        "data-chart-action=\"next-position\"",
        "point-audit-time",
        "成交前 / Before",
        "成交后 / After",
        "成交前权重 / Before",
        "目标权重 / Target",
        "成交后权重 / After",
        "data-local-target=\"factor\"",
        "data-local-target=\"metrics\"",
        "data-local-panel=\"risk\"",
        "parameter-grid",
        "align-items:stretch",
        "grid-auto-rows:minmax(58px,1fr)",
        "viewBox=\"0 0 1280 650\"",
        "font-size:11px;line-height:1.35",
        "font-size:14px;margin-top:2px",
    ):
        assert expected in report
    assert "移动鼠标查看任意时刻" not in report
    assert "min-height:320px" not in report
    assert "<details" not in report
    match = re.search(
        r'<script id="interactive-report-data" '
        r'type="application/json">(.*?)</script>',
        report,
        re.DOTALL,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert len(payload["points"]) <= 1_360
    assert payload["limits"]["rows_per_snapshot_table"] == 80
    assert payload["limits"]["event_times"] is None
    snapshots = payload["snapshots"].values()
    assert all("positions_before" in item for item in snapshots)
    snapshots = payload["snapshots"].values()
    assert any(item["rankings"] for item in snapshots)
    assert any(item["instructions"] for item in snapshots)
    assert any(item["risk_events"] for item in snapshots)


def test_report_rebuild_is_byte_identical_without_engine(tmp_path: Path) -> None:
    _, published = _publish(tmp_path)
    direct = render_report_from_artifacts(
        published.path, output_path=tmp_path / "rebuilt-a17.html"
    )
    assert sha256_file(direct) == sha256_file(
        published.path / "report.html"
    )
    rebuilt = tmp_path / "rebuilt-a17-cli.html"
    result = CliRunner().invoke(
        app,
        [
            "report",
            published.manifest.run_id,
            "--output-root",
            str(published.path.parent),
            "--output",
            str(rebuilt),
        ],
    )


def test_invalid_v2_artifact_cleans_staging_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    duplicate = pl.concat([_rankings(), _rankings().head(1)])
    with pytest.raises(ArtifactStoreError, match="primary key"):
        _publish(tmp_path, rankings=duplicate)
    root = tmp_path / "runs"
    assert not root.exists() or not [
        path for path in root.iterdir() if path.name != ".staging"
    ]
