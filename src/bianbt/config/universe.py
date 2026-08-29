"""Point-in-time universe configuration models."""

from __future__ import annotations

from pydantic import Field, field_validator

from bianbt.config.common import StrictModel
from bianbt.config.data import MarketConfig
from bianbt.config.durations import duration_seconds


class UniverseScheduleConfig(StrictModel):
    interval: str = "1h"

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value


class PointInTimeConfig(StrictModel):
    enabled: bool = True
    use_contract_snapshots: bool = True
    use_first_last_valid_bar: bool = True


class RollingMinimumConfig(StrictModel):
    window: str = "24h"
    minimum: float | None = Field(default=None, ge=0)

    @field_validator("window")
    @classmethod
    def validate_window(cls, value: str) -> str:
        duration_seconds(value)
        return value


class RollingMaximumRatioConfig(StrictModel):
    window: str = "24h"
    maximum: float | None = Field(default=0.01, ge=0, le=1)

    @field_validator("window")
    @classmethod
    def validate_window(cls, value: str) -> str:
        duration_seconds(value)
        return value


class UniverseFiltersConfig(StrictModel):
    trading_status_only: bool = True
    min_listing_age_days: int = Field(default=30, ge=0)
    min_history_bars: int | None = Field(default=1_440, ge=1)
    rolling_quote_volume: RollingMinimumConfig = Field(
        default_factory=RollingMinimumConfig
    )
    max_missing_ratio: RollingMaximumRatioConfig = Field(
        default_factory=RollingMaximumRatioConfig
    )
    exclude_symbols: tuple[str, ...] = ()

    @field_validator("exclude_symbols")
    @classmethod
    def normalize_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip().upper() for value in values)
        if any(not value for value in normalized):
            raise ValueError("must not contain empty symbols")
        if len(normalized) != len(set(normalized)):
            raise ValueError("must not contain duplicate symbols")
        return normalized


class UniverseOutputConfig(StrictModel):
    save_reason_codes: bool = True


class UniverseConfig(StrictModel):
    schedule: UniverseScheduleConfig = Field(default_factory=UniverseScheduleConfig)
    market: MarketConfig = Field(default_factory=MarketConfig)
    point_in_time: PointInTimeConfig = Field(default_factory=PointInTimeConfig)
    filters: UniverseFiltersConfig = Field(default_factory=UniverseFiltersConfig)
    output: UniverseOutputConfig = Field(default_factory=UniverseOutputConfig)
