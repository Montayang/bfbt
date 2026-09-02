"""Adapters for public archives and incremental public REST data."""

from bfbt.data.sources.base import (
    ArchiveDiscoveryRequest,
    ChecksumError,
    FetchResult,
    FetchStatus,
    RawObjectConflictError,
    RemoteArchiveObject,
    RestPage,
    SourceError,
    SourceProtocolError,
    SourceUnavailableError,
)
from bfbt.data.sources.binance_archive import BinanceArchiveSource
from bfbt.data.sources.binance_rest import BinanceRestSource
from bfbt.data.sources.http import PublicHttpClient, RetryPolicy

__all__ = [
    "ArchiveDiscoveryRequest",
    "BinanceArchiveSource",
    "BinanceRestSource",
    "ChecksumError",
    "FetchResult",
    "FetchStatus",
    "PublicHttpClient",
    "RawObjectConflictError",
    "RemoteArchiveObject",
    "RestPage",
    "RetryPolicy",
    "SourceError",
    "SourceProtocolError",
    "SourceUnavailableError",
]
