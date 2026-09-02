"""Factor, preprocessing, and label configuration models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from bfbt.config.common import StrictModel
from bfbt.config.durations import duration_seconds


class FrozenDict(dict[str, JsonValue]):
    """A JSON-compatible mapping that cannot change after validation."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("configuration mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class PreprocessStep(StrictModel):
    name: Literal["winsorize", "zscore", "rank"]
    method: Literal["quantile"] | None = None
    lower: float | None = Field(default=None, ge=0, le=1)
    upper: float | None = Field(default=None, ge=0, le=1)
    cross_sectional: bool = True

    @model_validator(mode="after")
    def validate_parameters(self) -> "PreprocessStep":
        if self.name == "winsorize":
            if self.method != "quantile":
                raise ValueError("winsorize requires method='quantile'")
            if self.lower is None or self.upper is None:
                raise ValueError("winsorize requires lower and upper")
            if self.lower >= self.upper:
                raise ValueError("winsorize lower must be less than upper")
        elif any(value is not None for value in (self.method, self.lower, self.upper)):
            raise ValueError(
                "method, lower, and upper are only valid for winsorize"
            )
        return self


class FactorDefinition(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=FrozenDict)
    compute_interval: str
    preprocess: tuple[PreprocessStep, ...] = ()

    @field_validator("compute_interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value

    @field_validator("parameters")
    @classmethod
    def validate_duration_parameters(
        cls, values: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        for key in (
            "lookback",
            "skip_recent",
            "window",
            "horizon",
            "source_interval",
            "sample_interval",
        ):
            value = values.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"parameter {key!r} must be a duration string")
                duration_seconds(value)
        return FrozenDict(values)


class LabelDefinition(StrictModel):
    name: str = Field(min_length=1)
    signal_delay_bars: int = Field(default=1, ge=1)
    horizon: str
    entry_field: Literal["open", "close"] = "open"
    exit_field: Literal["open", "close"] = "open"

    @field_validator("horizon")
    @classmethod
    def validate_horizon(cls, value: str) -> str:
        duration_seconds(value)
        return value


class FactorCacheConfig(StrictModel):
    enabled: bool = True


class FactorConfig(StrictModel):
    factors: tuple[FactorDefinition, ...] = ()
    labels: tuple[LabelDefinition, ...] = ()
    cache: FactorCacheConfig = Field(default_factory=FactorCacheConfig)

    @model_validator(mode="after")
    def validate_unique_names(self) -> "FactorConfig":
        for values, label in ((self.factors, "factor"), (self.labels, "label")):
            names = [value.name for value in values]
            if len(names) != len(set(names)):
                raise ValueError(f"{label} names must be unique")
        return self
