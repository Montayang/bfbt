"""Forward-return labels with explicit signal, entry, and exit timing."""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import LabelDefinition
from bfbt.data.hashing import content_sha256

LABEL_ENGINE_VERSION = "a07-label-v1"


class LabelError(ValueError):
    """A forward label cannot be built with unambiguous timing."""


@dataclass(frozen=True)
class LabelResult:
    frame: pl.LazyFrame
    label_name: str
    label_version: str
    bars_dataset_version: str
    universe_version: str
    base_interval: str


def _required_columns(definition: LabelDefinition) -> set[str]:
    return {
        "open_time",
        "close_time",
        "symbol",
        "interval",
        "is_complete",
        "dataset_version",
        definition.entry_field,
        definition.exit_field,
    }


def compute_forward_returns(
    bars: pl.LazyFrame,
    universe: pl.LazyFrame,
    definition: LabelDefinition,
    *,
    base_interval: str,
    bars_dataset_version: str,
    universe_version: str,
) -> LabelResult:
    """Label eligible signal rows without exposing future data to factors."""

    if not bars_dataset_version or bars_dataset_version.lower() == "latest":
        raise LabelError("bars_dataset_version must be explicit")
    if not universe_version or universe_version.lower() == "latest":
        raise LabelError("universe_version must be explicit")
    missing = _required_columns(definition) - set(bars.collect_schema().names())
    if missing:
        raise LabelError(f"bar input is missing columns: {sorted(missing)}")
    universe_required = {
        "timestamp",
        "symbol",
        "is_eligible",
        "universe_version",
    }
    missing = universe_required - set(universe.collect_schema().names())
    if missing:
        raise LabelError(f"universe input is missing columns: {sorted(missing)}")
    base_seconds = duration_seconds(base_interval)
    horizon_seconds = duration_seconds(definition.horizon)
    if horizon_seconds % base_seconds:
        raise LabelError("label horizon must be a multiple of base_interval")
    horizon_bars = horizon_seconds // base_seconds
    entry_offset = definition.signal_delay_bars
    exit_offset = entry_offset + horizon_bars
    timestamp_type = pl.Datetime("ms", "UTC")
    prepared = (
        bars.filter(
            (pl.col("interval") == base_interval)
            & (pl.col("dataset_version") == bars_dataset_version)
        )
        .with_columns(
            pl.col("open_time").cast(timestamp_type),
            pl.col("close_time").cast(timestamp_type),
        )
        .sort(["symbol", "open_time"])
    )
    entry_time_column = (
        "open_time" if definition.entry_field == "open" else "close_time"
    )
    exit_time_column = (
        "open_time" if definition.exit_field == "open" else "close_time"
    )
    candidates = (
        prepared.with_columns(
            pl.col(definition.entry_field)
            .shift(-entry_offset)
            .over("symbol")
            .alias("entry_price"),
            pl.col(definition.exit_field)
            .shift(-exit_offset)
            .over("symbol")
            .alias("exit_price"),
            pl.col(entry_time_column)
            .shift(-entry_offset)
            .over("symbol")
            .alias("entry_time"),
            pl.col(exit_time_column)
            .shift(-exit_offset)
            .over("symbol")
            .alias("exit_time"),
            pl.col("open_time")
            .shift(-entry_offset)
            .over("symbol")
            .alias("_entry_open_time"),
            pl.col("open_time")
            .shift(-exit_offset)
            .over("symbol")
            .alias("_exit_open_time"),
            pl.col("is_complete")
            .shift(-entry_offset)
            .over("symbol")
            .alias("_entry_complete"),
            pl.col("is_complete")
            .shift(-exit_offset)
            .over("symbol")
            .alias("_exit_complete"),
        )
        .with_columns(
            (
                (
                    pl.col("_entry_open_time").cast(pl.Int64)
                    - pl.col("close_time").cast(pl.Int64)
                    == (entry_offset - 1) * base_seconds * 1_000
                )
                & (
                    pl.col("_exit_open_time").cast(pl.Int64)
                    - pl.col("close_time").cast(pl.Int64)
                    == (exit_offset - 1) * base_seconds * 1_000
                )
            ).alias("_contiguous"),
            (
                pl.col("_entry_complete").fill_null(False)
                & pl.col("_exit_complete").fill_null(False)
            ).alias("_prices_complete"),
        )
        .select(
            pl.col("close_time").alias("timestamp"),
            "symbol",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "_contiguous",
            "_prices_complete",
        )
    )
    eligible = universe.filter(
        pl.col("is_eligible")
        & (pl.col("universe_version") == universe_version)
    ).select(pl.col("timestamp").cast(timestamp_type), "symbol")
    values = eligible.join(
        candidates, on=["timestamp", "symbol"], how="left"
    ).with_columns(
        pl.when(pl.col("entry_time").is_null() | pl.col("exit_time").is_null())
        .then(pl.lit("INSUFFICIENT_FUTURE"))
        .when(~pl.col("_contiguous").fill_null(False))
        .then(pl.lit("GAPPED_FUTURE"))
        .when(~pl.col("_prices_complete").fill_null(False))
        .then(pl.lit("INCOMPLETE_PRICE_BAR"))
        .when(
            pl.col("entry_price").is_null()
            | pl.col("exit_price").is_null()
            | ~pl.col("entry_price").is_finite()
            | ~pl.col("exit_price").is_finite()
            | (pl.col("entry_price") <= 0)
            | (pl.col("exit_price") <= 0)
        )
        .then(pl.lit("INVALID_PRICE"))
        .otherwise(None)
        .alias("invalid_reason")
    ).with_columns(
        pl.col("invalid_reason").is_null().alias("is_valid"),
        pl.when(pl.col("invalid_reason").is_null())
        .then(pl.col("exit_price") / pl.col("entry_price") - 1.0)
        .otherwise(None)
        .alias("forward_return"),
    )
    identity = {
        "engine": LABEL_ENGINE_VERSION,
        "definition": definition.model_dump(mode="json"),
        "bars_dataset_version": bars_dataset_version,
        "universe_version": universe_version,
        "base_interval": base_interval,
    }
    version = f"v1-{content_sha256(identity)[:24]}"
    output = (
        values.with_columns(
            pl.lit(definition.name).alias("label_name"),
            pl.lit(version).alias("label_version"),
            pl.lit(bars_dataset_version).alias("dataset_version"),
        )
        .select(
            "timestamp",
            "symbol",
            "label_name",
            "label_version",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "forward_return",
            "is_valid",
            "invalid_reason",
            "dataset_version",
        )
        .sort(["timestamp", "symbol"])
    )
    return LabelResult(
        frame=output,
        label_name=definition.name,
        label_version=version,
        bars_dataset_version=bars_dataset_version,
        universe_version=universe_version,
        base_interval=base_interval,
    )
