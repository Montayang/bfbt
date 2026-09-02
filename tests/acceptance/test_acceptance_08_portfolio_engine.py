"""User-run offline acceptance suite for A08; Codex does not execute it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest
from pydantic import ValidationError
from typer.main import get_command
from typer.testing import CliRunner

from bfbt.cli import app
from bfbt.config.backtest import (
    BacktestConfig,
    FeeConfig,
    PortfolioConfig,
    SlippageConfig,
)
from bfbt.engine.costs import CostModelError, fee_rate, slippage_rate
from bfbt.engine.execution import fill_time
from bfbt.engine.vectorized import BacktestError, run_vectorized_backtest
from bfbt.portfolio.constraints import construct_portfolio

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
FACTOR_VERSION = "factor-a08-fixture"
UNIVERSE_VERSION = "universe-a08-fixture"
BARS_VERSION = "bars-a08-fixture"
FUNDING_VERSION = "funding-a08-fixture"
PORTFOLIO_VERSION = "portfolio-a08-fixture"


def _portfolio_config(**overrides: object) -> PortfolioConfig:
    payload: dict[str, object] = {
        "construction": "long_short_count",
        "long_count": 1,
        "short_count": 1,
        "weighting": "equal",
        "gross_exposure": 1.0,
        "net_exposure": 0.0,
    }
    payload.update(overrides)
    return PortfolioConfig.model_validate(payload)


def _scores(
    volatility: tuple[float, ...] | None = None,
) -> pl.LazyFrame:
    payload: dict[str, object] = {
        "timestamp": [START] * 4,
        "symbol": ["A", "B", "C", "D"],
        "value": [-2.0, -1.0, 1.0, 2.0],
        "is_valid": [True] * 4,
        "factor_version": [FACTOR_VERSION] * 4,
        "universe_version": [UNIVERSE_VERSION] * 4,
    }
    if volatility is not None:
        payload["volatility"] = volatility
    return pl.DataFrame(payload).lazy()


def _targets(weights: dict[str, float], version: str = PORTFOLIO_VERSION):
    return pl.DataFrame(
        [
            {
                "signal_time": START,
                "symbol": symbol,
                "score": weight,
                "side": "LONG" if weight > 0 else "SHORT",
                "unconstrained_weight": weight,
                "target_weight": weight,
                "constraint_flags": "",
                "portfolio_version": version,
            }
            for symbol, weight in weights.items()
        ]
    ).lazy()


def _bars(prices: dict[str, tuple[tuple[float, float], ...]]):
    rows = []
    for symbol, values in prices.items():
        for minute, (opened, closed) in enumerate(values):
            opened_at = START + timedelta(minutes=minute)
            rows.append(
                {
                    "open_time": opened_at,
                    "close_time": opened_at + timedelta(minutes=1),
                    "symbol": symbol,
                    "interval": "1m",
                    "open": opened,
                    "close": closed,
                    "is_complete": True,
                    "dataset_version": BARS_VERSION,
                }
            )
    return pl.DataFrame(rows).lazy()


def _funding(rows: list[tuple[datetime, str, float]]):
    return pl.DataFrame(
        [
            {
                "funding_time": timestamp,
                "symbol": symbol,
                "funding_rate": rate,
                "mark_price": 100.0,
                "dataset_version": FUNDING_VERSION,
            }
            for timestamp, symbol, rate in rows
        ]
    ).lazy()


def _config(
    *,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    funding: bool = False,
    funding_policy: str = "error",
    max_turnover: float | None = None,
) -> BacktestConfig:
    return BacktestConfig.model_validate(
        {
            "schedule": {"signal_delay_bars": 1},
            "portfolio": {
                "long_quantile": 0.5,
                "short_quantile": 0.5,
                "max_turnover": max_turnover,
            },
            "execution": {
                "fee": (
                    {"model": "zero"}
                    if fee_bps is None
                    else {"model": "fixed_bps", "taker_bps": fee_bps}
                ),
                "slippage": (
                    {"model": "zero"}
                    if slippage_bps is None
                    else {"model": "fixed_bps", "bps": slippage_bps}
                ),
                "funding": {
                    "enabled": funding,
                    "missing_policy": funding_policy,
                },
            },
            "valuation": {"price": "trade_close"},
            "risk": {"leverage": 2.0},
        }
    )


def _run(targets, bars, config, funding=None):
    return run_vectorized_backtest(
        targets,
        bars,
        None,
        funding,
        config=config,
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=BARS_VERSION,
        mark_dataset_version=None,
        funding_dataset_version=(FUNDING_VERSION if funding is not None else None),
    )


def test_count_and_quantile_select_deterministic_tails() -> None:
    count = construct_portfolio(
        _scores(),
        _portfolio_config(),
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    quantile = construct_portfolio(
        _scores(),
        PortfolioConfig(long_quantile=0.25, short_quantile=0.25),
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    assert count["symbol"].to_list() == ["A", "D"]
    assert quantile["symbol"].to_list() == ["A", "D"]
    assert count["side"].to_list() == ["SHORT", "LONG"]


@pytest.mark.parametrize(
    ("weighting", "volatility", "expected"),
    [
        ("equal", None, [-0.25, -0.25, 0.25, 0.25]),
        ("score", None, [-1 / 3, -1 / 6, 1 / 6, 1 / 3]),
        (
            "inverse_volatility",
            (1.0, 2.0, 2.0, 1.0),
            [-1 / 3, -1 / 6, 1 / 6, 1 / 3],
        ),
    ],
)
def test_weighting_preserves_exposure(
    weighting: str,
    volatility: tuple[float, ...] | None,
    expected: list[float],
) -> None:
    result = construct_portfolio(
        _scores(volatility),
        _portfolio_config(long_count=2, short_count=2, weighting=weighting),
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    weights = result["target_weight"].to_list()
    assert weights == pytest.approx(expected)
    assert sum(abs(value) for value in weights) == pytest.approx(1.0)
    assert sum(weights) == pytest.approx(0.0)


def test_symbol_cap_is_explicit_and_not_renormalized() -> None:
    result = construct_portfolio(
        _scores(),
        _portfolio_config(max_symbol_weight=0.2),
        factor_version=FACTOR_VERSION,
        universe_version=UNIVERSE_VERSION,
    ).frame.collect()
    assert result["target_weight"].to_list() == pytest.approx([-0.2, 0.2])
    assert result["constraint_flags"].to_list() == [
        "MAX_SYMBOL_WEIGHT",
        "MAX_SYMBOL_WEIGHT",
    ]


def test_count_configuration_requires_both_counts() -> None:
    with pytest.raises(ValidationError, match="requires long_count and short_count"):
        PortfolioConfig.model_validate({"construction": "long_short_count"})


def test_next_open_delay_matches_a07_contract() -> None:
    assert fill_time(START, signal_delay_bars=1, base_interval="1m") == (
        START + timedelta(minutes=1)
    )
    assert fill_time(START, signal_delay_bars=3, base_interval="1m") == (
        START + timedelta(minutes=3)
    )


def test_missing_fixed_bps_never_becomes_zero() -> None:
    with pytest.raises(CostModelError, match="taker_bps"):
        fee_rate(FeeConfig(model="fixed_bps"))
    with pytest.raises(CostModelError, match="requires bps"):
        slippage_rate(SlippageConfig(model="fixed_bps"))


def test_long_short_pnl_and_return_identity() -> None:
    bars = _bars(
        {
            "A": ((100.0, 100.0), (100.0, 110.0)),
            "B": ((100.0, 100.0), (100.0, 90.0)),
        }
    )
    result = _run(_targets({"A": 0.5, "B": -0.5}), bars, _config())
    row = result.returns.collect().tail(1).to_dicts()[0]
    assert row["gross_price_return"] == pytest.approx(0.1)
    assert row["net_return"] == pytest.approx(0.1)
    assert row["equity"] == pytest.approx(1.1)
    assert result.trades.collect().height == 2


def test_mark_valuation_starts_new_quantity_at_trade_open() -> None:
    trade_bars = _bars({"A": ((100.0, 100.0), (100.0, 100.0))})
    mark_version = "mark-a08-fixture"
    mark_bars = _bars({"A": ((101.0, 101.0), (101.0, 102.0))}).with_columns(
        pl.lit(mark_version).alias("dataset_version")
    )
    config = BacktestConfig.model_validate(
        {
            **_config().model_dump(mode="python"),
            "valuation": {"price": "mark_close"},
        }
    )
    result = run_vectorized_backtest(
        _targets({"A": 1.0}),
        trade_bars,
        mark_bars,
        None,
        config=config,
        base_interval="1m",
        portfolio_version=PORTFOLIO_VERSION,
        bars_dataset_version=BARS_VERSION,
        mark_dataset_version=mark_version,
        funding_dataset_version=None,
    )
    row = result.returns.collect().tail(1).to_dicts()[0]
    assert row["gross_price_return"] == pytest.approx(0.02)
    assert row["equity"] == pytest.approx(1.02)


def test_cost_contributions_are_separate() -> None:
    bars = _bars({"A": ((100.0, 100.0), (100.0, 100.0))})
    result = _run(
        _targets({"A": 1.0}),
        bars,
        _config(fee_bps=10.0, slippage_bps=20.0),
    )
    row = result.returns.collect().tail(1).to_dicts()[0]
    assert row["gross_price_return"] == pytest.approx(0.0)
    assert row["fee_cost"] == pytest.approx(0.001)
    assert row["slippage_cost"] == pytest.approx(0.002)
    assert row["net_return"] == pytest.approx(-0.003)
    assert row["equity"] == pytest.approx(0.997)


def test_positive_funding_is_paid_by_long() -> None:
    bars = _bars({"A": ((100.0, 100.0), (100.0, 100.0))})
    event = START + timedelta(minutes=2)
    result = _run(
        _targets({"A": 0.5}),
        bars,
        _config(funding=True),
        _funding([(event, "A", 0.01)]),
    )
    row = result.returns.collect().tail(1).to_dicts()[0]
    assert row["funding_return"] == pytest.approx(-0.005)
    assert row["net_return"] == pytest.approx(-0.005)


def test_missing_funding_fails_under_error_policy() -> None:
    bars = _bars(
        {
            "A": ((100.0, 100.0), (100.0, 100.0)),
            "B": ((100.0, 100.0), (100.0, 100.0)),
        }
    )
    event = START + timedelta(minutes=2)
    with pytest.raises(BacktestError, match="missing funding record for B"):
        _run(
            _targets({"A": 0.5, "B": -0.5}),
            bars,
            _config(funding=True),
            _funding([(event, "A", 0.01)]),
        )


def test_funding_exclude_and_assume_zero_are_distinct() -> None:
    bars = _bars(
        {
            "A": ((100.0, 100.0), (100.0, 100.0)),
            "B": ((100.0, 100.0), (100.0, 100.0)),
        }
    )
    event = START + timedelta(minutes=2)
    funding = _funding([(event, "A", 0.01)])
    excluded = _run(
        _targets({"A": 0.5, "B": -0.5}),
        bars,
        _config(funding=True, funding_policy="exclude_symbol"),
        funding,
    )
    excluded_b = excluded.targets.collect().filter(pl.col("symbol") == "B")
    assert excluded_b["target_weight"].item() == 0.0
    assert "FUNDING_MISSING" in excluded_b["constraint_flags"].item()
    assert excluded.trades.collect()["symbol"].to_list() == ["A"]
    assert "funding_exclude_symbol:B" in excluded.warnings
    assumed = _run(
        _targets({"A": 0.5, "B": -0.5}),
        bars,
        _config(funding=True, funding_policy="assume_zero"),
        funding,
    )
    assert assumed.trades.collect()["symbol"].to_list() == ["A", "B"]
    assert any("funding_assume_zero" in item for item in assumed.warnings)


def test_turnover_limit_scales_target_deltas() -> None:
    bars = _bars(
        {
            "A": ((100.0, 100.0), (100.0, 100.0)),
            "B": ((100.0, 100.0), (100.0, 100.0)),
        }
    )
    trades = _run(
        _targets({"A": 0.5, "B": -0.5}),
        bars,
        _config(max_turnover=0.2),
    ).trades.collect()
    assert trades["filled_weight"].to_list() == pytest.approx([0.1, -0.1])
    assert sum(trades["turnover"].to_list()) == pytest.approx(0.2)
    assert all("MAX_TURNOVER" in item for item in trades["constraint_flags"])


def test_result_and_ledgers_are_deterministic() -> None:
    bars = _bars({"A": ((100.0, 100.0), (100.0, 101.0))})
    first = _run(_targets({"A": 0.5}), bars, _config())
    second = _run(_targets({"A": 0.5}), bars, _config())
    assert first.run_id == second.run_id
    assert first.result_hash == second.result_hash
    for name in ("targets", "trades", "positions", "costs", "returns"):
        assert getattr(first, name).collect().equals(getattr(second, name).collect())


def test_versions_are_explicit_and_cli_is_bounded() -> None:
    bars = _bars({"A": ((100.0, 100.0), (100.0, 100.0))})
    with pytest.raises(BacktestError, match="portfolio_version must be explicit"):
        run_vectorized_backtest(
            _targets({"A": 0.5}, version="latest"),
            bars,
            None,
            None,
            config=_config(),
            base_interval="1m",
            portfolio_version="latest",
            bars_dataset_version=BARS_VERSION,
            mark_dataset_version=None,
            funding_dataset_version=None,
        )
    runner = CliRunner()
    assert runner.invoke(app, ["backtest", "preview", "--help"]).exit_code == 0
    preview = get_command(app).commands["backtest"].commands["preview"]
    options = {
        option
        for parameter in preview.params
        for option in getattr(parameter, "opts", ())
    }
    assert {
        "--history-start",
        "--future-end",
        "--backtest-config",
        "--limit",
    } <= options
