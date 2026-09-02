"""Deterministic UTC-aligned aggregation of normalized bar data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import polars as pl

from bfbt.config.durations import duration_seconds, is_integer_multiple
from bfbt.data.hashing import content_sha256

RESAMPLER_CODE_VERSION = "a06-resampler-v1"


class ResampleError(ValueError):
    """A bar stream cannot be aggregated without ambiguous semantics."""


@dataclass(frozen=True)
class ResampleResult:
    frame: pl.LazyFrame
    dataset_name: Literal["bars", "mark_bars"]
    source_interval: str
    target_interval: str
    source_dataset_version: str
    dataset_version: str
    expected_source_bars: int


def resample_bars(
    frame: pl.LazyFrame,
    *,
    dataset_name: Literal["bars", "mark_bars"],
    source_interval: str,
    target_interval: str,
    source_dataset_version: str,
) -> ResampleResult:
    """Aggregate bars on deterministic UTC windows without filling gaps."""

    if dataset_name not in {"bars", "mark_bars"}:
        raise ResampleError("dataset_name must be bars or mark_bars")
    if not source_dataset_version or source_dataset_version.lower() == "latest":
        raise ResampleError("source_dataset_version must be explicit")
    source_seconds = duration_seconds(source_interval)
    target_seconds = duration_seconds(target_interval)
    if target_seconds <= source_seconds or not is_integer_multiple(
        target_interval, source_interval
    ):
        raise ResampleError("target_interval must be a larger integer multiple")
    expected = target_seconds // source_seconds
    required = {
        "open_time",
        "close_time",
        "symbol",
        "interval",
        "open",
        "high",
        "low",
        "close",
        "is_complete",
        "dataset_version",
    }
    if dataset_name == "bars":
        required.update(
            {
                "volume",
                "quote_volume",
                "trades",
                "taker_buy_volume",
                "taker_buy_quote_volume",
            }
        )
    missing = required - set(frame.collect_schema().names())
    if missing:
        raise ResampleError(f"bar input is missing columns: {sorted(missing)}")

    version_payload = {
        "dataset_name": dataset_name,
        "source_dataset_version": source_dataset_version,
        "source_interval": source_interval,
        "target_interval": target_interval,
        "resampler_code_version": RESAMPLER_CODE_VERSION,
        "alignment": "utc_window_left_closed_right_open",
        "incomplete_policy": "retain_and_mark_false",
    }
    version = f"a06-{content_sha256(version_payload)[:24]}"
    aggregations: list[pl.Expr] = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("is_complete").all().alias("_all_complete"),
        pl.col("open_time").n_unique().alias("_unique_times"),
        pl.col("open_time").min().alias("_first_open"),
        pl.len().alias("_row_count"),
    ]
    if dataset_name == "bars":
        aggregations.extend(
            [
                pl.col("volume").sum().alias("volume"),
                pl.col("quote_volume").sum().alias("quote_volume"),
                pl.col("trades").sum().alias("trades"),
                pl.col("taker_buy_volume").sum().alias("taker_buy_volume"),
                pl.col("taker_buy_quote_volume")
                .sum()
                .alias("taker_buy_quote_volume"),
            ]
        )
    target_delta = pl.duration(milliseconds=target_seconds * 1_000)
    output = (
        frame.filter(
            (pl.col("interval") == source_interval)
            & (pl.col("dataset_version") == source_dataset_version)
        )
        .sort(["symbol", "open_time"])
        .group_by_dynamic(
            "open_time",
            every=target_interval,
            period=target_interval,
            closed="left",
            label="left",
            group_by="symbol",
            start_by="window",
        )
        .agg(aggregations)
        .with_columns(
            (pl.col("open_time") + target_delta).alias("close_time"),
            pl.lit(target_interval).alias("interval"),
            (
                pl.col("_all_complete")
                & (pl.col("_row_count") == expected)
                & (pl.col("_unique_times") == expected)
                & (pl.col("_first_open") == pl.col("open_time"))
            ).alias("is_complete"),
            pl.lit("resampled").alias("source"),
            pl.lit(f"derived:{version}").alias("source_object_id"),
            pl.lit(version).alias("dataset_version"),
        )
        .drop("_all_complete", "_unique_times", "_first_open", "_row_count")
        .select(
            [
                "open_time",
                "close_time",
                "symbol",
                "interval",
                "open",
                "high",
                "low",
                "close",
                *(
                    [
                        "volume",
                        "quote_volume",
                        "trades",
                        "taker_buy_volume",
                        "taker_buy_quote_volume",
                    ]
                    if dataset_name == "bars"
                    else []
                ),
                "is_complete",
                "source",
                "source_object_id",
                "dataset_version",
            ]
        )
        .sort(["open_time", "symbol"])
    )
    return ResampleResult(
        frame=output,
        dataset_name=dataset_name,
        source_interval=source_interval,
        target_interval=target_interval,
        source_dataset_version=source_dataset_version,
        dataset_version=version,
        expected_source_bars=expected,
    )
