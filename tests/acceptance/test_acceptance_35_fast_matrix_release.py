"""A35 release-level diagnostics and public backend naming."""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl

from bianbt.engine.fast_matrix.equivalence import first_return_difference


def _returns(equity: float) -> pl.DataFrame:
    return pl.DataFrame({
        "timestamp": [datetime(2026, 1, 1, tzinfo=timezone.utc)],
        "gross_price_return": [0.0], "fee_cost": [0.0], "slippage_cost": [0.0],
        "funding_return": [0.0], "net_return": [0.0], "equity": [equity],
        "drawdown": [0.0], "gross_exposure": [0.0], "net_exposure": [0.0],
        "turnover": [0.0], "run_id": ["fm-" + "a" * 24],
    })


def test_equivalence_audit_reports_first_timestamp_and_field() -> None:
    assert first_return_difference(_returns(1000.0), _returns(1000.0)) is None
    difference = first_return_difference(_returns(1000.0), _returns(999.0))
    assert difference is not None
    assert difference.field == "equity"
    assert difference.absolute_error == 1.0
