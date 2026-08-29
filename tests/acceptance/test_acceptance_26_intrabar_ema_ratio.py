"""A26 acceptance for slower-candle EMA state updated every base bar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from bianbt.config.factor import FactorDefinition
from bianbt.factors.base import FactorError
from bianbt.factors.registry import compute_factor

START = datetime(2026, 7, 1, tzinfo=timezone.utc)
BARS_VERSION = "bars-a26"
UNIVERSE_VERSION = "universe-a26"
UTC_MS = pl.Datetime("ms", "UTC")


def _bars(prices: tuple[float, ...]) -> pl.LazyFrame:
    return pl.DataFrame(
        [
            {
                "open_time": START + timedelta(minutes=index),
                "close_time": START + timedelta(minutes=index + 1),
                "symbol": "BTCUSDT",
                "interval": "1m",
                "close": price,
                "is_complete": True,
                "dataset_version": BARS_VERSION,
            }
            for index, price in enumerate(prices)
        ]
    ).with_columns(
        pl.col("open_time").cast(UTC_MS),
        pl.col("close_time").cast(UTC_MS),
    ).lazy()


def _universe(minutes: tuple[int, ...]) -> pl.LazyFrame:
    return pl.DataFrame(
        [
            {
                "timestamp": START + timedelta(minutes=minute),
                "symbol": "BTCUSDT",
                "is_eligible": True,
                "universe_version": UNIVERSE_VERSION,
            }
            for minute in minutes
        ]
    ).with_columns(pl.col("timestamp").cast(UTC_MS)).lazy()


def _definition(**parameters: object) -> FactorDefinition:
    return FactorDefinition.model_validate(
        {
            "name": "intrabar_ema_ratio",
            "version": "v1",
            "parameters": {
                "source_interval": "2m",
                "fast_span": 2,
                "slow_span": 3,
                **parameters,
            },
            "compute_interval": "1m",
        }
    )


def _compute(
    bars: pl.LazyFrame,
    minutes: tuple[int, ...],
    *,
    initial_state: pl.DataFrame | None = None,
    state_start: datetime | None = None,
):
    return compute_factor(
        bars,
        _universe(minutes),
        _definition(),
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        universe_version=UNIVERSE_VERSION,
        initial_state=initial_state,
        state_start=state_start,
    )


def test_intrabar_close_revises_one_slow_candle_without_recursive_overcount() -> None:
    prices = (95.0, 100.0, 105.0, 110.0, 120.0, 130.0)
    result = _compute(_bars(prices), tuple(range(1, 7)))
    rows = result.frame.collect().filter(pl.col("is_valid")).to_dicts()
    assert [row["timestamp"] for row in rows] == [
        START + timedelta(minutes=5),
        START + timedelta(minutes=6),
    ]
    previous_fast = 2.0 / 3.0 * 110.0 + 1.0 / 3.0 * 100.0
    previous_slow = 0.5 * 110.0 + 0.5 * 100.0
    expected_at_five = (
        (2.0 / 3.0 * 120.0 + 1.0 / 3.0 * previous_fast)
        / (0.5 * 120.0 + 0.5 * previous_slow)
        - 1.0
    )
    expected_at_six = (
        (2.0 / 3.0 * 130.0 + 1.0 / 3.0 * previous_fast)
        / (0.5 * 130.0 + 0.5 * previous_slow)
        - 1.0
    )
    assert rows[0]["raw_value"] == pytest.approx(expected_at_five)
    assert rows[1]["raw_value"] == pytest.approx(expected_at_six)
    assert rows[0]["raw_value"] != pytest.approx(rows[1]["raw_value"])


def test_future_minute_does_not_change_prior_intrabar_value() -> None:
    original = _compute(
        _bars((95.0, 100.0, 105.0, 110.0, 120.0, 130.0)),
        (5,),
    ).frame.collect().item(0, "raw_value")
    changed = _compute(
        _bars((95.0, 100.0, 105.0, 110.0, 120.0, 9_999.0)),
        (5,),
    ).frame.collect().item(0, "raw_value")
    assert changed == pytest.approx(original)


def test_carried_state_matches_one_pass_at_chunk_boundary() -> None:
    prices = (95.0, 100.0, 105.0, 110.0, 120.0, 130.0, 125.0, 140.0)
    full = _compute(_bars(prices), tuple(range(1, 9))).frame.collect()
    first = _compute(_bars(prices[:6]), tuple(range(1, 6)))
    assert first.state is not None
    assert first.state.item(0, "sample_count") == 3
    boundary = START + timedelta(minutes=6)
    second = _compute(
        _bars(prices),
        (6, 7, 8),
        initial_state=first.state,
        state_start=boundary,
    )
    combined = pl.concat([first.frame.collect(), second.frame.collect()]).sort(
        ["timestamp", "symbol"]
    )
    assert_frame_equal(combined, full)
    assert second.state is not None
    assert second.state.item(0, "sample_count") == 4


@pytest.mark.parametrize(
    "parameters",
    [
        {"source_interval": "1m"},
        {"fast_span": 3, "slow_span": 3},
        {"fast_span": 0},
    ],
)
def test_invalid_ema_clock_or_spans_fail_closed(parameters: dict[str, object]) -> None:
    definition = _definition(**parameters)
    with pytest.raises(FactorError):
        compute_factor(
            _bars((100.0, 101.0)),
            _universe((1, 2)),
            definition,
            base_interval="1m",
            bars_dataset_version=BARS_VERSION,
            universe_version=UNIVERSE_VERSION,
        )


def test_state_start_requires_matching_state_and_source_alignment() -> None:
    bars = _bars((100.0, 101.0))
    with pytest.raises(FactorError, match="supplied together"):
        _compute(bars, (1, 2), state_start=START)


def test_report_renders_exact_r3_formula_and_clocks() -> None:
    from bianbt.reports.renderer import _factor_html, _v2_audit_html

    definition = _definition(
        source_interval="15m", fast_span=7, slow_span=25
    ).model_dump(mode="json")
    html = _factor_html("intrabar_ema_ratio", definition)
    assert "EMA7_15m_live(t) / EMA25_15m_live(t) - 1" in html
    assert "计算频率 / Compute Interval</th><td>1m" in html
    assert "EMA K线周期 / EMA Candle Interval</th><td>15m" in html
    audit = _v2_audit_html(
        {
            "factor": {"factors": [definition]},
            "backtest": {
                "config_version": "v2",
                "portfolio": {
                    "selection": {
                        "mode": "rank_descent",
                        "clock": "factor",
                        "audit_top_n": 7,
                        "descent": {
                            "start_rank_at_least": 7,
                            "entry_rank": 1,
                            "equal_policy": "keep",
                            "increase_policy": "reset",
                        },
                    },
                    "sizing": {"mode": "fixed_margin"},
                    "holding": {},
                    "constraints": {},
                },
                "schedule": {},
                "risk": {"symbol_exits": {}},
                "capital": {},
            },
        }
    )
    assert "盘中 EMA 比值因子（intrabar_ema_ratio）原始分数降序排名" in audit
    assert "原始 24h 涨幅" not in audit
