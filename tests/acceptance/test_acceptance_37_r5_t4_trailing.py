from __future__ import annotations

import sys
from pathlib import Path

import pytest


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
sys.path.insert(0, str(EXAMPLES))

from run_r5_t4_trailing_event import FACTOR_PROFILES, MONTHS, resolved  # noqa: E402


@pytest.mark.parametrize("month", ["2026-05", "2026-06", "2026-07"])
@pytest.mark.parametrize("variant", ["FIXED", "ROLLING"])
def test_r5_t4_run_contracts_are_frozen(variant: str, month: str) -> None:
    bundle = resolved(variant, month)
    config = bundle.backtest
    compact_month = month.replace("-", "")
    assert config.run.name == f"R5-T4-{variant}-{compact_month}-r01"
    assert config.run.dataset_version == MONTHS[month]["dataset_version"]
    assert config.run.start.isoformat() == MONTHS[month]["start"]
    assert config.run.end.isoformat() == MONTHS[month]["end"]
    assert bundle.data.time.start.isoformat() == MONTHS[month]["history_start"]
    assert bundle.data.time.end.isoformat() == MONTHS[month]["data_end"]
    assert config.engine.backend == "event"
    assert config.engine.purpose == "formal"
    assert config.risk.leverage == 5.0
    assert config.risk.symbol_exits.stop_loss.distance == pytest.approx(0.028)
    assert not config.risk.symbol_exits.take_profit.enabled
    trailing = config.risk.symbol_exits.trailing_stop
    assert trailing.enabled
    assert trailing.distance == pytest.approx(0.028)
    assert trailing.activation_distance == pytest.approx(0.058)
    assert config.portfolio.holding.mode == "single_position_replace"
    assert config.portfolio.holding.existing_signal == "ignore"
    if variant == "FIXED":
        assert config.capital.initial_equity == 10_000.0
        assert config.portfolio.sizing.mode == "fixed_margin"
        assert config.portfolio.sizing.margin_amount == pytest.approx(10_000.0 / 3.0)
    else:
        sizing = config.portfolio.sizing
        assert config.capital.initial_equity == 2_000.0
        assert sizing.mode == "rolling_margin"
        assert sizing.rolling_initial_margin == 200.0
        assert sizing.rolling_reset_margin == 200.0
        assert sizing.rolling_min_margin == 80.0
        assert sizing.rolling_max_margin == 1_000.0


@pytest.mark.parametrize("month", ["2026-05", "2026-06", "2026-07"])
def test_r5_t4_h1_rolling_identity_changes_only_factor_sampling(
    month: str,
) -> None:
    baseline = resolved("ROLLING", month)
    hourly = resolved("ROLLING", month, "H1")
    compact_month = month.replace("-", "")
    assert hourly.backtest.run.name == (
        f"R5-T4-H1-ROLLING-{compact_month}-r01"
    )
    assert FACTOR_PROFILES["H1"] == "1h"
    assert hourly.factor.factors[0].parameters == {
        "sample_count": 12,
        "sample_interval": "1h",
    }
    baseline_payload = baseline.model_dump(mode="json")
    hourly_payload = hourly.model_dump(mode="json")
    baseline_payload["factor"]["factors"][0]["parameters"][
        "sample_interval"
    ] = "1h"
    baseline_payload["backtest"]["run"]["name"] = hourly_payload[
        "backtest"
    ]["run"]["name"]
    assert baseline_payload == hourly_payload
