"""Portfolio, execution, risk, and output configuration models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import Field, ValidationError, field_validator, model_validator

from bianbt.config.common import StrictModel, as_utc
from bianbt.config.durations import duration_seconds


class RunConfig(StrictModel):
    name: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    dataset_version: str | None = None
    random_seed: int = 42

    @field_validator("start", "end")
    @classmethod
    def validate_utc(cls, value: datetime | None) -> datetime | None:
        return as_utc(value)

    @model_validator(mode="after")
    def validate_range(self) -> "RunConfig":
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self


class BacktestScheduleConfig(StrictModel):
    factor_interval: str = "1h"
    rebalance_interval: str = "4h"
    signal_delay_bars: int = Field(default=1, ge=0)

    @field_validator("factor_interval", "rebalance_interval")
    @classmethod
    def validate_intervals(cls, value: str) -> str:
        duration_seconds(value)
        return value


class PortfolioConfig(StrictModel):
    construction: Literal[
        "long_short_quantile", "long_short_count"
    ] = "long_short_quantile"
    long_quantile: float = Field(default=0.2, gt=0, le=0.5)
    short_quantile: float = Field(default=0.2, gt=0, le=0.5)
    long_count: int | None = Field(default=None, ge=1)
    short_count: int | None = Field(default=None, ge=1)
    weighting: Literal["equal", "score", "inverse_volatility"] = "equal"
    gross_exposure: float = Field(default=1.0, gt=0)
    net_exposure: float = 0.0
    max_symbol_weight: float | None = Field(default=None, gt=0, le=1)
    max_turnover: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_exposures(self) -> "PortfolioConfig":
        if self.long_quantile + self.short_quantile > 1:
            raise ValueError("long_quantile + short_quantile must be <= 1")
        if abs(self.net_exposure) > self.gross_exposure:
            raise ValueError("absolute net_exposure must be <= gross_exposure")
        counts = (self.long_count, self.short_count)
        if self.construction == "long_short_count" and any(
            value is None for value in counts
        ):
            raise ValueError("long_short_count requires long_count and short_count")
        if self.construction == "long_short_quantile" and any(
            value is not None for value in counts
        ):
            raise ValueError(
                "long_count and short_count require long_short_count"
            )
        return self


class FeeConfig(StrictModel):
    model: Literal["fixed_bps", "zero"] = "fixed_bps"
    taker_bps: float | None = Field(default=None, ge=0)
    maker_bps: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_model_fields(self) -> "FeeConfig":
        if self.model == "zero" and (
            self.taker_bps is not None or self.maker_bps is not None
        ):
            raise ValueError("zero fee model must not specify bps values")
        return self


class SlippageConfig(StrictModel):
    model: Literal["fixed_bps", "zero"] = "fixed_bps"
    bps: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_model_fields(self) -> "SlippageConfig":
        if self.model == "zero" and self.bps is not None:
            raise ValueError("zero slippage model must not specify bps")
        return self


class FundingConfig(StrictModel):
    enabled: bool = True
    missing_policy: Literal["error", "exclude_symbol", "assume_zero"] = "error"


class ExecutionConfig(StrictModel):
    fill_price: Literal["next_bar_open"] = "next_bar_open"
    partial_fill: bool = False
    fee: FeeConfig = Field(default_factory=FeeConfig)
    slippage: SlippageConfig = Field(default_factory=SlippageConfig)
    funding: FundingConfig = Field(default_factory=FundingConfig)


class ValuationConfig(StrictModel):
    price: Literal["mark_close", "trade_close"] = "mark_close"


class RiskConfig(StrictModel):
    leverage: float = Field(default=1.0, gt=0)
    enforce_liquidation: bool = False


class BacktestOutputConfig(StrictModel):
    root: Path = Path("data/backtest/runs")
    save_factor_values: bool = True
    save_universe: bool = True
    save_positions: bool = True
    save_trades: bool = True
    save_costs: bool = True
    render_html: bool = True


class BacktestPerformanceConfig(StrictModel):
    mode: Literal["in_memory", "chunked"] = "in_memory"
    chunk_interval: str = "2d"
    max_input_rows_per_chunk: int = Field(default=5_000_000, ge=1_000)
    max_incremental_rss_mib: int = Field(default=2_048, ge=64)
    collect_diagnostics: bool = True

    @field_validator("chunk_interval")
    @classmethod
    def validate_chunk_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value


class RankSideConfig(StrictModel):
    """One side of an exact-rank selection rule."""

    ranks: tuple[int, ...] = ()
    ranges: tuple[tuple[int, int], ...] = ()

    @field_validator("ranks")
    @classmethod
    def validate_ranks(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(rank < 1 for rank in value):
            raise ValueError("ranks must contain positive integers")
        if len(value) != len(set(value)):
            raise ValueError("ranks must be unique")
        return value

    @field_validator("ranges")
    @classmethod
    def validate_ranges(
        cls, value: tuple[tuple[int, int], ...]
    ) -> tuple[tuple[int, int], ...]:
        if any(start < 1 or end < start for start, end in value):
            raise ValueError("ranges must be positive inclusive [start, end] pairs")
        ordered = sorted(value)
        for previous, current in zip(ordered, ordered[1:]):
            if current[0] <= previous[1]:
                raise ValueError("ranges must not overlap")
        return value

    @model_validator(mode="after")
    def validate_no_duplicate_coverage(self) -> "RankSideConfig":
        for rank in self.ranks:
            if any(start <= rank <= end for start, end in self.ranges):
                raise ValueError("ranks must not duplicate coverage from ranges")
        return self

    def contains(self, rank: int) -> bool:
        return rank in self.ranks or any(
            start <= rank <= end for start, end in self.ranges
        )

    @property
    def is_empty(self) -> bool:
        return not self.ranks and not self.ranges


class RankDescentConfig(StrictModel):
    start_rank_at_least: int = Field(ge=2)
    entry_rank: int = Field(default=1, ge=1)
    equal_policy: Literal["keep", "reset"] = "keep"
    increase_policy: Literal["reset"] = "reset"

    @model_validator(mode="after")
    def validate_path(self) -> "RankDescentConfig":
        if self.entry_rank >= self.start_rank_at_least:
            raise ValueError("entry_rank must be less than start_rank_at_least")
        return self


class FactorCrossoverConfig(StrictModel):
    """Point-in-time factor threshold crossings for independent positions."""

    entry_threshold: float = 0.0
    exit_threshold: float = 0.0
    entry_when: Literal["cross_above"] = "cross_above"
    exit_when: Literal["cross_below"] = "cross_below"
    gap_policy: Literal["reset"] = "reset"
    initial_policy: Literal["wait_for_cross"] = "wait_for_cross"


class RankSelectionConfig(StrictModel):
    mode: Literal["rank_set", "rank_descent", "factor_crossover"] = "rank_set"
    rank_order: Literal["descending"] = "descending"
    clock: Literal["factor", "rebalance"] = "rebalance"
    lag: int = Field(default=0, ge=0)
    long: RankSideConfig = Field(default_factory=RankSideConfig)
    short: RankSideConfig = Field(default_factory=RankSideConfig)
    descent: RankDescentConfig | None = None
    crossover: FactorCrossoverConfig | None = None
    audit_top_n: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_sides(self) -> "RankSelectionConfig":
        if self.mode == "rank_descent":
            if self.descent is None:
                raise ValueError("rank_descent requires descent configuration")
            if self.lag != 0:
                raise ValueError("rank_descent requires lag=0")
            if not self.long.is_empty or not self.short.is_empty:
                raise ValueError("rank_descent must not configure rank_set sides")
            if self.crossover is not None:
                raise ValueError("rank_descent must not configure crossover")
            return self
        if self.mode == "factor_crossover":
            if self.crossover is None:
                raise ValueError("factor_crossover requires crossover configuration")
            if self.clock != "factor":
                raise ValueError("factor_crossover requires clock=factor")
            if self.lag != 0:
                raise ValueError("factor_crossover requires lag=0")
            if self.descent is not None:
                raise ValueError("factor_crossover must not configure descent")
            if not self.long.is_empty or not self.short.is_empty:
                raise ValueError("factor_crossover must not configure rank_set sides")
            return self
        if self.descent is not None:
            raise ValueError("descent configuration requires mode=rank_descent")
        if self.crossover is not None:
            raise ValueError("crossover configuration requires mode=factor_crossover")
        if self.long.is_empty and self.short.is_empty:
            raise ValueError("at least one of long or short must select a rank")
        for rank in (*self.long.ranks, *self.short.ranks):
            if self.long.contains(rank) and self.short.contains(rank):
                raise ValueError("long and short rank selections must not overlap")
        for long_start, long_end in self.long.ranges:
            for short_start, short_end in self.short.ranges:
                if max(long_start, short_start) <= min(long_end, short_end):
                    raise ValueError("long and short rank selections must not overlap")
        return self


class PositionSizingConfig(StrictModel):
    mode: Literal[
        "target_weight",
        "fixed_margin",
        "fixed_notional",
        "equity_fraction",
        "equity_margin_fraction",
        "position_fraction",
        "rolling_margin",
    ]
    weighting: Literal["equal", "score", "inverse_volatility"] | None = None
    target_gross_exposure: float | None = Field(default=None, gt=0)
    target_net_exposure: float | None = None
    margin_amount: float | None = Field(default=None, gt=0)
    notional_amount: float | None = Field(default=None, gt=0)
    fraction: float | None = Field(default=None, gt=0, le=1)
    reverse_policy: Literal[
        "flatten_only", "flatten_then_open", "net_delta"
    ] | None = None
    zero_position_policy: Literal[
        "skip", "error", "bootstrap_fixed_notional"
    ] | None = None
    bootstrap_notional_amount: float | None = Field(default=None, gt=0)
    rolling_initial_margin: float | None = Field(default=None, gt=0)
    rolling_reset_margin: float | None = Field(default=None, gt=0)
    rolling_min_margin: float | None = Field(default=None, gt=0)
    rolling_max_margin: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_mode_fields(self) -> "PositionSizingConfig":
        amounts = {
            "margin_amount": self.margin_amount,
            "notional_amount": self.notional_amount,
            "fraction": self.fraction,
        }
        required = {
            "fixed_margin": "margin_amount",
            "fixed_notional": "notional_amount",
            "equity_fraction": "fraction",
            "equity_margin_fraction": "fraction",
            "position_fraction": "fraction",
        }
        rolling_fields = (
            self.rolling_initial_margin,
            self.rolling_reset_margin,
            self.rolling_min_margin,
            self.rolling_max_margin,
        )
        if self.mode == "target_weight":
            if self.weighting is None or self.target_gross_exposure is None:
                raise ValueError(
                    "target_weight requires weighting and target_gross_exposure"
                )
            if self.target_net_exposure is None:
                raise ValueError("target_weight requires target_net_exposure")
            if abs(self.target_net_exposure) > self.target_gross_exposure:
                raise ValueError(
                    "absolute target_net_exposure must be <= target_gross_exposure"
                )
            if any(value is not None for value in amounts.values()):
                raise ValueError("target_weight must not specify incremental amounts")
            if self.reverse_policy is not None:
                raise ValueError("target_weight must not specify reverse_policy")
        elif self.mode == "rolling_margin":
            if any(value is not None for value in amounts.values()):
                raise ValueError("rolling_margin must not specify fixed sizing amounts")
            if any(value is None for value in rolling_fields):
                raise ValueError("rolling_margin requires initial, reset, min, and max margins")
            assert self.rolling_min_margin is not None
            assert self.rolling_max_margin is not None
            assert self.rolling_initial_margin is not None
            assert self.rolling_reset_margin is not None
            if self.rolling_min_margin >= self.rolling_max_margin:
                raise ValueError("rolling_min_margin must be less than rolling_max_margin")
            for name, value in (
                ("rolling_initial_margin", self.rolling_initial_margin),
                ("rolling_reset_margin", self.rolling_reset_margin),
            ):
                if not self.rolling_min_margin <= value <= self.rolling_max_margin:
                    raise ValueError(f"{name} must be within the inclusive rolling bounds")
            if self.weighting is not None or self.target_gross_exposure is not None:
                raise ValueError("rolling_margin must not specify target weighting/exposure")
            if self.target_net_exposure is not None:
                raise ValueError("rolling_margin must not specify target_net_exposure")
            if self.reverse_policy is None:
                raise ValueError("rolling_margin requires reverse_policy")
            if (
                self.zero_position_policy is not None
                or self.bootstrap_notional_amount is not None
            ):
                raise ValueError(
                    "rolling_margin must not specify position_fraction bootstrap fields"
                )
        else:
            field = required[self.mode]
            if amounts[field] is None:
                raise ValueError(f"{self.mode} requires {field}")
            if any(
                value is not None
                for name, value in amounts.items()
                if name != field
            ):
                raise ValueError(f"{self.mode} accepts only {field}")
            if self.weighting is not None or self.target_gross_exposure is not None:
                raise ValueError(
                    "incremental sizing must not specify target weighting/exposure"
                )
            if self.target_net_exposure is not None:
                raise ValueError(
                    "incremental sizing must not specify target_net_exposure"
                )
            if self.reverse_policy is None:
                raise ValueError(f"{self.mode} requires reverse_policy")

        if self.mode == "position_fraction":
            if self.zero_position_policy is None:
                raise ValueError(
                    "position_fraction requires zero_position_policy"
                )
            if self.zero_position_policy == "bootstrap_fixed_notional":
                if self.bootstrap_notional_amount is None:
                    raise ValueError(
                        "bootstrap_fixed_notional requires bootstrap_notional_amount"
                    )
            elif self.bootstrap_notional_amount is not None:
                raise ValueError(
                    "bootstrap_notional_amount requires bootstrap_fixed_notional"
                )
        elif self.mode != "rolling_margin" and (
            self.zero_position_policy is not None
            or self.bootstrap_notional_amount is not None
        ):
            raise ValueError(
                "zero-position fields are only valid for position_fraction"
            )
        if self.mode != "rolling_margin" and any(
            value is not None for value in rolling_fields
        ):
            raise ValueError("rolling margin fields require mode=rolling_margin")
        return self


class PortfolioConstraintsV2Config(StrictModel):
    max_gross_exposure: float | None = Field(default=None, gt=0)
    max_net_exposure: float | None = Field(default=None, ge=0)
    max_symbol_weight: float | None = Field(default=None, gt=0)
    max_symbol_notional: float | None = Field(default=None, gt=0)
    max_consecutive_adds: int | None = Field(default=None, ge=1)
    max_turnover: float | None = Field(default=None, ge=0)


class HoldingPolicyConfig(StrictModel):
    mode: Literal["independent", "single_position_replace"] = "independent"
    existing_signal: Literal["add", "ignore"] = "add"


class PortfolioV2Config(StrictModel):
    selection: RankSelectionConfig
    sizing: PositionSizingConfig
    constraints: PortfolioConstraintsV2Config = Field(
        default_factory=PortfolioConstraintsV2Config
    )
    holding: HoldingPolicyConfig = Field(default_factory=HoldingPolicyConfig)

    @model_validator(mode="after")
    def validate_stateful_sizing(self) -> "PortfolioV2Config":
        if self.sizing.mode == "rolling_margin" and (
            self.holding.mode != "single_position_replace"
            or self.holding.existing_signal != "ignore"
        ):
            raise ValueError(
                "rolling_margin requires single_position_replace with existing_signal=ignore"
            )
        return self


class CapitalConfig(StrictModel):
    currency: Literal["USDT"] = "USDT"
    initial_equity: float = Field(gt=0)
    margin_model: Literal["simple_cross"] = "simple_cross"
    reserved_cost_buffer: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def validate_buffer(self) -> "CapitalConfig":
        if self.reserved_cost_buffer >= self.initial_equity:
            raise ValueError("reserved_cost_buffer must be less than initial_equity")
        return self


class SymbolExitRuleConfig(StrictModel):
    enabled: bool = False
    distance: float | None = Field(default=None, gt=0, le=1)
    long_distance: float | None = Field(default=None, gt=0, le=1)
    short_distance: float | None = Field(default=None, gt=0, le=1)
    action: Literal["close", "reduce_fraction"] = "close"
    reduce_fraction: float | None = Field(default=None, gt=0, le=1)
    activation_distance: float | None = Field(default=None, gt=0, le=1)

    @model_validator(mode="after")
    def validate_rule(self) -> "SymbolExitRuleConfig":
        asymmetric = (
            self.long_distance is not None or self.short_distance is not None
        )
        if self.distance is not None and asymmetric:
            raise ValueError(
                "use either distance or long_distance/short_distance"
            )
        if self.enabled and self.distance is None:
            if self.long_distance is None or self.short_distance is None:
                raise ValueError(
                    "enabled exit rules require distance or both "
                    "long_distance and short_distance"
                )
        if self.action == "reduce_fraction":
            if self.reduce_fraction is None:
                raise ValueError("reduce_fraction action requires reduce_fraction")
        elif self.reduce_fraction is not None:
            raise ValueError("reduce_fraction value requires reduce_fraction action")
        return self


class SymbolExitsConfig(StrictModel):
    stop_loss: SymbolExitRuleConfig = Field(default_factory=SymbolExitRuleConfig)
    take_profit: SymbolExitRuleConfig = Field(default_factory=SymbolExitRuleConfig)
    trailing_stop: SymbolExitRuleConfig = Field(default_factory=SymbolExitRuleConfig)

    @model_validator(mode="after")
    def validate_activation(self) -> "SymbolExitsConfig":
        if (
            self.stop_loss.activation_distance is not None
            or self.take_profit.activation_distance is not None
        ):
            raise ValueError("activation_distance is only valid for trailing_stop")
        if self.trailing_stop.activation_distance is not None and not self.trailing_stop.enabled:
            raise ValueError("trailing activation requires an enabled trailing_stop")
        return self


class PortfolioExitsConfig(StrictModel):
    stop_loss: float | None = Field(default=None, gt=0, le=1)
    take_profit: float | None = Field(default=None, gt=0)
    max_drawdown: float | None = Field(default=None, gt=0, le=1)


class RiskV2Config(StrictModel):
    leverage: float = Field(gt=0)
    enforce_liquidation: Literal[False] = False
    evaluation_interval: str
    trigger_price: Literal["trade", "mark"]
    fill_model: Literal["next_bar_open", "same_bar_trigger"] = (
        "next_bar_open"
    )
    gap_policy: Literal["worse_executable"] = "worse_executable"
    intrabar_conflict: Literal["worst_case", "error"]
    symbol_exits: SymbolExitsConfig = Field(default_factory=SymbolExitsConfig)
    portfolio_exits: PortfolioExitsConfig = Field(default_factory=PortfolioExitsConfig)
    cooldown_bars: int = Field(default=0, ge=0)
    reentry_policy: Literal[
        "next_scheduled_rebalance", "after_cooldown"
    ]
    max_triggers_per_symbol: int | None = Field(default=None, ge=1)

    @field_validator("evaluation_interval")
    @classmethod
    def validate_evaluation_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value


class BacktestPerformanceV2Config(BacktestPerformanceConfig):
    max_process_rss_mib: int | None = Field(default=None, ge=256)
    resume_policy: Literal["resume", "error_if_exists"] = "resume"
    max_rank_lag: int = Field(default=24, ge=0)
    max_rank_state_rows: int = Field(default=20_000, ge=1)
    max_position_state_rows: int = Field(default=20_000, ge=1)
    max_pending_instructions: int = Field(default=20_000, ge=1)
    max_risk_state_rows: int = Field(default=20_000, ge=1)
    max_pending_risk_intents: int = Field(default=20_000, ge=1)
    reuse_mode: Literal["off", "read_write", "refresh"] = "off"
    reuse_root: Path | None = None
    sparse_execution: bool = False

    @model_validator(mode="after")
    def validate_reuse(self) -> "BacktestPerformanceV2Config":
        if self.reuse_mode != "off" and self.reuse_root is None:
            raise ValueError("reuse_root is required when reuse_mode is enabled")
        return self


class ExecutionEngineConfig(StrictModel):
    """Select an execution algorithm independently from the V1/V2 contract."""

    backend: Literal["auto", "fast_matrix", "event"] = "auto"
    purpose: Literal["research", "formal"] = "research"
    equivalence_audit: bool = False
    source_matrix_run_id: str | None = Field(
        default=None, pattern=r"^fm-[0-9a-f]{24}$"
    )

    @model_validator(mode="after")
    def validate_promotion_source(self) -> "ExecutionEngineConfig":
        if self.source_matrix_run_id is not None and (
            self.backend != "event" or self.purpose != "formal"
        ):
            raise ValueError(
                "source_matrix_run_id requires backend=event and purpose=formal"
            )
        return self


def _validate_section(model: Any, value: Any, field: str) -> Any:
    """Validate a dispatched section while retaining its public error path."""

    try:
        return model.model_validate(value)
    except ValidationError as exc:
        errors = []
        for original in exc.errors(include_url=False):
            error = dict(original)
            error["loc"] = (field, *original["loc"])
            errors.append(error)
        raise ValidationError.from_exception_data(
            "BacktestConfig", errors
        ) from exc


def _dispatch_nested_config(value: Any) -> Any:
    """Normalize missing-version V1 documents and strictly route V2 sections."""

    if not isinstance(value, Mapping):
        return value
    payload = dict(value)
    version = payload.get("config_version", "v1")
    payload["config_version"] = version
    if version == "v1":
        payload["portfolio"] = _validate_section(
            PortfolioConfig, payload.get("portfolio", {}), "portfolio"
        )
        payload["risk"] = _validate_section(
            RiskConfig, payload.get("risk", {}), "risk"
        )
        payload["performance"] = _validate_section(
            BacktestPerformanceConfig,
            payload.get("performance", {}),
            "performance",
        )
    elif version == "v2":
        payload["portfolio"] = _validate_section(
            PortfolioV2Config, payload.get("portfolio", {}), "portfolio"
        )
        payload["risk"] = _validate_section(
            RiskV2Config, payload.get("risk", {}), "risk"
        )
        payload["performance"] = _validate_section(
            BacktestPerformanceV2Config,
            payload.get("performance", {}),
            "performance",
        )
        payload["capital"] = _validate_section(
            CapitalConfig, payload.get("capital", {}), "capital"
        )
    return payload


class BacktestConfig(StrictModel):
    config_version: Literal["v1", "v2"] = "v1"
    run: RunConfig = Field(default_factory=RunConfig)
    schedule: BacktestScheduleConfig = Field(default_factory=BacktestScheduleConfig)
    portfolio: PortfolioConfig | PortfolioV2Config = Field(
        default_factory=PortfolioConfig
    )
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    valuation: ValuationConfig = Field(default_factory=ValuationConfig)
    risk: RiskConfig | RiskV2Config = Field(default_factory=RiskConfig)
    capital: CapitalConfig | None = None
    output: BacktestOutputConfig = Field(default_factory=BacktestOutputConfig)
    performance: BacktestPerformanceConfig | BacktestPerformanceV2Config = Field(
        default_factory=BacktestPerformanceConfig
    )
    # ``None`` is deliberately omitted from serialization.  Old documents therefore
    # retain their byte-for-byte canonical identity and their historical dispatch.
    engine: ExecutionEngineConfig | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="before")
    @classmethod
    def dispatch_version(cls, value: Any) -> Any:
        return _dispatch_nested_config(value)

    @model_validator(mode="after")
    def validate_version_sections(self) -> "BacktestConfig":
        if self.config_version == "v1":
            if self.engine is not None:
                raise ValueError("engine is only valid for config_version v2")
            if not isinstance(self.portfolio, PortfolioConfig):
                raise ValueError("v1 requires the legacy portfolio contract")
            if not isinstance(self.risk, RiskConfig):
                raise ValueError("v1 requires the legacy risk contract")
            if type(self.performance) is not BacktestPerformanceConfig:
                raise ValueError("v1 requires the legacy performance contract")
            if self.capital is not None:
                raise ValueError("capital is only valid for config_version v2")
        else:
            if not isinstance(self.portfolio, PortfolioV2Config):
                raise ValueError("v2 requires portfolio selection/sizing contracts")
            if not isinstance(self.risk, RiskV2Config):
                raise ValueError("v2 requires the V2 risk contract")
            if not isinstance(self.performance, BacktestPerformanceV2Config):
                raise ValueError("v2 requires the V2 performance contract")
            if self.capital is None:
                raise ValueError("v2 requires capital")
            if self.portfolio.selection.lag > self.performance.max_rank_lag:
                raise ValueError(
                    "portfolio.selection.lag must be <= performance.max_rank_lag"
                )
            if (
                self.performance.mode == "chunked"
                and self.performance.max_process_rss_mib is None
            ):
                raise ValueError(
                    "v2 chunked performance requires max_process_rss_mib"
                )
        return self

    def assert_execution_supported(self) -> None:
        """Confirm the dispatched V1/V2 execution mode is implemented."""
