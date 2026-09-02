from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError
from bfbt.factors.sampled_mean import (
    sampled_mean_ratio_inverse_raw,
    sampled_mean_ratio_raw,
)


UTC_MS = pl.Datetime("ms", "UTC")
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(*, missing: int | None = None) -> pl.LazyFrame:
    rows = []
    for minute in range(181):
        if minute == missing:
            continue
        opened = START + timedelta(minutes=minute)
        rows.append(
            {
                "open_time": opened,
                "close_time": opened + timedelta(minutes=1),
                "symbol": "BTCUSDT",
                "interval": "1m",
                "close": 100.0 + minute,
                "is_complete": True,
                "dataset_version": "bars-sampled-v1",
            }
        )
    return pl.DataFrame(rows).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    ).lazy()


def _definition(name: str = "sampled_mean_ratio") -> FactorDefinition:
    return FactorDefinition.model_validate(
        {
            "name": name,
            "version": "v1",
            "parameters": {"sample_interval": "15m", "sample_count": 12},
            "compute_interval": "1m",
        }
    )


def test_phase_aligned_twelve_point_formula_and_inverse() -> None:
    positive = sampled_mean_ratio_raw(
        _bars(), _definition(), base_interval="1m"
    ).collect()
    inverse = sampled_mean_ratio_inverse_raw(
        _bars(), _definition("sampled_mean_ratio_inverse"), base_interval="1m"
    ).collect()
    row = positive.filter(pl.col("timestamp") == START + timedelta(minutes=181))
    reverse = inverse.filter(
        pl.col("timestamp") == START + timedelta(minutes=181)
    )
    samples = [280.0 - 15.0 * index for index in range(12)]
    expected = 280.0 / (sum(samples) / len(samples)) - 1.0
    assert row.item(0, "raw_value") == pytest.approx(expected)
    assert reverse.item(0, "raw_value") == pytest.approx(-expected)


def test_missing_row_is_not_bridged_by_row_lag() -> None:
    result = sampled_mean_ratio_raw(
        _bars(missing=165), _definition(), base_interval="1m"
    ).collect()
    row = result.filter(pl.col("timestamp") == START + timedelta(minutes=181))
    assert row.item(0, "raw_value") is None


@pytest.mark.parametrize(
    "parameters",
    [
        {"sample_interval": "90s", "sample_count": 12},
        {"sample_interval": "15m", "sample_count": 1},
    ],
)
def test_invalid_sampling_parameters_fail_closed(parameters: dict[str, object]) -> None:
    definition = FactorDefinition.model_validate(
        {
            "name": "sampled_mean_ratio",
            "version": "v1",
            "parameters": parameters,
            "compute_interval": "1m",
        }
    )
    with pytest.raises(FactorError):
        sampled_mean_ratio_raw(
            _bars(), definition, base_interval="1m"
        ).collect()
