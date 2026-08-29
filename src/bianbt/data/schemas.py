"""Versioned Arrow schemas for normalized Binance futures datasets."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from bianbt.data.hashing import content_sha256

SCHEMA_METADATA_PREFIX = "bianbt."
UTC_MILLISECONDS = pa.timestamp("ms", tz="UTC")


class UnknownSchemaError(KeyError):
    """Requested dataset/schema version is not registered."""


def _field(
    name: str,
    data_type: pa.DataType,
    *,
    nullable: bool,
    description: str,
    unit: str | None = None,
) -> pa.Field:
    metadata = {b"description": description.encode("utf-8")}
    if unit is not None:
        metadata[b"unit"] = unit.encode("utf-8")
    return pa.field(name, data_type, nullable=nullable, metadata=metadata)


def _schema(
    dataset: str,
    version: str,
    fields: list[pa.Field],
    *,
    primary_key: tuple[str, ...],
    sort_key: tuple[str, ...],
    description: str,
) -> pa.Schema:
    metadata = {
        b"bianbt.dataset": dataset.encode("utf-8"),
        b"bianbt.schema_version": version.encode("utf-8"),
        b"bianbt.primary_key": json.dumps(primary_key).encode("utf-8"),
        b"bianbt.sort_key": json.dumps(sort_key).encode("utf-8"),
        b"bianbt.timezone": b"UTC",
        b"bianbt.description": description.encode("utf-8"),
    }
    return pa.schema(fields, metadata=metadata)


@dataclass(frozen=True)
class SchemaDefinition:
    dataset: str
    version: str
    primary_key: tuple[str, ...]
    sort_key: tuple[str, ...]
    schema: pa.Schema

    def descriptor(self) -> dict[str, Any]:
        """Return a JSON-safe logical descriptor independent of IPC encoding."""

        fields = []
        for field in self.schema:
            field_metadata = {
                key.decode("utf-8"): value.decode("utf-8")
                for key, value in sorted((field.metadata or {}).items())
            }
            fields.append(
                {
                    "name": field.name,
                    "type": str(field.type),
                    "nullable": field.nullable,
                    "metadata": field_metadata,
                }
            )
        schema_metadata = {
            key.decode("utf-8"): value.decode("utf-8")
            for key, value in sorted((self.schema.metadata or {}).items())
        }
        return {
            "dataset": self.dataset,
            "version": self.version,
            "primary_key": list(self.primary_key),
            "sort_key": list(self.sort_key),
            "fields": fields,
            "metadata": schema_metadata,
        }

    @property
    def fingerprint(self) -> str:
        """Return the stable logical schema fingerprint."""

        return content_sha256(self.descriptor())


BARS_V1 = _schema(
    "bars",
    "v1",
    [
        _field("open_time", UTC_MILLISECONDS, nullable=False, description="Bar left boundary"),
        _field("close_time", UTC_MILLISECONDS, nullable=False, description="Bar availability boundary"),
        _field("symbol", pa.string(), nullable=False, description="Binance contract symbol"),
        _field("interval", pa.string(), nullable=False, description="Logical bar interval"),
        _field("open", pa.float64(), nullable=False, description="Opening trade price", unit="quote/base"),
        _field("high", pa.float64(), nullable=False, description="Highest trade price", unit="quote/base"),
        _field("low", pa.float64(), nullable=False, description="Lowest trade price", unit="quote/base"),
        _field("close", pa.float64(), nullable=False, description="Closing trade price", unit="quote/base"),
        _field("volume", pa.float64(), nullable=False, description="Base asset volume", unit="base"),
        _field("quote_volume", pa.float64(), nullable=False, description="Quote asset volume", unit="quote"),
        _field("trades", pa.int64(), nullable=False, description="Trade count"),
        _field("taker_buy_volume", pa.float64(), nullable=False, description="Taker buy base volume", unit="base"),
        _field("taker_buy_quote_volume", pa.float64(), nullable=False, description="Taker buy quote volume", unit="quote"),
        _field("is_complete", pa.bool_(), nullable=False, description="Whether the source bar is final"),
        _field("source", pa.string(), nullable=False, description="Archive or REST source"),
        _field("source_object_id", pa.string(), nullable=False, description="Raw object manifest reference"),
        _field("dataset_version", pa.string(), nullable=False, description="Immutable dataset content version"),
    ],
    primary_key=("open_time", "symbol", "interval"),
    sort_key=("open_time", "symbol"),
    description="Normalized USD-M perpetual trade bars",
)

MARK_BARS_V1 = _schema(
    "mark_bars",
    "v1",
    [
        _field("open_time", UTC_MILLISECONDS, nullable=False, description="Bar left boundary"),
        _field("close_time", UTC_MILLISECONDS, nullable=False, description="Bar availability boundary"),
        _field("symbol", pa.string(), nullable=False, description="Binance contract symbol"),
        _field("interval", pa.string(), nullable=False, description="Logical bar interval"),
        _field("open", pa.float64(), nullable=False, description="Opening mark price"),
        _field("high", pa.float64(), nullable=False, description="Highest mark price"),
        _field("low", pa.float64(), nullable=False, description="Lowest mark price"),
        _field("close", pa.float64(), nullable=False, description="Closing mark price"),
        _field("is_complete", pa.bool_(), nullable=False, description="Whether the source bar is final"),
        _field("source", pa.string(), nullable=False, description="Archive or REST source"),
        _field("source_object_id", pa.string(), nullable=False, description="Raw object manifest reference"),
        _field("dataset_version", pa.string(), nullable=False, description="Immutable dataset content version"),
    ],
    primary_key=("open_time", "symbol", "interval"),
    sort_key=("open_time", "symbol"),
    description="Normalized USD-M perpetual mark-price bars",
)

FUNDING_V1 = _schema(
    "funding",
    "v1",
    [
        _field("funding_time", UTC_MILLISECONDS, nullable=False, description="Actual funding settlement time"),
        _field("symbol", pa.string(), nullable=False, description="Binance contract symbol"),
        _field("funding_rate", pa.float64(), nullable=False, description="Positive means longs pay shorts"),
        _field("mark_price", pa.float64(), nullable=True, description="Settlement-related mark price"),
        _field("funding_interval_hours", pa.float64(), nullable=True, description="Known funding interval", unit="hours"),
        _field("source_object_id", pa.string(), nullable=False, description="Raw object manifest reference"),
        _field("dataset_version", pa.string(), nullable=False, description="Immutable dataset content version"),
    ],
    primary_key=("funding_time", "symbol"),
    sort_key=("funding_time", "symbol"),
    description="Observed USD-M perpetual funding settlements",
)

CONTRACTS_V1 = _schema(
    "contracts",
    "v1",
    [
        _field("snapshot_time", UTC_MILLISECONDS, nullable=False, description="Metadata observation time"),
        _field("symbol", pa.string(), nullable=False, description="Binance contract symbol"),
        _field("contract_type", pa.string(), nullable=False, description="Contract type at snapshot"),
        _field("status", pa.string(), nullable=False, description="Trading status at snapshot"),
        _field("base_asset", pa.string(), nullable=False, description="Base asset"),
        _field("quote_asset", pa.string(), nullable=False, description="Quote asset"),
        _field("margin_asset", pa.string(), nullable=False, description="Margin asset"),
        _field("onboard_time", UTC_MILLISECONDS, nullable=True, description="Official onboard time"),
        _field("delivery_time", UTC_MILLISECONDS, nullable=True, description="Official delivery time"),
        _field("price_tick", pa.float64(), nullable=False, description="PRICE_FILTER tick size"),
        _field("quantity_step", pa.float64(), nullable=False, description="LOT_SIZE step size"),
        _field("min_quantity", pa.float64(), nullable=True, description="Minimum order quantity"),
        _field("min_notional", pa.float64(), nullable=True, description="Minimum order notional"),
        _field("observed_first_bar", UTC_MILLISECONDS, nullable=True, description="First locally observed bar"),
        _field("observed_last_bar", UTC_MILLISECONDS, nullable=True, description="Last locally observed bar"),
        _field("source_object_id", pa.string(), nullable=False, description="Raw object manifest reference"),
        _field("dataset_version", pa.string(), nullable=False, description="Immutable dataset content version"),
    ],
    primary_key=("snapshot_time", "symbol"),
    sort_key=("snapshot_time", "symbol"),
    description="Point-in-time USD-M perpetual contract metadata",
)


RANKINGS_V1 = _schema(
    "rankings",
    "v1",
    [
        _field(
            "timestamp",
            UTC_MILLISECONDS,
            nullable=False,
            description="Rank snapshot decision-clock timestamp",
        ),
        _field(
            "rank_clock",
            pa.string(),
            nullable=False,
            description="factor or rebalance snapshot clock",
        ),
        _field("symbol", pa.string(), nullable=False, description="Contract symbol"),
        _field("factor_name", pa.string(), nullable=False, description="Factor name"),
        _field("raw_score", pa.float64(), nullable=False, description="Unranked factor score"),
        _field(
            "ordinal_rank",
            pa.int32(),
            nullable=False,
            description="One is the highest score after deterministic tie-break",
        ),
        _field(
            "percentile_rank",
            pa.float64(),
            nullable=False,
            description="Cross-sectional percentile in [0, 1], higher is better",
        ),
        _field(
            "sample_count",
            pa.int32(),
            nullable=False,
            description="Eligible scored contracts in this snapshot",
        ),
        _field("factor_version", pa.string(), nullable=False, description="Factor implementation version"),
        _field("universe_version", pa.string(), nullable=False, description="Point-in-time universe version"),
        _field("run_id", pa.string(), nullable=False, description="Owning formal run"),
    ],
    primary_key=("timestamp", "factor_name", "symbol"),
    sort_key=("timestamp", "factor_name", "ordinal_rank", "symbol"),
    description="V2 deterministic cross-sectional rank snapshots",
)

POSITION_INSTRUCTIONS_V1 = _schema(
    "position_instructions",
    "v1",
    [
        _field("instruction_id", pa.string(), nullable=False, description="Stable instruction identity"),
        _field("decision_time", UTC_MILLISECONDS, nullable=False, description="Time the strategy or risk rule decided"),
        _field("rank_source_time", UTC_MILLISECONDS, nullable=True, description="Rank snapshot used by this decision"),
        _field("symbol", pa.string(), nullable=False, description="Contract symbol"),
        _field("side", pa.string(), nullable=False, description="LONG, SHORT, or FLAT"),
        _field("instruction_mode", pa.string(), nullable=False, description="Target or incremental sizing mode"),
        _field("requested_delta_notional", pa.float64(), nullable=True, description="Signed notional delta before constraints"),
        _field("constrained_delta_notional", pa.float64(), nullable=True, description="Signed notional delta after constraints"),
        _field("requested_target_weight", pa.float64(), nullable=True, description="Requested signed target weight"),
        _field("source_event_id", pa.string(), nullable=True, description="Optional originating risk event"),
        _field("reason_code", pa.string(), nullable=False, description="Stable selection, constraint, or suppression reason"),
        _field("priority", pa.int16(), nullable=False, description="Lower numeric value has higher event priority"),
        _field("run_id", pa.string(), nullable=False, description="Owning formal run"),
    ],
    primary_key=("instruction_id",),
    sort_key=("decision_time", "priority", "symbol", "instruction_id"),
    description="V2 requested and constrained position instructions",
)

RISK_EVENTS_V1 = _schema(
    "risk_events",
    "v1",
    [
        _field("event_id", pa.string(), nullable=False, description="Stable risk event identity"),
        _field("evaluation_time", UTC_MILLISECONDS, nullable=False, description="Risk clock evaluation time"),
        _field("trigger_time", UTC_MILLISECONDS, nullable=False, description="Bar time at which the rule triggered"),
        _field("symbol", pa.string(), nullable=True, description="Contract symbol; null for portfolio events"),
        _field("event_type", pa.string(), nullable=False, description="Stop, take-profit, trailing, or portfolio event"),
        _field("direction", pa.string(), nullable=True, description="LONG or SHORT position direction"),
        _field("entry_price", pa.float64(), nullable=True, description="Average entry reference price"),
        _field("trigger_level", pa.float64(), nullable=False, description="Configured price or equity trigger level"),
        _field("observed_price", pa.float64(), nullable=False, description="Observed price or equity that crossed the level"),
        _field("conflict_policy", pa.string(), nullable=False, description="OHLC ambiguity policy"),
        _field("action", pa.string(), nullable=False, description="Close or reduce action"),
        _field("fill_time", UTC_MILLISECONDS, nullable=True, description="Actual fill time when available"),
        _field("reason_code", pa.string(), nullable=False, description="Stable risk outcome reason"),
        _field("run_id", pa.string(), nullable=False, description="Owning formal run"),
    ],
    primary_key=("event_id",),
    sort_key=("evaluation_time", "symbol", "event_id"),
    description="V2 symbol and portfolio risk trigger events",
)


def _definition(dataset: str, version: str, schema: pa.Schema) -> SchemaDefinition:
    metadata = schema.metadata or {}
    return SchemaDefinition(
        dataset=dataset,
        version=version,
        primary_key=tuple(json.loads(metadata[b"bianbt.primary_key"])),
        sort_key=tuple(json.loads(metadata[b"bianbt.sort_key"])),
        schema=schema,
    )


_REGISTRY = {
    ("bars", "v1"): _definition("bars", "v1", BARS_V1),
    ("mark_bars", "v1"): _definition("mark_bars", "v1", MARK_BARS_V1),
    ("funding", "v1"): _definition("funding", "v1", FUNDING_V1),
    ("contracts", "v1"): _definition("contracts", "v1", CONTRACTS_V1),
}

_ARTIFACT_REGISTRY = {
    ("rankings", "v1"): _definition("rankings", "v1", RANKINGS_V1),
    ("position_instructions", "v1"): _definition(
        "position_instructions", "v1", POSITION_INSTRUCTIONS_V1
    ),
    ("risk_events", "v1"): _definition("risk_events", "v1", RISK_EVENTS_V1),
}


def list_schema_definitions() -> tuple[SchemaDefinition, ...]:
    """Return V1 market-data schemas; retained as the compatibility registry."""

    return tuple(_REGISTRY[key] for key in sorted(_REGISTRY))


def list_artifact_schema_definitions() -> tuple[SchemaDefinition, ...]:
    """Return formal run-artifact schemas sorted by table and version."""

    return tuple(_ARTIFACT_REGISTRY[key] for key in sorted(_ARTIFACT_REGISTRY))


def get_schema_definition(dataset: str, version: str) -> SchemaDefinition:
    """Resolve one exact schema; floating versions are never accepted."""

    try:
        return {**_REGISTRY, **_ARTIFACT_REGISTRY}[(dataset, version)]
    except KeyError as exc:
        available = ", ".join(
            f"{name}/{item_version}"
            for name, item_version in sorted({**_REGISTRY, **_ARTIFACT_REGISTRY})
        )
        raise UnknownSchemaError(
            f"unknown schema {dataset}/{version}; available: {available}"
        ) from exc


def validate_arrow_schema(
    actual: pa.Schema,
    *,
    dataset: str,
    version: str,
    check_metadata: bool = True,
) -> None:
    """Raise when an Arrow schema differs from the registered contract."""

    expected = get_schema_definition(dataset, version).schema
    if not actual.equals(expected, check_metadata=check_metadata):
        raise ValueError(f"Arrow schema does not match {dataset}/{version}")
