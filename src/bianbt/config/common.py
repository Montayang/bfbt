"""Shared configuration types and validators."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Reject unknown fields and prevent mutation after validation."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
        str_strip_whitespace=True,
    )


def as_utc(value: datetime | None) -> datetime | None:
    """Require an aware UTC timestamp and normalize its representation."""

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must include a UTC timezone offset")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("must be expressed in UTC")
    return value.astimezone(timezone.utc)
