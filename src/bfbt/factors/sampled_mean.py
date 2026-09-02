"""Phase-aligned sampled price-to-mean factors on base bars."""

from __future__ import annotations

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


def _parameters(
    definition: FactorDefinition, base_interval: str
) -> tuple[int, int]:
    sample_interval = definition.parameters.get("sample_interval")
    sample_count = definition.parameters.get("sample_count")
    if not isinstance(sample_interval, str):
        raise FactorError("sample_interval must be a duration string")
    if (
        not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < 2
    ):
        raise FactorError("sample_count must be an integer of at least 2")
    sample_seconds = duration_seconds(sample_interval)
    base_seconds = duration_seconds(base_interval)
    if sample_seconds < base_seconds or sample_seconds % base_seconds:
        raise FactorError(
            "sample_interval must be an integer multiple of base_interval"
        )
    return sample_seconds // base_seconds, sample_count


def sampled_mean_ratio_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
    reverse: bool = False,
) -> pl.LazyFrame:
    """Return close / mean(phase-aligned sampled closes) - 1.

    A 15-minute interval and 12 samples on 1-minute bars uses row lags
    0, 15, ..., 165. Exact timestamp checks prevent a missing bar from being
    silently bridged by row offsets.
    """

    spacing_bars, sample_count = _parameters(definition, base_interval)
    base_ms = duration_seconds(base_interval) * 1_000
    price_names: list[str] = []
    validity: list[pl.Expr] = []
    expressions: list[pl.Expr] = []
    close_time = pl.col("close_time")
    for index in range(sample_count):
        lag = index * spacing_bars
        price_name = f"_sample_price_{index}"
        time_name = f"_sample_time_{index}"
        complete_name = f"_sample_complete_{index}"
        price_names.append(price_name)
        expressions.extend(
            (
                pl.col("close").shift(lag).over("symbol").alias(price_name),
                close_time.shift(lag).over("symbol").alias(time_name),
                pl.col("is_complete")
                .shift(lag)
                .over("symbol")
                .fill_null(False)
                .alias(complete_name),
            )
        )
        validity.extend(
            (
                pl.col(complete_name),
                pl.col(price_name).is_not_null(),
                pl.col(price_name).is_finite(),
                pl.col(price_name) > 0,
                (
                    close_time.cast(pl.Int64)
                    - pl.col(time_name).cast(pl.Int64)
                    == lag * base_ms
                ),
            )
        )
    sampled = bars.with_columns(expressions)
    mean = pl.mean_horizontal(*(pl.col(name) for name in price_names))
    raw = pl.col("close") / mean - 1.0
    if reverse:
        raw = -raw
    return sampled.select(
        close_time.alias("timestamp"),
        "symbol",
        pl.when(pl.all_horizontal(*validity))
        .then(raw)
        .otherwise(None)
        .alias("raw_value"),
    )


def sampled_mean_ratio_inverse_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
) -> pl.LazyFrame:
    """Return the negative sampled price-to-mean ratio as a distinct factor."""

    return sampled_mean_ratio_raw(
        bars, definition, base_interval=base_interval, reverse=True
    )
