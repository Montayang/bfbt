from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bfbt.config.factor import FactorDefinition
from bfbt.factors.gtja191 import gtja191_raw


UTC_MS = pl.Datetime("ms", "UTC")


def _bars(closes: list[float], *, gap_after: int | None = None) -> pl.LazyFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    offsets = list(range(len(closes)))
    if gap_after is not None:
        offsets = [value + (1 if value > gap_after else 0) for value in offsets]
    opens = [start + timedelta(minutes=value) for value in offsets]
    return pl.DataFrame(
        {
            "open_time": pl.Series(opens, dtype=UTC_MS),
            "close_time": pl.Series(
                [value + timedelta(minutes=1) for value in opens], dtype=UTC_MS
            ),
            "symbol": ["BTCUSDT"] * len(closes),
            "interval": ["1m"] * len(closes),
            "close": closes,
            "volume": [1.0] * len(closes),
            "quote_volume": [100.0] * len(closes),
            "is_complete": [True] * len(closes),
            "dataset_version": ["bars-v1"] * len(closes),
        }
    ).lazy()


def _definition(alpha: int, **parameters: str) -> FactorDefinition:
    return FactorDefinition(
        name=f"gtja_alpha{alpha:03d}",
        version="v1",
        parameters=parameters,
        compute_interval="1m",
    )


@pytest.mark.parametrize(
    ("alpha", "expected"),
    [
        (18, 30.0 / 25.0),
        (20, (30.0 - 24.0) / 24.0 * 100.0),
        (31, (30.0 - 24.5) / 24.5 * 100.0),
        (53, 100.0),
        (66, (30.0 - 27.5) / 27.5 * 100.0),
        (71, (30.0 - 18.5) / 18.5 * 100.0),
        (88, (30.0 - 10.0) / 10.0 * 100.0),
        (112, 100.0),
    ],
)
def test_selected_closed_form_factors(alpha: int, expected: float) -> None:
    result = gtja191_raw(
        _bars([float(value) for value in range(1, 31)]),
        _definition(alpha),
        base_interval="1m",
        alpha=alpha,
    ).collect()

    assert result[-1, "raw_value"] == pytest.approx(expected)


@pytest.mark.parametrize(("alpha", "expected"), [(24, 5.0), (151, 20.0)])
def test_recursive_sma_factors_converge_on_constant_difference(
    alpha: int, expected: float
) -> None:
    result = gtja191_raw(
        _bars([float(value) for value in range(1, 81)]),
        _definition(alpha),
        base_interval="1m",
        alpha=alpha,
    ).collect()

    assert result[-1, "raw_value"] == pytest.approx(expected)


def test_alpha40_volume_field_control() -> None:
    closes = [100.0 + (1.0 if index % 3 else -1.0) * index for index in range(40)]
    bars = _bars(closes).with_columns(
        pl.when(pl.int_range(pl.len()) % 2 == 0).then(10.0).otherwise(1.0).alias("volume"),
        pl.when(pl.int_range(pl.len()) % 2 == 0).then(1.0).otherwise(10.0).alias("quote_volume"),
    )
    quote = gtja191_raw(
        bars, _definition(40, volume_field="quote_volume"), base_interval="1m", alpha=40
    ).collect()
    base = gtja191_raw(
        bars, _definition(40, volume_field="volume"), base_interval="1m", alpha=40
    ).collect()

    assert quote[-1, "raw_value"] != base[-1, "raw_value"]


def test_gap_resets_bar_count_history() -> None:
    result = gtja191_raw(
        _bars([float(value) for value in range(40)], gap_after=20),
        _definition(18),
        base_interval="1m",
        alpha=18,
    ).collect()

    assert result[21, "raw_value"] is None
    assert result[26, "raw_value"] is not None
