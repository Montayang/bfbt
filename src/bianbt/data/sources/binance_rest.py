"""Public USD-M REST pagination and metadata snapshot adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any, Literal

from bianbt.config.common import as_utc
from bianbt.config.durations import duration_seconds
from bianbt.data.sources.base import (
    BINANCE_KLINE_INTERVALS,
    RestPage,
    SourceProtocolError,
    normalize_binance_symbol,
)
from bianbt.data.sources.http import PublicHttpClient

_BASE_URL = "https://fapi.binance.com"
_KLINE_ENDPOINTS = {
    "bars": "/fapi/v1/klines",
    "mark_bars": "/fapi/v1/markPriceKlines",
}


def _milliseconds(value: datetime) -> int:
    checked = as_utc(value)
    assert checked is not None
    return int(checked.timestamp() * 1000)


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _json(response_body: bytes, endpoint: str) -> Any:
    try:
        return json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceProtocolError(f"{endpoint} returned invalid JSON") from exc


def _symbol(value: str) -> str:
    return normalize_binance_symbol(value)


class BinanceRestSource:
    """Read only public market-data endpoints; no API key is accepted."""

    def __init__(
        self,
        http: PublicHttpClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.http = http
        self._now = now

    def kline_pages(
        self,
        *,
        dataset_name: Literal["bars", "mark_bars"],
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int = 1500,
    ) -> Iterator[RestPage]:
        if not 1 <= limit <= 1500:
            raise ValueError("kline limit must be between 1 and 1500")
        if interval not in BINANCE_KLINE_INTERVALS:
            raise ValueError("interval is not supported by USD-M REST klines")
        interval_ms = duration_seconds(interval) * 1000
        start_ms = _milliseconds(start)
        end_ms = _milliseconds(end)
        if end_ms <= start_ms:
            raise ValueError("end must be greater than start")
        endpoint = _KLINE_ENDPOINTS[dataset_name]
        normalized_symbol = _symbol(symbol)
        cursor = start_ms
        page_number = 1
        previous_open: int | None = None
        while cursor < end_ms:
            response = self.http.request(
                "GET",
                f"{_BASE_URL}{endpoint}",
                params={
                    "symbol": normalized_symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": limit,
                },
            )
            payload = _json(response.content, endpoint)
            if not isinstance(payload, list):
                raise SourceProtocolError(f"{endpoint} response must be a JSON array")
            if not payload:
                break
            if len(payload) > limit:
                raise SourceProtocolError(f"{endpoint} returned more than limit rows")
            opens: list[int] = []
            closes: list[int] = []
            for record in payload:
                if not isinstance(record, list) or len(record) < 7:
                    raise SourceProtocolError(
                        f"{endpoint} kline records must contain at least 7 fields"
                    )
                if not isinstance(record[0], int) or not isinstance(record[6], int):
                    raise SourceProtocolError(
                        f"{endpoint} kline timestamps must be integers"
                    )
                opens.append(record[0])
                closes.append(record[6])
            if opens != sorted(set(opens)):
                raise SourceProtocolError(
                    f"{endpoint} kline open times must be unique and increasing"
                )
            if opens[0] < cursor or opens[-1] >= end_ms:
                raise SourceProtocolError(f"{endpoint} returned rows outside request range")
            if previous_open is not None and opens[0] <= previous_open:
                raise SourceProtocolError(f"{endpoint} pagination did not advance")
            retrieved_at = self._now()
            yield RestPage(
                dataset_name=dataset_name,
                endpoint=endpoint,
                source_uri=str(response.request.url),
                symbol=normalized_symbol,
                interval=interval,
                available_from=_from_milliseconds(opens[0]),
                available_to=_from_milliseconds(closes[-1] + 1),
                retrieved_at=retrieved_at,
                page_number=page_number,
                records=tuple(payload),
                response_body=response.content,
                http_status=response.status_code,
            )
            previous_open = opens[-1]
            next_cursor = previous_open + interval_ms
            if next_cursor <= cursor:
                raise SourceProtocolError(f"{endpoint} pagination cursor stalled")
            cursor = next_cursor
            page_number += 1
            if len(payload) < limit:
                break

    def funding_pages(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
        limit: int = 1000,
    ) -> Iterator[RestPage]:
        if not 1 <= limit <= 1000:
            raise ValueError("funding limit must be between 1 and 1000")
        start_ms = _milliseconds(start)
        end_ms = _milliseconds(end)
        if end_ms <= start_ms:
            raise ValueError("end must be greater than start")
        endpoint = "/fapi/v1/fundingRate"
        normalized_symbol = _symbol(symbol)
        cursor = start_ms
        page_number = 1
        previous_time: int | None = None
        while cursor < end_ms:
            response = self.http.request(
                "GET",
                f"{_BASE_URL}{endpoint}",
                params={
                    "symbol": normalized_symbol,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": limit,
                },
            )
            payload = _json(response.content, endpoint)
            if not isinstance(payload, list):
                raise SourceProtocolError(f"{endpoint} response must be a JSON array")
            if not payload:
                break
            if len(payload) > limit:
                raise SourceProtocolError(f"{endpoint} returned more than limit rows")
            times: list[int] = []
            for record in payload:
                if not isinstance(record, dict) or not isinstance(
                    record.get("fundingTime"), int
                ):
                    raise SourceProtocolError(
                        f"{endpoint} records require integer fundingTime"
                    )
                if record.get("symbol") != normalized_symbol:
                    raise SourceProtocolError(
                        f"{endpoint} returned a different symbol"
                    )
                times.append(record["fundingTime"])
            if times != sorted(set(times)):
                raise SourceProtocolError(
                    f"{endpoint} funding times must be unique and increasing"
                )
            if times[0] < cursor or times[-1] >= end_ms:
                raise SourceProtocolError(f"{endpoint} returned rows outside request range")
            if previous_time is not None and times[0] <= previous_time:
                raise SourceProtocolError(f"{endpoint} pagination did not advance")
            retrieved_at = self._now()
            yield RestPage(
                dataset_name="funding",
                endpoint=endpoint,
                source_uri=str(response.request.url),
                symbol=normalized_symbol,
                interval=None,
                available_from=_from_milliseconds(times[0]),
                available_to=_from_milliseconds(times[-1] + 1),
                retrieved_at=retrieved_at,
                page_number=page_number,
                records=tuple(payload),
                response_body=response.content,
                http_status=response.status_code,
            )
            previous_time = times[-1]
            cursor = previous_time + 1
            page_number += 1
            if len(payload) < limit:
                break

    def exchange_info(self) -> RestPage:
        return self._snapshot(
            endpoint="/fapi/v1/exchangeInfo",
            dataset_name="contracts",
        )

    def funding_info(self) -> RestPage:
        return self._snapshot(
            endpoint="/fapi/v1/fundingInfo",
            dataset_name="funding",
        )

    def _snapshot(
        self,
        *,
        endpoint: str,
        dataset_name: Literal["contracts", "funding"],
    ) -> RestPage:
        response = self.http.request("GET", f"{_BASE_URL}{endpoint}")
        payload = _json(response.content, endpoint)
        if endpoint.endswith("exchangeInfo") and not isinstance(payload, dict):
            raise SourceProtocolError("exchangeInfo response must be a JSON object")
        if endpoint.endswith("fundingInfo") and not isinstance(payload, list):
            raise SourceProtocolError("fundingInfo response must be a JSON array")
        retrieved_at = self._now()
        records = tuple(payload) if isinstance(payload, list) else (payload,)
        return RestPage(
            dataset_name=dataset_name,
            endpoint=endpoint,
            source_uri=str(response.request.url),
            symbol=None,
            interval=None,
            available_from=retrieved_at,
            available_to=None,
            retrieved_at=retrieved_at,
            page_number=1,
            records=records,
            response_body=response.content,
            http_status=response.status_code,
        )
