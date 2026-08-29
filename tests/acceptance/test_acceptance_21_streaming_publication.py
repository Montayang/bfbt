from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from bianbt.artifacts.store import ArtifactStoreError
from bianbt.artifacts.v2 import V2RunArtifactStore
from bianbt.data.v2_contracts import V2ReasonCode
from bianbt.engine.events import (
    EventArbitrationError,
    link_risk_event_fills_lazy,
)
from bianbt.metrics import compute_run_metrics
from bianbt.reports.renderer import (
    _bounded_event_times,
    _interactive_payload,
    _bounded_returns,
)

UTC = timezone.utc
START = datetime(2026, 6, 1, tzinfo=UTC)
RUN_ID = "a21-engine-fixture"


def _return_ledger(rows: int) -> pl.DataFrame:
    net_returns = [0.0001 if index % 3 else -0.00005 for index in range(rows)]
    equity = 1.0
    equities = []
    drawdowns = []
    peak = equity
    for value in net_returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        equities.append(equity)
        drawdowns.append(equity / peak - 1.0)
    return pl.DataFrame(
        {
            "timestamp": [START + timedelta(minutes=i) for i in range(rows)],
            "gross_price_return": net_returns,
            "fee_cost": [0.0] * rows,
            "slippage_cost": [0.0] * rows,
            "funding_return": [0.0] * rows,
            "net_return": net_returns,
            "equity": equities,
            "drawdown": drawdowns,
            "gross_exposure": [1.0] * rows,
            "net_exposure": [1.0] * rows,
            "turnover": [0.0] * rows,
        },
        schema_overrides={"timestamp": pl.Datetime("ms", "UTC")},
    )


def _risk_events() -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "event_id": ["risk-1"],
            "evaluation_time": [START],
            "trigger_time": [START],
            "fill_time": [None],
            "symbol": ["BTCUSDT"],
            "event_type": ["STOP_LOSS"],
            "direction": ["LONG"],
            "entry_price": [100.0],
            "trigger_level": [98.0],
            "observed_price": [97.0],
            "action": ["CLOSE"],
            "reason_code": [V2ReasonCode.STOP_LOSS_TRIGGERED.value],
            "run_id": [RUN_ID],
        },
        schema_overrides={
            "evaluation_time": pl.Datetime("ms", "UTC"),
            "trigger_time": pl.Datetime("ms", "UTC"),
            "fill_time": pl.Datetime("ms", "UTC"),
        },
    ).lazy()


def _linked_trades(source_event_id: str = "risk-1") -> pl.LazyFrame:
    return pl.DataFrame(
        {
            "fill_time": [START + timedelta(minutes=1)],
            "source_event_id": [source_event_id],
            "status": ["FILLED"],
        },
        schema_overrides={"fill_time": pl.Datetime("ms", "UTC")},
    ).lazy()


def test_a21_metrics_reduce_lazy_ledger_without_row_materialization() -> None:
    ledger = _return_ledger(20_000)
    metrics = compute_run_metrics(ledger.lazy(), base_interval="1m")
    assert metrics.performance.observations == 20_000
    assert metrics.performance.ending_equity == pytest.approx(
        ledger.item(-1, "equity")
    )
    assert metrics.risk.total_turnover == 0.0
    assert metrics.attribution.maximum_identity_error == 0.0


def test_a21_report_return_sample_is_bounded_and_keeps_extremes(
    tmp_path: Path,
) -> None:
    table = tmp_path / "tables"
    table.mkdir()
    ledger = _return_ledger(10_000).with_columns(
        pl.when(pl.col("timestamp") == START + timedelta(minutes=4_321))
        .then(-0.75)
        .otherwise(pl.col("drawdown"))
        .alias("drawdown")
    )
    ledger.write_parquet(table / "returns.parquet")
    sampled, source_rows = _bounded_returns(tmp_path)
    assert source_rows == 10_000
    assert sampled.height <= 1_360
    assert sampled.item(0, "timestamp") == START
    assert sampled.item(-1, "timestamp") == START + timedelta(minutes=9_999)
    assert sampled["drawdown"].min() == -0.75


def test_a21_event_time_sample_has_fixed_bound() -> None:
    events = pl.DataFrame(
        {
            "event_time": [START + timedelta(minutes=i) for i in range(50_000)]
        },
        schema_overrides={"event_time": pl.Datetime("ms", "UTC")},
    ).lazy()
    sampled = _bounded_event_times(events, limit=40)
    assert len(sampled) <= 40
    assert sampled[0] == START
    assert sampled[-1] == START + timedelta(minutes=49_999)


def test_a21_risk_linking_stays_lazy_and_validates_outcomes() -> None:
    linked = link_risk_event_fills_lazy(
        _risk_events(), _linked_trades(), run_id=RUN_ID
    )
    assert isinstance(linked, pl.LazyFrame)
    assert linked.collect().item(0, "fill_time") == START + timedelta(minutes=1)
    with pytest.raises(EventArbitrationError, match="must link"):
        link_risk_event_fills_lazy(
            _risk_events(), _linked_trades("other-risk"), run_id=RUN_ID
        )


def test_a21_primary_key_check_scans_published_parquet(tmp_path: Path) -> None:
    path = tmp_path / "rankings.parquet"
    frame = pl.DataFrame(
        {
            "timestamp": [START, START],
            "factor_name": ["momentum", "momentum"],
            "symbol": ["BTCUSDT", "BTCUSDT"],
        },
        schema_overrides={"timestamp": pl.Datetime("ms", "UTC")},
    )
    frame.write_parquet(path)
    with pytest.raises(ArtifactStoreError, match="primary key"):
        V2RunArtifactStore._validate_primary_key(path, dataset="rankings")


def test_a21_interactive_payload_records_source_and_sample_sizes(
    tmp_path: Path,
) -> None:
    table = tmp_path / "tables"
    table.mkdir()
    _return_ledger(10_000).write_parquet(table / "returns.parquet")
    sampled, source_rows = _bounded_returns(tmp_path)
    payload = json.loads(
        _interactive_payload(
            tmp_path,
            sampled,
            source_return_rows=source_rows,
        )
    )
    assert payload["limits"]["source_return_rows"] == 10_000
    assert payload["limits"]["return_sample_rows"] <= 1_360
    assert len(payload["points"]) <= 1_360
