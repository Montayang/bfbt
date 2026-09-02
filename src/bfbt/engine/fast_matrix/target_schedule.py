"""Canonical shared target schedule consumed by matrix and event execution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import polars as pl

from bfbt.data.hashing import content_sha256

UTC_MS = pl.Datetime("ms", "UTC")
TARGET_SCHEDULE_VERSION = "a36-target-schedule-v3-dictionary-identity"
IDENTITY_CHUNK_ROWS = 10_000
TARGET_SCHEDULE_SCHEMA = {
    "signal_time": UTC_MS,
    "fill_time": UTC_MS,
    "symbol": pl.Categorical,
    "target_weight": pl.Float64,
    "source_signal_id": pl.Categorical,
    "factor_version": pl.Categorical,
    "universe_version": pl.Categorical,
    "portfolio_version": pl.Categorical,
}


class TargetScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class TargetSchedule:
    frame: pl.DataFrame
    rebalance_times: tuple[datetime, ...]
    schedule_id: str
    parent_manifest_sha256: str


def build_target_schedule(
    frame: pl.DataFrame | pl.LazyFrame,
    *,
    rebalance_times: tuple[datetime, ...],
    parent_manifest_sha256: str,
) -> TargetSchedule:
    if len(parent_manifest_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in parent_manifest_sha256
    ):
        raise TargetScheduleError("parent_manifest_sha256 must be SHA-256")
    actual = frame.collect(engine="streaming") if isinstance(frame, pl.LazyFrame) else frame
    missing = set(TARGET_SCHEDULE_SCHEMA) - set(actual.columns)
    if missing:
        raise TargetScheduleError(f"target schedule is missing columns: {sorted(missing)}")
    actual = actual.select(list(TARGET_SCHEDULE_SCHEMA)).cast(TARGET_SCHEDULE_SCHEMA).sort(
        ["fill_time", "symbol"]
    )
    duplicate = actual.group_by(["fill_time", "symbol"]).len().filter(pl.col("len") > 1)
    if duplicate.height:
        raise TargetScheduleError("target schedule contains duplicate fill_time/symbol")
    if actual.filter(pl.col("fill_time") <= pl.col("signal_time")).height:
        raise TargetScheduleError("fill_time must be after signal_time")
    if actual.filter(
        ~pl.col("target_weight").is_finite()
        | pl.col("symbol").cast(pl.String).str.strip_chars().eq("")
    ).height:
        raise TargetScheduleError("target weights must be finite and symbols non-empty")
    ordered = tuple(sorted(set(rebalance_times)))
    if len(ordered) != len(rebalance_times):
        raise TargetScheduleError("rebalance_times must be unique")
    unknown = set(actual["fill_time"].unique().to_list()) - set(ordered)
    if unknown:
        raise TargetScheduleError("target rows must belong to an explicit full rebalance snapshot")
    row_chunks = [
        {
            "row_count": part.height,
            "sha256": content_sha256([
                {
                    name: value.isoformat() if isinstance(value, datetime) else value
                    for name, value in row.items()
                }
                for row in part.to_dicts()
            ]),
        }
        for part in actual.iter_slices(IDENTITY_CHUNK_ROWS)
    ]
    payload = {
        "version": TARGET_SCHEDULE_VERSION,
        "parent_manifest_sha256": parent_manifest_sha256,
        "rebalance_times": [value.isoformat() for value in ordered],
        "row_count": actual.height,
        "row_chunks": row_chunks,
    }
    return TargetSchedule(
        frame=actual,
        rebalance_times=ordered,
        schedule_id=f"target-{content_sha256(payload)[:24]}",
        parent_manifest_sha256=parent_manifest_sha256,
    )
