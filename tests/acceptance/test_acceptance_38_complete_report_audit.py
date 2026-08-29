"""A38: reports may compact curves but must never sample execution audit rows."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from bianbt.reports.renderer import _bounded_returns, _interactive_payload


START = datetime(2026, 7, 1, tzinfo=timezone.utc)
UTC_MS = pl.Datetime("ms", "UTC")


def _returns(rows: int) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "timestamp": [START + timedelta(minutes=index) for index in range(rows)],
            "gross_price_return": [0.0] * rows,
            "fee_cost": [0.0] * rows,
            "slippage_cost": [0.0] * rows,
            "funding_return": [0.0] * rows,
            "net_return": [0.0] * rows,
            "equity": [1_000.0] * rows,
            "drawdown": [0.0] * rows,
            "gross_exposure": [0.0] * rows,
            "net_exposure": [0.0] * rows,
            "turnover": [0.0] * rows,
        },
        schema_overrides={"timestamp": UTC_MS},
    )


def test_a38_preserves_every_trade_and_exact_neighbor_position_state(
    tmp_path: Path,
) -> None:
    tables = tmp_path / "tables"
    tables.mkdir()
    _returns(10_000).write_parquet(tables / "returns.parquet")

    trade_times = [START + timedelta(minutes=index * 10 + 5) for index in range(600)]
    rows = [
        {
            "fill_time": timestamp,
            "sequence": index + 1,
            "symbol": f"S{index}",
            "side": "BUY",
            "notional": 100.0,
        }
        for index, timestamp in enumerate(trade_times)
    ]
    rows.extend(
        {
            "fill_time": trade_times[index],
            "sequence": 601 + index,
            "symbol": f"X{index}",
            "side": "SELL",
            "notional": 50.0,
        }
        for index in range(102)
    )
    pl.DataFrame(rows).with_columns(pl.col("fill_time").cast(UTC_MS)).write_parquet(
        tables / "trades.parquet"
    )

    focus_index = 300
    focus_time = trade_times[focus_index]
    pl.DataFrame(
        {
            "timestamp": [
                focus_time - timedelta(minutes=1),
                focus_time + timedelta(minutes=1),
            ],
            "symbol": [f"S{focus_index}", f"S{focus_index}"],
            "quantity": [1.0, 2.0],
            "actual_weight": [0.1, 0.2],
            "mark_price": [10.0, 10.0],
            "unrealized_pnl": [0.0, 1.0],
        }
    ).write_parquet(tables / "positions.parquet")

    sampled, source_rows = _bounded_returns(tmp_path)
    payload = json.loads(
        _interactive_payload(
            tmp_path,
            sampled,
            source_return_rows=source_rows,
        )
    )

    def rows(snapshot: dict[str, object], key: str) -> list[dict[str, object]]:
        fields = payload["row_schemas"][key]
        return [dict(zip(fields, values, strict=True)) for values in snapshot[key]]

    preserved_trades = [
        trade
        for snapshot in payload["snapshots"].values()
        for trade in rows(snapshot, "trades")
    ]
    assert len(preserved_trades) == 702
    assert {trade["sequence"] for trade in preserved_trades} == set(range(1, 703))
    assert payload["limits"]["preserved_event_times"] == 600
    assert payload["limits"]["preserved_trade_rows"] == 702

    point_times = {point["timestamp"] for point in payload["points"]}
    expected_times = {
        timestamp.isoformat().replace("+00:00", "Z") for timestamp in trade_times
    }
    assert expected_times <= point_times

    focus_key = focus_time.isoformat().replace("+00:00", "Z")
    snapshot = payload["snapshots"][focus_key]
    assert snapshot["position_before_time"] == (
        focus_time - timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    assert snapshot["position_after_time"] == (
        focus_time + timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    before = rows(snapshot, "positions_before")
    after = rows(snapshot, "positions_after")
    assert before[0]["symbol"] == f"S{focus_index}"
    assert before[0]["quantity"] == 1.0
    assert after[0]["quantity"] == 2.0
