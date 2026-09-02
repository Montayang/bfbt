"""Data-source and local-storage configuration models."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from bfbt.config.common import StrictModel, as_utc
from bfbt.config.durations import duration_seconds, is_integer_multiple


class MarketConfig(StrictModel):
    venue: Literal["binance"] = "binance"
    segment: Literal["usd_m_futures"] = "usd_m_futures"
    contract_type: Literal["perpetual"] = "perpetual"
    quote_asset: Literal["USDT"] = "USDT"
    margin_asset: Literal["USDT"] = "USDT"


class BarDatasetConfig(StrictModel):
    enabled: bool = True
    base_interval: str = "1m"

    @field_validator("base_interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value


class ToggleDatasetConfig(StrictModel):
    enabled: bool = True


class DatasetConfig(StrictModel):
    bars: BarDatasetConfig = Field(default_factory=BarDatasetConfig)
    mark_bars: BarDatasetConfig = Field(default_factory=BarDatasetConfig)
    funding: ToggleDatasetConfig = Field(default_factory=ToggleDatasetConfig)
    contracts: ToggleDatasetConfig = Field(default_factory=ToggleDatasetConfig)
    index_bars: BarDatasetConfig = Field(
        default_factory=lambda: BarDatasetConfig(enabled=False)
    )


class SourceConfig(StrictModel):
    primary: Literal["binance_public_archive"] = "binance_public_archive"
    incremental: Literal["binance_rest"] = "binance_rest"
    allow_authenticated_endpoints: bool = False
    request_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    max_retries: int = Field(default=4, ge=0, le=20)
    max_concurrency: int = Field(default=8, ge=1, le=64)


class DataTimeConfig(StrictModel):
    timezone: Literal["UTC"] = "UTC"
    base_interval: str = "1m"
    derived_intervals: tuple[str, ...] = ("5m", "15m", "1h", "4h")
    start: datetime | None = None
    end: datetime | None = None
    range_semantics: Literal["left_closed_right_open"] = "left_closed_right_open"

    @field_validator("base_interval")
    @classmethod
    def validate_base_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value

    @field_validator("derived_intervals")
    @classmethod
    def validate_derived_intervals(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        for value in values:
            duration_seconds(value)
        if len(values) != len(set(values)):
            raise ValueError("must not contain duplicate intervals")
        return values

    @field_validator("start", "end")
    @classmethod
    def validate_utc(cls, value: datetime | None) -> datetime | None:
        return as_utc(value)

    @model_validator(mode="after")
    def validate_range_and_intervals(self) -> "DataTimeConfig":
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("end must be greater than start")
        for interval in self.derived_intervals:
            if not is_integer_multiple(interval, self.base_interval):
                raise ValueError(
                    f"derived interval {interval!r} must be an integer multiple "
                    f"of base interval {self.base_interval!r}"
                )
        return self


class StorageConfig(StrictModel):
    root: Path = Path("data/backtest/datasets/default")
    raw: Path | None = None
    normalized: Path | None = None
    curated: Path | None = None
    metadata: Path | None = None
    format: Literal["parquet"] = "parquet"
    layout: Literal["long"] = "long"
    compression: Literal["zstd"] = "zstd"
    partition_granularity: Literal["month"] = "month"
    target_file_size_mib: int = Field(default=256, ge=16, le=2048)
    row_group_rows: int = Field(default=262_144, ge=1_024)


class DataValidationConfig(StrictModel):
    reject_duplicate_keys: bool = True
    reject_bad_ohlc: bool = True
    reject_non_positive_prices: bool = True
    report_missing_bars: bool = True
    verify_archive_checksums: bool = True
    max_partition_missing_ratio: float = Field(default=0.01, ge=0, le=1)


class DataConfig(StrictModel):
    market: MarketConfig = Field(default_factory=MarketConfig)
    datasets: DatasetConfig = Field(default_factory=DatasetConfig)
    source: SourceConfig = Field(default_factory=SourceConfig)
    time: DataTimeConfig = Field(default_factory=DataTimeConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    validation: DataValidationConfig = Field(default_factory=DataValidationConfig)
