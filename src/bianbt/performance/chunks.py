"""Deterministic left-closed/right-open temporal chunk planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bianbt.config.common import as_utc
from bianbt.config.durations import duration_seconds


class ChunkPlanError(ValueError):
    """A time range cannot be split without gaps or ambiguous boundaries."""


@dataclass(frozen=True)
class TimeChunk:
    ordinal: int
    start: datetime
    end: datetime
    input_start: datetime

    @property
    def duration_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


def plan_time_chunks(
    *,
    start: datetime,
    end: datetime,
    chunk_interval: str,
    overlap_seconds: int = 0,
    earliest_input: datetime | None = None,
) -> tuple[TimeChunk, ...]:
    """Return contiguous core windows with a bounded backward input overlap."""

    checked_start, checked_end = as_utc(start), as_utc(end)
    assert checked_start is not None and checked_end is not None
    if checked_end <= checked_start:
        raise ChunkPlanError("chunk range end must be greater than start")
    if overlap_seconds < 0:
        raise ChunkPlanError("overlap_seconds must be non-negative")
    seconds = duration_seconds(chunk_interval)
    floor = as_utc(earliest_input) if earliest_input is not None else None
    chunks = []
    cursor = checked_start
    ordinal = 0
    while cursor < checked_end:
        core_end = min(checked_end, cursor + timedelta(seconds=seconds))
        input_start = cursor - timedelta(seconds=overlap_seconds)
        if floor is not None:
            input_start = max(input_start, floor)
        chunks.append(
            TimeChunk(
                ordinal=ordinal,
                start=cursor,
                end=core_end,
                input_start=input_start,
            )
        )
        cursor = core_end
        ordinal += 1
    for previous, current in zip(chunks, chunks[1:]):
        if previous.end != current.start:
            raise AssertionError("internal chunk planner gap")
    return tuple(chunks)
