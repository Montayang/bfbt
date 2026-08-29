"""Cross-file configuration contracts and run-readiness checks."""

from __future__ import annotations

from pydantic import model_validator

from bianbt.config.backtest import BacktestConfig
from bianbt.config.common import StrictModel
from bianbt.config.data import DataConfig
from bianbt.config.durations import duration_seconds, is_integer_multiple
from bianbt.config.factor import FactorConfig
from bianbt.config.universe import UniverseConfig


class RunReadinessError(ValueError):
    """A valid draft is missing one or more values required to start a run."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("\n".join(f"- {issue}" for issue in issues))


class ResolvedConfig(StrictModel):
    data: DataConfig
    universe: UniverseConfig
    factor: FactorConfig
    backtest: BacktestConfig

    @model_validator(mode="after")
    def validate_cross_file_contracts(self) -> "ResolvedConfig":
        if self.data.market != self.universe.market:
            raise ValueError("data.market and universe.market must match")

        base = self.data.time.base_interval
        dataset_intervals = tuple(
            dataset.base_interval
            for dataset in (
                self.data.datasets.bars,
                self.data.datasets.mark_bars,
                self.data.datasets.index_bars,
            )
            if dataset.enabled
        )
        if any(interval != base for interval in dataset_intervals):
            raise ValueError("all dataset base intervals must match data.time.base_interval")

        parameter_intervals = tuple(
            value
            for factor in self.factor.factors
            for key, value in factor.parameters.items()
            if key in {
                "lookback",
                "skip_recent",
                "window",
                "horizon",
                "source_interval",
                "sample_interval",
            }
            and isinstance(value, str)
        )
        risk_interval = getattr(
            self.backtest.risk, "evaluation_interval", None
        )
        intervals = (
            self.universe.schedule.interval,
            self.universe.filters.rolling_quote_volume.window,
            self.universe.filters.max_missing_ratio.window,
            self.backtest.schedule.factor_interval,
            self.backtest.schedule.rebalance_interval,
            self.backtest.performance.chunk_interval,
            *(factor.compute_interval for factor in self.factor.factors),
            *(label.horizon for label in self.factor.labels),
            *parameter_intervals,
            *((risk_interval,) if risk_interval is not None else ()),
        )
        for interval in intervals:
            if not is_integer_multiple(interval, base):
                raise ValueError(
                    f"interval {interval!r} must be an integer multiple "
                    f"of data base interval {base!r}"
                )
        chunk = self.backtest.performance.chunk_interval
        if self.backtest.performance.mode == "chunked":
            for name, interval in (
                ("universe.schedule.interval", self.universe.schedule.interval),
                (
                    "backtest.schedule.factor_interval",
                    self.backtest.schedule.factor_interval,
                ),
                (
                    "backtest.schedule.rebalance_interval",
                    self.backtest.schedule.rebalance_interval,
                ),
            ):
                if duration_seconds(chunk) % duration_seconds(interval):
                    raise ValueError(
                        f"performance.chunk_interval must be a multiple of {name}"
                    )
            if (
                risk_interval is not None
                and duration_seconds(chunk) % duration_seconds(risk_interval)
            ):
                raise ValueError(
                    "performance.chunk_interval must be a multiple of "
                    "backtest.risk.evaluation_interval"
                )
        if (
            self.backtest.config_version == "v2"
            and getattr(self.backtest.risk, "trigger_price", None) == "mark"
            and not self.data.datasets.mark_bars.enabled
        ):
            raise ValueError(
                "data.datasets.mark_bars.enabled is required for "
                "backtest.risk.trigger_price=mark"
            )
        return self

    def assert_run_ready(self) -> None:
        """Apply checks deferred while users are editing draft configs."""

        issues: list[str] = []
        run = self.backtest.run
        if not run.name:
            issues.append("backtest.run.name: required for a runnable configuration")
        if run.start is None:
            issues.append("backtest.run.start: required for a runnable configuration")
        if run.end is None:
            issues.append("backtest.run.end: required for a runnable configuration")
        if not run.dataset_version or run.dataset_version == "latest":
            issues.append(
                "backtest.run.dataset_version: explicit non-'latest' version required"
            )
        if not self.factor.factors:
            issues.append("factor.factors: at least one factor is required")
        if self.data.source.allow_authenticated_endpoints:
            issues.append("data.source.allow_authenticated_endpoints: must be false")
        if not self.data.datasets.bars.enabled:
            issues.append("data.datasets.bars.enabled: required for backtests")
        if (
            self.backtest.valuation.price == "mark_close"
            and not self.data.datasets.mark_bars.enabled
        ):
            issues.append(
                "data.datasets.mark_bars.enabled: required for mark_close valuation"
            )
        if (
            self.backtest.execution.funding.enabled
            and not self.data.datasets.funding.enabled
        ):
            issues.append(
                "data.datasets.funding.enabled: required when funding is enabled"
            )
        if self.backtest.schedule.signal_delay_bars < 1:
            issues.append(
                "backtest.schedule.signal_delay_bars: must be >= 1 for next_bar_open"
            )

        fee = self.backtest.execution.fee
        if fee.model == "fixed_bps" and fee.taker_bps is None:
            issues.append("backtest.execution.fee.taker_bps: required for fixed_bps")
        slippage = self.backtest.execution.slippage
        if slippage.model == "fixed_bps" and slippage.bps is None:
            issues.append(
                "backtest.execution.slippage.bps: required for fixed_bps"
            )
        if run.start is not None and self.data.time.start is not None:
            if run.start < self.data.time.start:
                issues.append("backtest.run.start: precedes configured data.time.start")
        if run.end is not None and self.data.time.end is not None:
            if run.end > self.data.time.end:
                issues.append("backtest.run.end: exceeds configured data.time.end")

        if issues:
            raise RunReadinessError(issues)
