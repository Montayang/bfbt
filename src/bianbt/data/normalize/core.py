"""Deterministic normalization of verified Binance Raw objects."""

from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit

import pyarrow as pa

from bianbt.config.durations import duration_seconds
from bianbt.data.hashing import content_sha256, sha256_file
from bianbt.data.manifests import DatasetName, RawObjectManifest
from bianbt.data.schemas import get_schema_definition, validate_arrow_schema

NORMALIZER_CODE_VERSION = "a05-normalizer-v1"


class NormalizationError(ValueError):
    """A verified Raw object cannot be mapped to its normalized contract."""


@dataclass(frozen=True)
class NormalizedBatch:
    dataset_name: DatasetName
    schema_version: str
    dataset_version: str
    table: pa.Table
    partition_values: dict[str, str]
    source_manifests: tuple[RawObjectManifest, ...]
    normalizer_parameters_hash: str

    @property
    def time_column(self) -> str:
        return {
            "bars": "open_time",
            "mark_bars": "open_time",
            "funding": "funding_time",
            "contracts": "snapshot_time",
        }[self.dataset_name]


@dataclass(frozen=True)
class NormalizationRelease:
    dataset_name: DatasetName
    dataset_version: str
    normalizer_parameters_hash: str
    sources: tuple[tuple[str, str], ...]


def _safe_child(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents:
        raise NormalizationError("Raw object path escapes its configured root")
    return candidate


def raw_object_path(manifest: RawObjectManifest, raw_root: Path) -> Path:
    """Derive the portable A04 Raw path from a validated manifest."""

    parsed = urlsplit(manifest.source_uri)
    if manifest.source == "binance_public_archive":
        prefix = "/data/futures/um/"
        if not parsed.path.startswith(prefix):
            raise NormalizationError("archive URI is not a USD-M public-data path")
        relative = f"binance/futures/um/{parsed.path.removeprefix(prefix)}"
    else:
        kinds = {
            "/fapi/v1/klines": "klines",
            "/fapi/v1/markPriceKlines": "markPriceKlines",
            "/fapi/v1/fundingRate": "fundingRate",
            "/fapi/v1/exchangeInfo": "exchangeInfo",
            "/fapi/v1/fundingInfo": "fundingInfo",
        }
        try:
            kind = kinds[parsed.path]
        except KeyError as exc:
            raise NormalizationError(f"unsupported public REST object: {parsed.path}") from exc
        parts = ["binance", "futures", "um", "rest", kind]
        if manifest.symbol is not None:
            parts.append(manifest.symbol)
        if manifest.interval is not None:
            parts.append(manifest.interval)
        parts.append(f"{manifest.object_id}.json")
        relative = "/".join(parts)
    path = _safe_child(raw_root, relative)
    if not path.is_file():
        raise NormalizationError(f"Raw object file does not exist: {path}")
    if path.stat().st_size != manifest.byte_size:
        raise NormalizationError(f"Raw object size differs from manifest: {path}")
    if sha256_file(path) != manifest.checksum_sha256:
        raise NormalizationError(f"Raw object checksum differs from manifest: {path}")
    return path


def _json_payload(manifest: RawObjectManifest, raw_root: Path) -> Any:
    path = raw_object_path(manifest, raw_root)
    try:
        return json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NormalizationError(f"Raw REST object is invalid JSON: {path}") from exc


def _archive_rows(manifest: RawObjectManifest, raw_root: Path) -> list[list[str]]:
    path = raw_object_path(manifest, raw_root)
    try:
        with zipfile.ZipFile(path) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) != 1:
                raise NormalizationError("archive must contain exactly one data file")
            with archive.open(members[0]) as source:
                text = io.TextIOWrapper(source, encoding="utf-8", newline="")
                return [list(row) for row in csv.reader(text) if row]
    except zipfile.BadZipFile as exc:
        raise NormalizationError(f"Raw object is not a valid ZIP: {path}") from exc


def _float(value: object, field: str, *, nullable: bool = False) -> float | None:
    if nullable and (value is None or value == ""):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{field} must be numeric") from exc
    if not math.isfinite(result):
        raise NormalizationError(f"{field} must be finite")
    return result


def _int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise NormalizationError(f"{field} must be an integer")
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise NormalizationError(f"{field} must be an integer") from exc
    if isinstance(value, float) and value != result:
        raise NormalizationError(f"{field} must be an integer")
    return result


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NormalizationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if not normalized.isprintable():
        raise NormalizationError(f"{field} must contain printable text")
    return normalized


def _time(value: object, field: str, *, end_inclusive: bool = False) -> datetime:
    milliseconds = _int(value, field)
    if milliseconds < 100_000_000_000 or milliseconds > 100_000_000_000_000:
        raise NormalizationError(f"{field} is not a millisecond Unix timestamp")
    if end_inclusive:
        milliseconds += 1
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise NormalizationError(f"{field} is outside supported datetime range") from exc


def _dataset_version(
    dataset_name: DatasetName,
    manifests: tuple[RawObjectManifest, ...],
    parameters: dict[str, object],
) -> tuple[str, str]:
    definition = get_schema_definition(dataset_name, "v1")
    parameters_hash = content_sha256(parameters)
    identity = {
        "dataset_name": dataset_name,
        "schema_version": "v1",
        "schema_fingerprint": definition.fingerprint,
        "normalizer_code_version": NORMALIZER_CODE_VERSION,
        "normalizer_parameters_hash": parameters_hash,
        "sources": [
            {
                "object_id": item.object_id,
                "checksum_sha256": item.checksum_sha256,
            }
            for item in manifests
        ],
    }
    return f"a05-{content_sha256(identity)[:24]}", parameters_hash


def _prepare_manifests(
    manifests: Iterable[RawObjectManifest], dataset_name: DatasetName
) -> tuple[RawObjectManifest, ...]:
    items = tuple(sorted(manifests, key=lambda item: item.object_id))
    if not items:
        raise NormalizationError("at least one Raw object is required")
    if len({item.object_id for item in items}) != len(items):
        raise NormalizationError("Raw object IDs must be unique")
    if any(item.dataset_name != dataset_name for item in items):
        raise NormalizationError("all Raw objects must belong to the target dataset")
    return items


def _normalizer_parameters(dataset_name: DatasetName) -> dict[str, object]:
    if dataset_name in {"bars", "mark_bars"}:
        return {
            "dataset_name": dataset_name,
            "close_time": "source_inclusive_plus_1ms",
            "is_complete": "archive_or_close_boundary_le_retrieved_at",
        }
    if dataset_name == "funding":
        return {"funding_interval": "fundingInfo_by_symbol_when_available"}
    return {
        "filters": "PRICE_FILTER_and_LOT_SIZE",
        "precision_fields": False,
    }


def build_normalization_release(
    dataset_name: DatasetName,
    manifests: Iterable[RawObjectManifest],
) -> NormalizationRelease:
    """Bind many bounded normalization batches to one immutable release."""

    items = _prepare_manifests(manifests, dataset_name)
    version, parameters_hash = _dataset_version(
        dataset_name,
        items,
        _normalizer_parameters(dataset_name),
    )
    return NormalizationRelease(
        dataset_name=dataset_name,
        dataset_version=version,
        normalizer_parameters_hash=parameters_hash,
        sources=tuple(
            (item.object_id, item.checksum_sha256) for item in items
        ),
    )


def _release_version(
    dataset_name: DatasetName,
    items: tuple[RawObjectManifest, ...],
    parameters: dict[str, object],
    release: NormalizationRelease | None,
) -> tuple[str, str]:
    if release is None:
        return _dataset_version(dataset_name, items, parameters)
    if release.dataset_name != dataset_name:
        raise NormalizationError("release dataset does not match batch")
    parameters_hash = content_sha256(parameters)
    if release.normalizer_parameters_hash != parameters_hash:
        raise NormalizationError("release parameters do not match batch")
    allowed = set(release.sources)
    supplied = {(item.object_id, item.checksum_sha256) for item in items}
    if not supplied <= allowed:
        raise NormalizationError("batch contains sources outside release")
    return release.dataset_version, parameters_hash




def _partition_values(
    table: pa.Table,
    *,
    time_column: str,
    interval: str | None,
) -> dict[str, str]:
    if table.num_rows == 0:
        raise NormalizationError("normalization produced no rows")
    times = table.column(time_column).to_pylist()
    months = {(item.year, item.month) for item in times}
    if len(months) != 1:
        raise NormalizationError("one normalized batch cannot cross a UTC month")
    year, month = months.pop()
    values = {"year": f"{year:04d}", "month": f"{month:02d}"}
    if interval is not None:
        values["interval"] = interval
    return values


def _table(dataset_name: DatasetName, rows: list[dict[str, object]]) -> pa.Table:
    definition = get_schema_definition(dataset_name, "v1")
    try:
        table = pa.Table.from_pylist(rows, schema=definition.schema)
    except (pa.ArrowException, TypeError, ValueError) as exc:
        raise NormalizationError(f"rows do not match {dataset_name}/v1") from exc
    table = table.sort_by([(key, "ascending") for key in definition.sort_key])
    validate_arrow_schema(table.schema, dataset=dataset_name, version="v1")
    return table


def _kline_records(manifest: RawObjectManifest, raw_root: Path) -> list[list[object]]:
    if manifest.source == "binance_public_archive":
        rows: list[list[object]] = _archive_rows(manifest, raw_root)
        if rows and rows[0] and not str(rows[0][0]).isdigit():
            rows = rows[1:]
        return rows
    payload = _json_payload(manifest, raw_root)
    if not isinstance(payload, list) or any(not isinstance(item, list) for item in payload):
        raise NormalizationError("REST kline payload must be an array of arrays")
    return payload


def normalize_bars(
    manifests: Iterable[RawObjectManifest],
    *,
    raw_root: Path,
    dataset_name: Literal["bars", "mark_bars"] = "bars",
    release: NormalizationRelease | None = None,
) -> NormalizedBatch:
    """Normalize trade or mark-price klines without repairing invalid rows."""

    items = _prepare_manifests(manifests, dataset_name)
    intervals = {item.interval for item in items}
    symbols = {item.symbol for item in items}
    if None in intervals or None in symbols or len(intervals) != 1:
        raise NormalizationError("bar Raw objects require one common interval and symbols")
    interval = next(iter(intervals))
    assert interval is not None
    duration_seconds(interval)
    parameters = _normalizer_parameters(dataset_name)
    version, parameters_hash = _release_version(
        dataset_name, items, parameters, release
    )
    rows: list[dict[str, object]] = []
    for manifest in items:
        assert manifest.symbol is not None
        for record in _kline_records(manifest, raw_root):
            if len(record) < 7:
                raise NormalizationError("kline record must contain at least 7 fields")
            open_time = _time(record[0], "open_time")
            close_time = _time(record[6], "close_time", end_inclusive=True)
            common: dict[str, object] = {
                "open_time": open_time,
                "close_time": close_time,
                "symbol": manifest.symbol,
                "interval": interval,
                "open": _float(record[1], "open"),
                "high": _float(record[2], "high"),
                "low": _float(record[3], "low"),
                "close": _float(record[4], "close"),
                "is_complete": (
                    manifest.source == "binance_public_archive"
                    or close_time <= manifest.retrieved_at
                ),
                "source": manifest.source,
                "source_object_id": manifest.object_id,
                "dataset_version": version,
            }
            if dataset_name == "bars":
                if len(record) < 11:
                    raise NormalizationError("trade kline record must contain 11 fields")
                common.update(
                    {
                        "volume": _float(record[5], "volume"),
                        "quote_volume": _float(record[7], "quote_volume"),
                        "trades": _int(record[8], "trades"),
                        "taker_buy_volume": _float(record[9], "taker_buy_volume"),
                        "taker_buy_quote_volume": _float(
                            record[10], "taker_buy_quote_volume"
                        ),
                    }
                )
            rows.append(common)
    table = _table(dataset_name, rows)
    return NormalizedBatch(
        dataset_name=dataset_name,
        schema_version="v1",
        dataset_version=version,
        table=table,
        partition_values=_partition_values(
            table, time_column="open_time", interval=interval
        ),
        source_manifests=items,
        normalizer_parameters_hash=parameters_hash,
    )


def normalize_funding(
    manifests: Iterable[RawObjectManifest],
    *,
    raw_root: Path,
    release: NormalizationRelease | None = None,
) -> NormalizedBatch:
    """Normalize funding settlements, optionally enriching interval snapshots."""

    items = _prepare_manifests(manifests, "funding")
    parameters = _normalizer_parameters("funding")
    version, parameters_hash = _release_version(
        "funding", items, parameters, release
    )
    intervals: dict[str, float] = {}
    record_sources: list[tuple[RawObjectManifest, object]] = []
    for manifest in items:
        if urlsplit(manifest.source_uri).path == "/fapi/v1/fundingInfo":
            payload = _json_payload(manifest, raw_root)
            if not isinstance(payload, list):
                raise NormalizationError("fundingInfo payload must be an array")
            for item in payload:
                if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
                    raise NormalizationError("fundingInfo records require symbol")
                value = _float(item.get("fundingIntervalHours"), "fundingIntervalHours")
                assert value is not None
                intervals[item["symbol"].upper()] = value
            continue
        if manifest.source == "binance_public_archive":
            records = _archive_rows(manifest, raw_root)
            if not records:
                continue
            header = [item.strip() for item in records[0]]
            if "calc_time" not in header and "fundingTime" not in header:
                raise NormalizationError("funding archive requires a recognized header")
            for values in records[1:]:
                record_sources.append((manifest, dict(zip(header, values))))
        else:
            payload = _json_payload(manifest, raw_root)
            if not isinstance(payload, list):
                raise NormalizationError("fundingRate payload must be an array")
            record_sources.extend((manifest, item) for item in payload)
    rows: list[dict[str, object]] = []
    for manifest, record in record_sources:
        if not isinstance(record, dict):
            raise NormalizationError("funding record must be an object")
        symbol_value = record.get("symbol") or manifest.symbol
        if not isinstance(symbol_value, str):
            raise NormalizationError("funding record requires symbol")
        symbol = symbol_value.upper()
        time_value = record.get("fundingTime", record.get("calc_time"))
        rate_value = record.get("fundingRate", record.get("last_funding_rate"))
        rows.append(
            {
                "funding_time": _time(time_value, "funding_time"),
                "symbol": symbol,
                "funding_rate": _float(rate_value, "funding_rate"),
                "mark_price": _float(record.get("markPrice"), "mark_price", nullable=True),
                "funding_interval_hours": intervals.get(symbol),
                "source_object_id": manifest.object_id,
                "dataset_version": version,
            }
        )
    table = _table("funding", rows)
    return NormalizedBatch(
        dataset_name="funding",
        schema_version="v1",
        dataset_version=version,
        table=table,
        partition_values=_partition_values(
            table, time_column="funding_time", interval=None
        ),
        source_manifests=items,
        normalizer_parameters_hash=parameters_hash,
    )


def _filter_value(filters: object, filter_type: str, key: str) -> float | None:
    if not isinstance(filters, list):
        raise NormalizationError("contract filters must be an array")
    for item in filters:
        if isinstance(item, dict) and item.get("filterType") == filter_type:
            return _float(item.get(key), f"{filter_type}.{key}", nullable=True)
    return None


def normalize_contracts(
    manifests: Iterable[RawObjectManifest],
    *,
    raw_root: Path,
    release: NormalizationRelease | None = None,
) -> NormalizedBatch:
    """Normalize point-in-time exchangeInfo contract snapshots."""

    items = _prepare_manifests(manifests, "contracts")
    parameters = _normalizer_parameters("contracts")
    version, parameters_hash = _release_version(
        "contracts", items, parameters, release
    )
    rows: list[dict[str, object]] = []
    for manifest in items:
        if urlsplit(manifest.source_uri).path != "/fapi/v1/exchangeInfo":
            raise NormalizationError("contracts input must be exchangeInfo")
        payload = _json_payload(manifest, raw_root)
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise NormalizationError("exchangeInfo requires a symbols array")
        for record in payload["symbols"]:
            if not isinstance(record, dict):
                raise NormalizationError("exchangeInfo symbol record must be an object")
            filters = record.get("filters")
            tick = _filter_value(filters, "PRICE_FILTER", "tickSize")
            step = _filter_value(filters, "LOT_SIZE", "stepSize")
            if tick is None or step is None:
                raise NormalizationError("contract requires PRICE_FILTER and LOT_SIZE")
            rows.append(
                {
                    "snapshot_time": manifest.retrieved_at,
                    "symbol": _string(record.get("symbol"), "symbol").upper(),
                    "contract_type": _string(record.get("contractType"), "contractType"),
                    "status": _string(record.get("status"), "status"),
                    "base_asset": _string(record.get("baseAsset"), "baseAsset"),
                    "quote_asset": _string(record.get("quoteAsset"), "quoteAsset"),
                    "margin_asset": _string(record.get("marginAsset"), "marginAsset"),
                    "onboard_time": (
                        _time(record["onboardDate"], "onboardDate")
                        if record.get("onboardDate") is not None
                        else None
                    ),
                    "delivery_time": (
                        _time(record["deliveryDate"], "deliveryDate")
                        if record.get("deliveryDate") is not None
                        else None
                    ),
                    "price_tick": tick,
                    "quantity_step": step,
                    "min_quantity": _filter_value(filters, "LOT_SIZE", "minQty"),
                    "min_notional": _filter_value(filters, "MIN_NOTIONAL", "notional"),
                    "observed_first_bar": None,
                    "observed_last_bar": None,
                    "source_object_id": manifest.object_id,
                    "dataset_version": version,
                }
            )
    table = _table("contracts", rows)
    return NormalizedBatch(
        dataset_name="contracts",
        schema_version="v1",
        dataset_version=version,
        table=table,
        partition_values=_partition_values(
            table, time_column="snapshot_time", interval=None
        ),
        source_manifests=items,
        normalizer_parameters_hash=parameters_hash,
    )
