"""Deterministic quality metrics for normalized Arrow batches."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Literal

import pyarrow as pa
from pydantic import Field, field_validator, model_validator

from bianbt.config.common import StrictModel, as_utc
from bianbt.config.durations import duration_seconds
from bianbt.data.hashing import content_sha256
from bianbt.data.manifests import DatasetName
from bianbt.data.normalize.core import NormalizedBatch
from bianbt.data.schemas import get_schema_definition


class QualityError(ValueError):
    """A normalized batch does not satisfy publication quality policy."""


class QualityPolicy(StrictModel):
    reject_duplicate_keys: bool = True
    reject_null_required: bool = True
    reject_invalid_numeric: bool = True
    reject_bad_ohlc: bool = True
    reject_non_positive_prices: bool = True
    reject_negative_values: bool = True
    max_missing_ratio: float = Field(default=0.01, ge=0, le=1)


class QualityReport(StrictModel):
    report_version: Literal["quality/v1"] = "quality/v1"
    report_id: str = Field(min_length=1)
    dataset_name: DatasetName
    dataset_version: str = Field(min_length=1)
    status: Literal["pass", "fail"]
    row_count: int = Field(ge=0)
    symbols_count: int = Field(ge=0)
    duplicate_keys: int = Field(ge=0)
    null_required: int = Field(ge=0)
    invalid_numeric: int = Field(ge=0)
    bad_ohlc: int = Field(ge=0)
    non_positive_prices: int = Field(ge=0)
    negative_values: int = Field(ge=0)
    inconsistent_intervals: int = Field(ge=0)
    missing_bars: int = Field(ge=0)
    missing_ratio: float = Field(ge=0, le=1)
    source_object_ids: tuple[str, ...] = Field(min_length=1)
    errors: tuple[str, ...]
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def status_matches_errors(self) -> "QualityReport":
        if (self.status == "pass") == bool(self.errors):
            raise ValueError("quality status must be pass exactly when errors are empty")
        return self


_NUMERIC_COLUMNS = {
    "bars": (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_volume",
        "taker_buy_quote_volume",
        "trades",
    ),
    "mark_bars": ("open", "high", "low", "close"),
    "funding": ("funding_rate", "mark_price", "funding_interval_hours"),
    "contracts": ("price_tick", "quantity_step", "min_quantity", "min_notional"),
}


def _rows(table: pa.Table) -> list[dict[str, object]]:
    return table.to_pylist()


def _is_finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def evaluate_quality(
    batch: NormalizedBatch,
    *,
    policy: QualityPolicy,
    evaluated_at: datetime,
) -> QualityReport:
    """Evaluate publication-blocking rules without mutating or repairing rows."""

    definition = get_schema_definition(batch.dataset_name, batch.schema_version)
    rows = _rows(batch.table)
    keys = [tuple(row[name] for name in definition.primary_key) for row in rows]
    duplicate_keys = len(keys) - len(set(keys))
    null_required = sum(
        1
        for row in rows
        for field in definition.schema
        if not field.nullable and row[field.name] is None
    )
    invalid_numeric = 0
    for row in rows:
        for name in _NUMERIC_COLUMNS[batch.dataset_name]:
            value = row[name]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                invalid_numeric += 1

    bad_ohlc = 0
    non_positive = 0
    negative = 0
    if batch.dataset_name in {"bars", "mark_bars"}:
        for row in rows:
            price_values = [row[name] for name in ("open", "high", "low", "close")]
            if all(_is_finite_number(value) for value in price_values):
                prices = [float(value) for value in price_values]
                if not (
                    prices[2]
                    <= min(prices[0], prices[3])
                    <= max(prices[0], prices[3])
                    <= prices[1]
                ):
                    bad_ohlc += 1
                if any(value <= 0 for value in prices):
                    non_positive += 1
            if batch.dataset_name == "bars":
                amount_names = (
                    "volume",
                    "quote_volume",
                    "taker_buy_volume",
                    "taker_buy_quote_volume",
                )
                amount_values = [row[name] for name in amount_names]
                if all(_is_finite_number(value) for value in amount_values):
                    amounts = [float(value) for value in amount_values]
                    if any(value < 0 for value in amounts):
                        negative += 1
                    if (
                        amounts[2] > amounts[0] + 1e-12
                        or amounts[3] > amounts[1] + 1e-9
                    ):
                        negative += 1
                if _is_finite_number(row["trades"]) and float(row["trades"]) < 0:
                    negative += 1
    elif batch.dataset_name == "funding":
        for row in rows:
            for name in ("mark_price", "funding_interval_hours"):
                value = row[name]
                if _is_finite_number(value) and float(value) <= 0:
                    non_positive += 1
    elif batch.dataset_name == "contracts":
        for row in rows:
            required_positive = (row["price_tick"], row["quantity_step"])
            if all(_is_finite_number(value) for value in required_positive) and any(
                float(value) <= 0 for value in required_positive
            ):
                non_positive += 1
            for name in ("min_quantity", "min_notional"):
                value = row[name]
                if _is_finite_number(value) and float(value) < 0:
                    negative += 1

    inconsistent = 0
    missing = 0
    if batch.dataset_name in {"bars", "mark_bars"}:
        grouped: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        for row in rows:
            symbol = row["symbol"]
            interval = row["interval"]
            open_time = row["open_time"]
            if (
                isinstance(symbol, str)
                and isinstance(interval, str)
                and isinstance(open_time, datetime)
            ):
                grouped[(symbol, interval)].append(open_time)
        for (_, interval), times in grouped.items():
            expected_ms = duration_seconds(interval) * 1000
            for left, right in zip(sorted(times), sorted(times)[1:]):
                delta_ms = int((right - left).total_seconds() * 1000)
                if delta_ms <= 0 or delta_ms % expected_ms:
                    inconsistent += 1
                elif delta_ms > expected_ms:
                    missing += delta_ms // expected_ms - 1
    missing_ratio = missing / (len(rows) + missing) if rows or missing else 0.0

    errors: list[str] = []
    if policy.reject_duplicate_keys and duplicate_keys:
        errors.append("duplicate_primary_keys")
    if policy.reject_null_required and null_required:
        errors.append("null_required_fields")
    if policy.reject_invalid_numeric and invalid_numeric:
        errors.append("invalid_numeric_values")
    if policy.reject_bad_ohlc and bad_ohlc:
        errors.append("bad_ohlc")
    if policy.reject_non_positive_prices and non_positive:
        errors.append("non_positive_values")
    if policy.reject_negative_values and negative:
        errors.append("negative_or_impossible_values")
    if inconsistent:
        errors.append("inconsistent_bar_intervals")
    if missing_ratio > policy.max_missing_ratio:
        errors.append("missing_ratio_exceeds_policy")

    metrics = {
        "dataset_name": batch.dataset_name,
        "dataset_version": batch.dataset_version,
        "row_count": len(rows),
        "symbols_count": len({row["symbol"] for row in rows}),
        "duplicate_keys": duplicate_keys,
        "null_required": null_required,
        "invalid_numeric": invalid_numeric,
        "bad_ohlc": bad_ohlc,
        "non_positive_prices": non_positive,
        "negative_values": negative,
        "inconsistent_intervals": inconsistent,
        "missing_bars": missing,
        "missing_ratio": missing_ratio,
        "source_object_ids": [item.object_id for item in batch.source_manifests],
        "errors": errors,
        "policy": policy.model_dump(mode="json"),
    }
    report_id = f"quality-{batch.dataset_name}-{content_sha256(metrics)[:24]}"
    return QualityReport(
        report_id=report_id,
        dataset_name=batch.dataset_name,
        dataset_version=batch.dataset_version,
        status="fail" if errors else "pass",
        row_count=len(rows),
        symbols_count=metrics["symbols_count"],  # type: ignore[arg-type]
        duplicate_keys=duplicate_keys,
        null_required=null_required,
        invalid_numeric=invalid_numeric,
        bad_ohlc=bad_ohlc,
        non_positive_prices=non_positive,
        negative_values=negative,
        inconsistent_intervals=inconsistent,
        missing_bars=missing,
        missing_ratio=missing_ratio,
        source_object_ids=tuple(item.object_id for item in batch.source_manifests),
        errors=tuple(errors),
        evaluated_at=evaluated_at,
    )
