"""Small synchronous HTTP layer with bounded, observable retries."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from bianbt.data.sources.base import SourceError, SourceUnavailableError

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 4
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must be non-negative")

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after is not None:
            try:
                parsed = float(retry_after)
            except ValueError:
                parsed = -1
            if parsed >= 0:
                return min(parsed, self.max_delay_seconds)
        return min(self.base_delay_seconds * (2**attempt), self.max_delay_seconds)


class PublicHttpClient:
    """HTTP client that never accepts account credentials or auth headers."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        retry_policy: RetryPolicy | None = None,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleeper = sleeper
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "bianbt-public-market-data/0.1"},
        )
        forbidden = {key.lower() for key in self.client.headers} & {
            "authorization",
            "x-mbx-apikey",
        }
        if forbidden:
            raise ValueError("public HTTP client must not contain authentication headers")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "PublicHttpClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, object] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                response = self.client.request(method, url, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.retry_policy.max_retries:
                    break
                self._sleeper(self.retry_policy.delay(attempt, None))
                continue
            if response.status_code == 404 and allow_not_found:
                return response
            if response.status_code not in _RETRYABLE_STATUS:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    raise SourceUnavailableError(
                        f"HTTP {response.status_code} for {response.request.url}"
                    ) from exc
                return response
            if attempt >= self.retry_policy.max_retries:
                last_error = SourceUnavailableError(
                    f"HTTP {response.status_code} after retries for {response.request.url}"
                )
                break
            self._sleeper(
                self.retry_policy.delay(attempt, response.headers.get("Retry-After"))
            )
        raise SourceError(f"request failed for {method} {url}: {last_error}") from last_error

    def download(self, url: str, target: Path) -> tuple[int, str | None]:
        """Stream one response to an exact temporary path with bounded retries."""

        last_error: Exception | None = None
        for attempt in range(self.retry_policy.max_retries + 1):
            try:
                with self.client.stream("GET", url) as response:
                    if response.status_code in _RETRYABLE_STATUS:
                        if attempt >= self.retry_policy.max_retries:
                            raise SourceUnavailableError(
                                f"HTTP {response.status_code} after retries for "
                                f"{response.request.url}"
                            )
                        retry_after = response.headers.get("Retry-After")
                    else:
                        try:
                            response.raise_for_status()
                        except httpx.HTTPStatusError as exc:
                            raise SourceUnavailableError(
                                f"HTTP {response.status_code} for "
                                f"{response.request.url}"
                            ) from exc
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open("wb") as stream:
                            # Archive checksums cover the exact wire payload, so
                            # content-encoding must not be transparently decoded.
                            for chunk in response.iter_raw():
                                stream.write(chunk)
                            stream.flush()
                            os.fsync(stream.fileno())
                        return response.status_code, response.headers.get("ETag")
                self._sleeper(self.retry_policy.delay(attempt, retry_after))
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self.retry_policy.max_retries:
                    break
                self._sleeper(self.retry_policy.delay(attempt, None))
        raise SourceError(f"download failed for {url}: {last_error}") from last_error
