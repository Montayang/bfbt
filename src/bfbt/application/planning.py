"""Shared formal-run history and execution-tail bounds."""

from __future__ import annotations

from datetime import datetime, timedelta

from bfbt.config.bundle import ResolvedConfig
from bfbt.config.durations import duration_seconds
from bfbt.data.manifests import DatasetReference


def history_seconds(config: ResolvedConfig, factor_name: str) -> int:
    base_seconds = duration_seconds(config.data.time.base_interval)
    minimum_history = config.universe.filters.min_history_bars or 0
    seconds = max(
        minimum_history * base_seconds,
        duration_seconds(config.universe.filters.rolling_quote_volume.window),
        duration_seconds(config.universe.filters.max_missing_ratio.window),
    )
    definition = next(
        item for item in config.factor.factors if item.name == factor_name
    )
    parameter_seconds = [
        duration_seconds(value)
        for key, value in definition.parameters.items()
        if key in {"lookback", "window", "skip_recent"}
        and isinstance(value, str)
    ]
    if parameter_seconds:
        seconds = max(seconds, sum(parameter_seconds))
    if definition.name == "intrabar_ema_ratio":
        source = definition.parameters.get("source_interval")
        slow_span = definition.parameters.get("slow_span")
        if isinstance(source, str) and isinstance(slow_span, int):
            seconds = max(seconds, duration_seconds(source) * slow_span)
    if definition.name in {
        "sampled_mean_ratio",
        "sampled_mean_ratio_inverse",
    }:
        interval = definition.parameters.get("sample_interval")
        count = definition.parameters.get("sample_count")
        if isinstance(interval, str) and isinstance(count, int) and count >= 2:
            seconds = max(seconds, duration_seconds(interval) * (count - 1))
    return seconds


def history_start(config: ResolvedConfig, factor_name: str) -> datetime:
    start = config.backtest.run.start
    assert start is not None
    return start - timedelta(seconds=history_seconds(config, factor_name))


def future_end(config: ResolvedConfig) -> datetime:
    end = config.backtest.run.end
    assert end is not None
    base_seconds = duration_seconds(config.data.time.base_interval)
    bars = config.backtest.schedule.signal_delay_bars + 1
    return end + timedelta(seconds=bars * base_seconds)


def contracts_scan_end(
    config: ResolvedConfig, member: DatasetReference
) -> datetime:
    """Return a safe contract scan end for the configured PIT policy."""

    if config.universe.point_in_time.use_contract_snapshots:
        end = config.backtest.run.end
        assert end is not None
        return end
    return member.available_to
