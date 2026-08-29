"""User-run offline acceptance suite for A07; Codex does not execute it."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from bianbt.cli import app
from bianbt.config.factor import FactorDefinition, LabelDefinition
from bianbt.factors.base import FactorError
from bianbt.factors.registry import compute_factor, list_factors
from bianbt.labels.forward_returns import compute_forward_returns
from bianbt.research.evaluator import evaluate_factor

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
BARS_VERSION = "bars-a07-fixture"
UNIVERSE_VERSION = "universe-a07-fixture"


def _bars(
    series: dict[str, tuple[float, ...]],
    *,
    minutes: tuple[int, ...] | None = None,
    incomplete: set[tuple[str, int]] | None = None,
    version: str = BARS_VERSION,
) -> pl.LazyFrame:
    rows = []
    incomplete = incomplete or set()
    for symbol_index, (symbol, prices) in enumerate(series.items()):
        selected_minutes = minutes or tuple(range(len(prices)))
        assert len(selected_minutes) == len(prices)
        for minute, price in zip(selected_minutes, prices):
            open_time = START + timedelta(minutes=minute)
            quote_volume = float(10 * (symbol_index + 1))
            rows.append(
                {
                    "open_time": open_time,
                    "close_time": open_time + timedelta(minutes=1),
                    "symbol": symbol,
                    "interval": "1m",
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price,
                    "volume": 1.0,
                    "quote_volume": quote_volume,
                    "trades": 2,
                    "taker_buy_volume": 0.5,
                    "taker_buy_quote_volume": quote_volume / 4,
                    "is_complete": (symbol, minute) not in incomplete,
                    "source": "fixture",
                    "source_object_id": f"{symbol}-{minute}",
                    "dataset_version": version,
                }
            )
    return pl.DataFrame(rows).lazy()


def _universe(
    symbols: tuple[str, ...],
    timestamps: tuple[datetime, ...],
    *,
    ineligible: set[tuple[datetime, str]] | None = None,
) -> pl.LazyFrame:
    ineligible = ineligible or set()
    return pl.DataFrame(
        [
            {
                "timestamp": timestamp,
                "symbol": symbol,
                "is_eligible": (timestamp, symbol) not in ineligible,
                "reason_code": (
                    "ILLIQUID" if (timestamp, symbol) in ineligible else "ELIGIBLE"
                ),
                "universe_version": UNIVERSE_VERSION,
            }
            for timestamp in timestamps
            for symbol in symbols
        ]
    ).lazy()


def _factor(
    name: str = "momentum",
    *,
    parameters: dict[str, object] | None = None,
    preprocess: list[dict[str, object]] | None = None,
) -> FactorDefinition:
    return FactorDefinition.model_validate(
        {
            "name": name,
            "version": "v1",
            "parameters": parameters or {"lookback": "2m"},
            "compute_interval": "1m",
            "preprocess": preprocess or [],
        }
    )


def _compute(
    bars: pl.LazyFrame,
    universe: pl.LazyFrame,
    definition: FactorDefinition,
):
    return compute_factor(
        bars,
        universe,
        definition,
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        universe_version=UNIVERSE_VERSION,
    )


def _label(horizon: str = "2m") -> LabelDefinition:
    return LabelDefinition.model_validate(
        {
            "name": f"forward_return_{horizon}",
            "signal_delay_bars": 1,
            "horizon": horizon,
            "entry_field": "open",
            "exit_field": "open",
        }
    )


def test_registry_exposes_supported_factors() -> None:
    assert {item.name for item in list_factors()} == {
        "momentum",
        "quote_volume",
        "realized_volatility",
        "reversal",
        "taker_buy_ratio",
        "amihud_illiquidity",
        "intrabar_ema_ratio",
        "sampled_mean_ratio",
        "sampled_mean_ratio_inverse",
        "gtja_alpha018",
        "gtja_alpha020",
        "gtja_alpha024",
        "gtja_alpha031",
        "gtja_alpha040",
        "gtja_alpha053",
        "gtja_alpha066",
        "gtja_alpha071",
        "gtja_alpha088",
        "gtja_alpha089",
        "gtja_alpha112",
        "gtja_alpha151",
    }


def test_momentum_uses_only_bars_closed_by_signal_time() -> None:
    signal = START + timedelta(minutes=3)
    universe = _universe(("BTCUSDT",), (signal,))
    original = _bars({"BTCUSDT": (100.0, 110.0, 120.0, 130.0, 140.0)})
    changed_future = _bars(
        {"BTCUSDT": (100.0, 110.0, 120.0, 9_999.0, 1.0)}
    )
    first = _compute(original, universe, _factor()).frame.collect().to_dicts()[0]
    second = (
        _compute(changed_future, universe, _factor())
        .frame.collect()
        .to_dicts()[0]
    )
    assert first["raw_value"] == pytest.approx(0.2)
    assert second["raw_value"] == pytest.approx(first["raw_value"])


def test_gapped_lookback_is_invalid_instead_of_bridged() -> None:
    signal = START + timedelta(minutes=4)
    bars = _bars(
        {"BTCUSDT": (100.0, 110.0, 120.0)},
        minutes=(0, 2, 3),
    )
    row = (
        _compute(bars, _universe(("BTCUSDT",), (signal,)), _factor())
        .frame.collect()
        .to_dicts()[0]
    )
    assert row["is_valid"] is False
    assert row["invalid_reason"] == "INSUFFICIENT_OR_GAPPED_HISTORY"


def test_incomplete_bar_invalidates_momentum_window() -> None:
    signal = START + timedelta(minutes=3)
    bars = _bars(
        {"BTCUSDT": (100.0, 110.0, 120.0)},
        incomplete={("BTCUSDT", 1)},
    )
    row = _compute(
        bars,
        _universe(("BTCUSDT",), (signal,)),
        _factor(),
    ).frame.collect().to_dicts()[0]
    assert row["is_valid"] is False
    assert row["invalid_reason"] == "INSUFFICIENT_OR_GAPPED_HISTORY"


def test_cross_sectional_zscore_uses_only_eligible_symbols() -> None:
    signal = START + timedelta(minutes=3)
    symbols = ("AUSDT", "BUSDT", "CUSDT", "OUTLIER")
    bars = _bars(
        {
            "AUSDT": (100.0, 110.0, 120.0),
            "BUSDT": (100.0, 120.0, 150.0),
            "CUSDT": (100.0, 90.0, 80.0),
            "OUTLIER": (1.0, 1.0, 10_000.0),
        }
    )
    universe = _universe(symbols, (signal,), ineligible={(signal, "OUTLIER")})
    definition = _factor(preprocess=[{"name": "zscore"}])
    result = _compute(bars, universe, definition).frame.collect()
    assert result["symbol"].to_list() == ["AUSDT", "BUSDT", "CUSDT"]
    assert sum(result["value"].to_list()) == pytest.approx(0.0)
    assert result["value"].std(ddof=0) == pytest.approx(1.0)


def test_rank_transform_has_stable_zero_to_one_scale() -> None:
    signal = START + timedelta(minutes=3)
    symbols = ("AUSDT", "BUSDT", "CUSDT")
    bars = _bars(
        {
            "AUSDT": (100.0, 100.0, 90.0),
            "BUSDT": (100.0, 100.0, 110.0),
            "CUSDT": (100.0, 100.0, 120.0),
        }
    )
    definition = _factor(preprocess=[{"name": "rank"}])
    values = _compute(
        bars, _universe(symbols, (signal,)), definition
    ).frame.collect()["value"].to_list()
    assert values == [0.0, 0.5, 1.0]


@pytest.mark.parametrize(
    ("name", "parameters", "expected"),
    [
        ("reversal", {"lookback": "2m"}, -0.21),
        ("quote_volume", {"window": "2m"}, 20.0),
        ("taker_buy_ratio", {"window": "2m"}, 0.25),
        ("realized_volatility", {"window": "2m"}, 0.0),
        (
            "amihud_illiquidity",
            {"window": "2m"},
            math.log(1.1) / 10 * 1_000_000,
        ),
    ],
)
def test_builtin_factor_formulas(
    name: str, parameters: dict[str, object], expected: float
) -> None:
    signal = START + timedelta(minutes=3)
    bars = _bars({"BTCUSDT": (100.0, 110.0, 121.0)})
    row = _compute(
        bars,
        _universe(("BTCUSDT",), (signal,)),
        _factor(name, parameters=parameters),
    ).frame.collect().to_dicts()[0]
    assert row["is_valid"] is True
    assert row["raw_value"] == pytest.approx(
        expected, rel=1e-12, abs=1e-12
    )


def test_forward_label_has_exact_next_open_and_horizon_exit() -> None:
    signal = START + timedelta(minutes=3)
    bars = _bars(
        {"BTCUSDT": (100.0, 110.0, 120.0, 130.0, 140.0, 150.0, 160.0)}
    )
    result = compute_forward_returns(
        bars,
        _universe(("BTCUSDT",), (signal,)),
        _label(),
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        universe_version=UNIVERSE_VERSION,
    )
    row = result.frame.collect().to_dicts()[0]
    assert row["entry_time"] == signal
    assert row["exit_time"] == signal + timedelta(minutes=2)
    assert row["entry_price"] == 130.0
    assert row["exit_price"] == 150.0
    assert row["forward_return"] == pytest.approx(150.0 / 130.0 - 1)


def test_forward_label_rejects_missing_or_gapped_future() -> None:
    signal = START + timedelta(minutes=3)
    short = _bars({"BTCUSDT": (100.0, 110.0, 120.0, 130.0)})
    short_row = compute_forward_returns(
        short,
        _universe(("BTCUSDT",), (signal,)),
        _label(),
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect().to_dicts()[0]
    gapped = _bars(
        {"BTCUSDT": (100.0, 110.0, 120.0, 130.0, 150.0, 160.0)},
        minutes=(0, 1, 2, 3, 5, 6),
    )
    gap_row = compute_forward_returns(
        gapped,
        _universe(("BTCUSDT",), (signal,)),
        _label(),
        base_interval="1m",
        bars_dataset_version=BARS_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect().to_dicts()[0]
    assert short_row["invalid_reason"] == "INSUFFICIENT_FUTURE"
    assert gap_row["invalid_reason"] == "GAPPED_FUTURE"


def test_research_outputs_align_counts_ic_quantiles_and_coverage() -> None:
    timestamp = START + timedelta(minutes=3)
    symbols = ("A", "B", "C", "NO_FACTOR")
    universe = _universe(symbols, (timestamp,))
    factors = pl.DataFrame(
        {
            "timestamp": [timestamp] * 3,
            "symbol": ["A", "B", "C"],
            "value": [1.0, 2.0, 3.0],
            "is_valid": [True, True, True],
        }
    ).lazy()
    labels = pl.DataFrame(
        {
            "timestamp": [timestamp] * 3,
            "symbol": ["A", "B", "C"],
            "forward_return": [-0.1, 0.0, 0.1],
            "is_valid": [True, True, True],
        }
    ).lazy()
    evaluation = evaluate_factor(
        factors,
        labels,
        universe,
        universe_version=UNIVERSE_VERSION,
        quantiles=3,
    )
    ic = evaluation.ic.collect().to_dicts()[0]
    coverage = evaluation.coverage.collect().to_dicts()[0]
    quantiles = evaluation.quantile_returns.collect()
    assert ic["sample_count"] == 3
    assert ic["ic"] == pytest.approx(1.0)
    assert ic["rank_ic"] == pytest.approx(1.0)
    assert coverage["eligible_count"] == 4
    assert coverage["aligned_valid_count"] == 3
    assert quantiles["quantile"].to_list() == [1, 2, 3]


def test_factor_rank_turnover_reports_common_symbol_changes() -> None:
    first = START + timedelta(minutes=3)
    second = START + timedelta(minutes=4)
    factors = pl.DataFrame(
        {
            "timestamp": [first] * 3 + [second] * 3,
            "symbol": ["A", "B", "C"] * 2,
            "value": [1.0, 2.0, 3.0, 3.0, 2.0, 1.0],
            "is_valid": [True] * 6,
        }
    ).lazy()
    turnover = evaluate_factor(
        factors,
        pl.DataFrame(
            {
                "timestamp": [],
                "symbol": [],
                "forward_return": [],
                "is_valid": [],
            },
            schema={
                "timestamp": pl.Datetime("us", "UTC"),
                "symbol": pl.String,
                "forward_return": pl.Float64,
                "is_valid": pl.Boolean,
            },
        ).lazy(),
        _universe(("A", "B", "C"), (first, second)),
        universe_version=UNIVERSE_VERSION,
    ).turnover.collect().to_dicts()
    assert turnover[1]["sample_count"] == 3
    assert turnover[1]["rank_turnover"] == pytest.approx(2 / 3)


def test_versions_are_deterministic_and_latest_is_rejected() -> None:
    signal = START + timedelta(minutes=3)
    bars = _bars({"BTCUSDT": (100.0, 110.0, 120.0)})
    universe = _universe(("BTCUSDT",), (signal,))
    first = _compute(bars, universe, _factor())
    second = _compute(bars, universe, _factor())
    assert first.factor_version == second.factor_version
    with pytest.raises(FactorError, match="explicit"):
        compute_factor(
            bars,
            universe,
            _factor(),
            base_interval="1m",
            bars_dataset_version="latest",
            universe_version=UNIVERSE_VERSION,
        )


def test_a07_cli_commands_expose_explicit_history_and_future_bounds() -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["research", "list-factors"]).exit_code == 0
    assert runner.invoke(app, ["research", "preview", "--help"]).exit_code == 0
    root = get_command(app)
    preview = root.commands["research"].commands["preview"]
    options = {
        option
        for parameter in preview.params
        for option in getattr(parameter, "opts", ())
    }
    assert {
        "--history-start",
        "--future-end",
        "--universe-config",
        "--factor-config",
        "--limit",
    } <= options
