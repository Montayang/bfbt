"""Typed boundaries shared by public Binance market-data sources."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from bianbt.config.common import StrictModel, as_utc
from bianbt.config.durations import duration_seconds

ArchiveDataset = Literal["bars", "mark_bars", "funding"]
RestDataset = Literal["bars", "mark_bars", "funding", "contracts"]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
BINANCE_KLINE_INTERVALS = frozenset(
    {
        "1m",
        "3m",
        "5m",
        "15m",
        "30m",
        "1h",
        "2h",
        "4h",
        "6h",
        "8h",
        "12h",
        "1d",
        "3d",
        "1w",
    }
)


def normalize_binance_symbol(value: str) -> str:
    """Accept Binance Unicode symbols while rejecting path and URI punctuation."""

    normalized = value.upper()
    if not normalized or not all(
        character.isalnum() or character == "_" for character in normalized
    ):
        raise ValueError(
            "must contain only Unicode letters, digits, or underscore"
        )
    return normalized


class SourceError(RuntimeError):
    """Base error for public market-data source operations."""


class SourceProtocolError(SourceError):
    """A remote response violates the documented source contract."""


class SourceUnavailableError(SourceError):
    """A requested public object or endpoint is unavailable."""


class ChecksumError(SourceError):
    """An archive checksum is missing, malformed, or does not match."""


class RawObjectConflictError(SourceError):
    """An immutable local raw object conflicts with current upstream bytes."""


class FetchStatus(str, Enum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"


class ArchiveDiscoveryRequest(StrictModel):
    dataset_name: ArchiveDataset
    symbol: str = Field(min_length=1)
    interval: str | None = None
    frequency: Literal["monthly", "daily"]
    start: datetime
    end: datetime

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return normalize_binance_symbol(value)

    @field_validator("start", "end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def validate_request(self) -> "ArchiveDiscoveryRequest":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        if self.dataset_name in {"bars", "mark_bars"}:
            if self.interval is None:
                raise ValueError("bar archives require interval")
            duration_seconds(self.interval)
            if self.interval not in BINANCE_KLINE_INTERVALS:
                raise ValueError("interval is not supported by USD-M archive klines")
        elif self.interval is not None:
            raise ValueError("funding archives do not accept interval")
        if self.dataset_name == "funding" and self.frequency != "monthly":
            raise ValueError("fundingRate archives are monthly-only")
        return self


class RemoteArchiveObject(StrictModel):
    dataset_name: ArchiveDataset
    symbol: str
    interval: str | None
    frequency: Literal["monthly", "daily"]
    period: str
    available_from: datetime
    available_to: datetime
    url: str
    checksum_url: str
    relative_path: str

    @field_validator("available_from", "available_to")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @field_validator("url", "checksum_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "data.binance.vision"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("must be a credential-free data.binance.vision HTTPS URL")
        return value

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("must be a safe relative POSIX path")
        return path.as_posix()


class FetchResult(StrictModel):
    status: FetchStatus
    object_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    manifest_path: str = Field(min_length=1)
    http_status: int = Field(ge=100, le=599)
    byte_size: int = Field(gt=0)
    checksum_sha256: Sha256
    upstream_checksum_sha256: Sha256 | None
    retrieved_at: datetime
    etag: str | None = None
    catalog_inserted: bool | None = None

    @field_validator("retrieved_at")
    @classmethod
    def normalize_retrieved_at(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked


class RestPage(StrictModel):
    dataset_name: RestDataset
    endpoint: str
    source_uri: str
    symbol: str | None
    interval: str | None
    available_from: datetime | None
    available_to: datetime | None
    retrieved_at: datetime
    page_number: int = Field(ge=1)
    records: tuple[object, ...]
    response_body: bytes
    http_status: int = Field(ge=100, le=599)

    @field_validator("available_from", "available_to", "retrieved_at")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        return as_utc(value)
