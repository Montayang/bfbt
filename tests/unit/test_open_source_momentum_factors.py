from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import log, sqrt

import polars as pl
import pytest

from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError
from bfbt.factors.open_source_momentum import (
    SUPPORTED_FACTORS,
    open_source_momentum_raw,
)


UTC_MS = pl.Datetime("ms", "UTC")


def _bars(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
    gap_after: int | None = None,
) -> pl.LazyFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    offsets = list(range(len(closes)))
    if gap_after is not None:
        offsets = [value + (value > gap_after) for value in offsets]
    opens = [start + timedelta(minutes=value) for value in offsets]
    highs = highs or [value + 0.5 for value in closes]
    lows = lows or [value - 0.5 for value in closes]
    volumes = volumes or [100.0] * len(closes)
    return pl.DataFrame(
        {
            "open_time": pl.Series(opens, dtype=UTC_MS),
            "close_time": pl.Series(
                [value + timedelta(minutes=1) for value in opens],
                dtype=UTC_MS,
            ),
            "symbol": ["BTCUSDT"] * len(closes),
            "interval": ["1m"] * len(closes),
            "close": closes,
            "high": highs,
            "low": lows,
            "volume": volumes,
            "quote_volume": volumes,
            "is_complete": [True] * len(closes),
            "dataset_version": ["bars-v1"] * len(closes),
        }
    ).lazy()


def _definition(name: str, **parameters: object) -> FactorDefinition:
    return FactorDefinition(
        name=name,
        version="v1",
        parameters=parameters,
        compute_interval="1m",
    )


def _compute(
    name: str,
    bars: pl.LazyFrame,
    **parameters: object,
) -> pl.DataFrame:
    return open_source_momentum_raw(
        bars,
        _definition(name, **parameters),
        base_interval="1m",
        factor=name,
    ).collect()


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left, right)
    )
    left_square = sum((value - left_mean) ** 2 for value in left)
    right_square = sum((value - right_mean) ** 2 for value in right)
    return covariance / sqrt(left_square * right_square)


def _ema(
    values: list[float | None], span: int, minimum: int
) -> list[float | None]:
    alpha = 2.0 / (span + 1.0)
    state: float | None = None
    observations = 0
    output: list[float | None] = []
    for value in values:
        if value is None:
            output.append(None)
            continue
        observations += 1
        state = value if state is None else alpha * value + (1 - alpha) * state
        output.append(state if observations >= minimum else None)
    return output


def test_supported_factor_identity_is_complete() -> None:
    assert len(SUPPORTED_FACTORS) == 14
    assert len(set(SUPPORTED_FACTORS)) == len(SUPPORTED_FACTORS)


def test_qlib_price_factors_match_closed_forms() -> None:
    closes = [float(value) for value in range(1, 41)]
    bars = _bars(closes)

    assert _compute(
        "oss_qlib_beta", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(1.0 / 40.0)
    assert _compute(
        "oss_qlib_signed_rsqr", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(1.0)
    assert _compute(
        "oss_qlib_rsv", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(19.5 / 20.0)
    assert _compute(
        "oss_qlib_imxd", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(19.0 / 20.0)
    assert _compute(
        "oss_qlib_cntd", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(1.0)
    assert _compute(
        "oss_qlib_sumd", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(1.0)
    assert _compute(
        "oss_qlib_roc_mom", bars, window_bars=20
    )[-1, "raw_value"] == pytest.approx(1.0)


def test_imxd_uses_qlib_oldest_first_tie_semantics() -> None:
    closes = [10.0] * 8
    highs = [11.0, 12.0, 12.0, 11.0, 10.0, 10.0, 10.0, 10.0]
    lows = [9.0, 9.0, 9.0, 8.0, 8.0, 9.0, 9.0, 9.0]
    value = _compute(
        "oss_qlib_imxd",
        _bars(closes, highs=highs, lows=lows),
        window_bars=8,
    )[-1, "raw_value"]

    assert value == pytest.approx((2.0 - 4.0) / 8.0)


@pytest.mark.parametrize(
    "name",
    ["oss_qlib_beta", "oss_qlib_signed_rsqr", "oss_qlib_imxd"],
)
def test_fixed_window_factors_do_not_emit_partial_history(name: str) -> None:
    result = _compute(
        name,
        _bars([float(value) for value in range(1, 9)]),
        window_bars=5,
    )

    assert result[:4, "raw_value"].null_count() == 4
    assert result[4, "raw_value"] is not None


def test_signed_rsquared_rejects_constant_price_window() -> None:
    result = _compute(
        "oss_qlib_signed_rsqr", _bars([10.0] * 8), window_bars=5
    )

    assert result[-1, "raw_value"] is None


def test_qlib_cord_matches_rolling_pearson_formula() -> None:
    closes = [100.0]
    volumes = [100.0]
    price_ratios = [1.02, 0.99, 1.03, 1.01, 0.98]
    volume_ratios = [1.20, 0.90, 1.40, 1.10, 0.80]
    for price_ratio, volume_ratio in zip(price_ratios, volume_ratios):
        closes.append(closes[-1] * price_ratio)
        volumes.append(volumes[-1] * volume_ratio)
    expected = _correlation(
        price_ratios,
        [log(value + 1.0) for value in volume_ratios],
    )
    value = _compute(
        "oss_qlib_cord",
        _bars(closes, volumes=volumes),
        window_bars=5,
    )[-1, "raw_value"]

    assert value == pytest.approx(expected)


def test_ta_smoothing_factors_have_expected_direction() -> None:
    closes = [float(value) for value in range(1, 121)]
    bars = _bars(closes)

    trix = _compute("oss_ta_trix", bars, window_bars=15)[-1, "raw_value"]
    tsi = _compute(
        "oss_ta_tsi", bars, slow_span=25, fast_span=13
    )[-1, "raw_value"]
    kama = _compute(
        "oss_ta_kama_distance",
        bars,
        efficiency_bars=10,
        fast_span=2,
        slow_span=30,
    )[-1, "raw_value"]

    assert trix > 0
    assert tsi == pytest.approx(100.0)
    assert kama > 0


def test_trix_and_tsi_match_independent_recursive_reference() -> None:
    closes = [100.0 + index * 0.4 + (index % 5) * 0.7 for index in range(80)]
    trix_first = _ema(closes, 5, 5)
    trix_second = _ema(trix_first, 5, 5)
    trix_third = _ema(trix_second, 5, 5)
    assert trix_third[-1] is not None and trix_third[-2] is not None
    expected_trix = (trix_third[-1] / trix_third[-2] - 1.0) * 100.0

    changes: list[float | None] = [None] + [
        closes[index] - closes[index - 1]
        for index in range(1, len(closes))
    ]
    tsi_numerator = _ema(_ema(changes, 7, 7), 3, 3)
    tsi_denominator = _ema(
        _ema([None if value is None else abs(value) for value in changes], 7, 7),
        3,
        3,
    )
    assert tsi_numerator[-1] is not None and tsi_denominator[-1] is not None
    expected_tsi = 100.0 * tsi_numerator[-1] / tsi_denominator[-1]

    assert _compute(
        "oss_ta_trix", _bars(closes), window_bars=5
    )[-1, "raw_value"] == pytest.approx(expected_trix)
    assert _compute(
        "oss_ta_tsi", _bars(closes), slow_span=7, fast_span=3
    )[-1, "raw_value"] == pytest.approx(expected_tsi)


def test_kst_matches_weighted_constant_return_formula() -> None:
    closes = [100.0 * 1.01**index for index in range(100)]
    expected = 100.0 * sum(
        weight * (1.01**lookback - 1.0)
        for weight, lookback in enumerate([10, 15, 20, 30], start=1)
    )
    value = _compute("oss_ta_kst", _bars(closes))[-1, "raw_value"]

    assert value == pytest.approx(expected)
    assert _compute("oss_ta_kst", _bars(closes))[43, "raw_value"] is None
    assert _compute("oss_ta_kst", _bars(closes))[44, "raw_value"] is not None


def test_vortex_and_rolling_vpt_match_closed_forms() -> None:
    closes = [100.0 + index for index in range(40)]
    bars = _bars(closes)
    vortex = _compute(
        "oss_ta_vortex_diff", bars, window_bars=14
    )[-1, "raw_value"]

    geometric = [100.0 * 1.01**index for index in range(30)]
    vpt = _compute(
        "oss_ta_vpt_roll", _bars(geometric), window_bars=20
    )[-1, "raw_value"]

    assert vortex == pytest.approx(2.0 / 1.5)
    assert vpt == pytest.approx(0.01)


@pytest.mark.parametrize(
    "name",
    [
        "oss_qlib_roc_mom",
        "oss_ta_trix",
        "oss_ta_tsi",
        "oss_ta_kama_distance",
        "oss_ta_vortex_diff",
    ],
)
def test_gap_resets_history(name: str) -> None:
    result = _compute(
        name,
        _bars([float(value) for value in range(1, 101)], gap_after=70),
        window_bars=5,
        slow_span=5,
        fast_span=2,
        efficiency_bars=5,
    )

    assert result[71, "raw_value"] is None


def test_future_changes_do_not_rewrite_earlier_value() -> None:
    closes = [float(value) for value in range(1, 41)]
    changed = closes[:30] + [10_000.0] * 10
    original = _compute(
        "oss_qlib_beta", _bars(closes), window_bars=10
    )
    modified = _compute(
        "oss_qlib_beta", _bars(changed), window_bars=10
    )

    assert original[29, "raw_value"] == modified[29, "raw_value"]


@pytest.mark.parametrize(
    ("name", "parameters", "message"),
    [
        ("oss_qlib_beta", {"window_bars": 1}, "window_bars"),
        ("oss_ta_tsi", {"slow_span": 10, "fast_span": 10}, "fast_span"),
        (
            "oss_ta_kama_distance",
            {"fast_span": 30, "slow_span": 2},
            "fast_span",
        ),
        ("oss_ta_kst", {"roc_bars": [1, 2]}, "four-item"),
        ("oss_ta_vpt_roll", {"volume_field": "trades"}, "volume_field"),
    ],
)
def test_invalid_parameters_fail_closed(
    name: str, parameters: dict[str, object], message: str
) -> None:
    with pytest.raises(FactorError, match=message):
        _compute(name, _bars([1.0, 2.0, 3.0]), **parameters)
