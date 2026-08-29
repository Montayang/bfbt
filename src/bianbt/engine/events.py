"""Deterministic A17 arbitration and trace links for V2 execution intents."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Iterable

import polars as pl

from bianbt.data.hashing import content_sha256
from bianbt.data.v2_contracts import EventPriority, V2ReasonCode

EVENT_ARBITRATOR_VERSION = "a17-events-v1"
UTC_MS = pl.Datetime("ms", "UTC")
EPSILON = 1e-10

INTENT_SCHEMA = {
    "instruction_id": pl.String,
    "decision_time": UTC_MS,
    "fill_time": UTC_MS,
    "rank_source_time": UTC_MS,
    "symbol": pl.String,
    "side": pl.String,
    "instruction_mode": pl.String,
    "requested_delta_notional": pl.Float64,
    "constrained_delta_notional": pl.Float64,
    "requested_target_weight": pl.Float64,
    "source_event_id": pl.String,
    "reason_code": pl.String,
    "priority": pl.Int16,
    "run_id": pl.String,
}
INSTRUCTION_ARTIFACT_SCHEMA = {
    key: dtype for key, dtype in INTENT_SCHEMA.items() if key != "fill_time"
}
ARBITRATION_TRACE_SCHEMA = {
    "fill_time": UTC_MS,
    "symbol": pl.String,
    "instruction_id": pl.String,
    "outcome": pl.String,
    "winner_instruction_id": pl.String,
    "original_reason_code": pl.String,
    "final_reason_code": pl.String,
    "priority": pl.Int16,
}


class EventArbitrationError(ValueError):
    """Intents or fills cannot be reconciled without ambiguity."""


@dataclass(frozen=True)
class ArbitrationBatch:
    """Formal instructions plus bounded diagnostics for one arbitration call."""

    instructions: pl.DataFrame
    accepted_intents: pl.DataFrame
    trace: pl.DataFrame
    input_count: int
    accepted_count: int
    suppressed_count: int


@dataclass(frozen=True)
class TradeLinkBatch:
    """Trades carrying stable instruction/risk references."""

    trades: pl.DataFrame
    linked_instruction_count: int
    linked_risk_event_count: int


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _frame(
    rows: list[dict[str, object]], schema: dict[str, pl.DataType]
) -> pl.DataFrame:
    if not rows:
        return _empty(schema)
    return pl.DataFrame(rows).select(list(schema)).cast(schema)


def _materialize(value: pl.DataFrame | pl.LazyFrame) -> pl.DataFrame:
    return value.collect() if isinstance(value, pl.LazyFrame) else value


class EventArbitrator:
    """Resolve intents sharing one actual fill clock and symbol.

    Lower numeric priority wins. Equal-priority conflicts use stable instruction
    ID ordering. Losing requests remain auditable with zero constrained delta.
    """

    def __init__(self, *, run_id: str, max_pending_intents: int = 20_000) -> None:
        if not run_id or run_id.lower() == "latest":
            raise EventArbitrationError("run_id must be explicit")
        if max_pending_intents < 1:
            raise EventArbitrationError("max_pending_intents must be positive")
        self.run_id = run_id
        self.max_pending_intents = max_pending_intents
        identity = {
            "engine": EVENT_ARBITRATOR_VERSION,
            "run_id": run_id,
            "max_pending_intents": max_pending_intents,
        }
        self.arbitrator_version = f"a17-{content_sha256(identity)[:24]}"

    def normalize(
        self,
        intents: pl.DataFrame | pl.LazyFrame,
        *,
        enforce_pending_limit: bool = True,
    ) -> pl.DataFrame:
        frame = _materialize(intents)
        missing = set(INTENT_SCHEMA) - set(frame.columns)
        if missing:
            raise EventArbitrationError(
                f"intent input is missing columns: {sorted(missing)}"
            )
        if enforce_pending_limit and frame.height > self.max_pending_intents:
            raise EventArbitrationError(
                f"pending intents {frame.height} exceed "
                f"max_pending_intents={self.max_pending_intents}"
            )
        try:
            normalized = frame.select(list(INTENT_SCHEMA)).cast(INTENT_SCHEMA)
        except pl.exceptions.PolarsError as exc:
            raise EventArbitrationError(f"intent types are invalid: {exc}") from exc
        if normalized["instruction_id"].n_unique() != normalized.height:
            raise EventArbitrationError("instruction_id must be globally unique")
        allowed_priorities = {int(item) for item in EventPriority}
        allowed_reasons = {item.value for item in V2ReasonCode}
        for row in normalized.iter_rows(named=True):
            instruction_id = str(row["instruction_id"])
            if not instruction_id or instruction_id.lower() == "latest":
                raise EventArbitrationError("instruction_id must be explicit")
            if row["decision_time"] is None or row["fill_time"] is None:
                raise EventArbitrationError(
                    "decision_time and fill_time cannot be null"
                )
            if row["fill_time"] < row["decision_time"]:
                raise EventArbitrationError("fill_time cannot precede decision_time")
            if str(row["side"]) not in {"LONG", "SHORT", "FLAT"}:
                raise EventArbitrationError("side must be LONG, SHORT, or FLAT")
            if int(row["priority"]) not in allowed_priorities:
                raise EventArbitrationError(
                    f"unknown event priority: {row['priority']}"
                )
            if str(row["reason_code"]) not in allowed_reasons:
                raise EventArbitrationError(
                    f"unknown reason_code: {row['reason_code']}"
                )
            for value in (
                row["requested_delta_notional"],
                row["constrained_delta_notional"],
            ):
                if value is not None and not isfinite(float(value)):
                    raise EventArbitrationError("intent notionals must be finite")
            if str(row["run_id"]) != self.run_id:
                raise EventArbitrationError("all intents must belong to run_id")
        return normalized.sort(
            ["fill_time", "symbol", "priority", "instruction_id"]
        )

    def arbitrate(
        self,
        intents: pl.DataFrame | pl.LazyFrame,
        *,
        cooldown_symbols: Iterable[str] = (),
    ) -> ArbitrationBatch:
        normalized = self.normalize(intents)
        cooldown = {str(symbol) for symbol in cooldown_symbols}
        output_rows: list[dict[str, object]] = []
        accepted_rows: list[dict[str, object]] = []
        trace_rows: list[dict[str, object]] = []
        for group in normalized.partition_by(
            ["fill_time", "symbol"], maintain_order=True
        ):
            rows = group.to_dicts()
            eligible = [
                row
                for row in rows
                if not (
                    str(row["symbol"]) in cooldown
                    and int(row["priority"])
                    == int(EventPriority.SCHEDULED_STRATEGY)
                )
            ]
            winner = eligible[0] if eligible else None
            winner_id = str(winner["instruction_id"]) if winner else None
            for row in rows:
                original_reason = str(row["reason_code"])
                is_cooldown = (
                    str(row["symbol"]) in cooldown
                    and int(row["priority"])
                    == int(EventPriority.SCHEDULED_STRATEGY)
                )
                accepted = winner is not None and row["instruction_id"] == winner_id
                if is_cooldown:
                    final_reason = V2ReasonCode.COOLDOWN_ACTIVE.value
                elif accepted:
                    final_reason = original_reason
                else:
                    final_reason = (
                        V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value
                    )
                final = dict(row)
                final["run_id"] = self.run_id
                final["reason_code"] = final_reason
                if not accepted:
                    final["constrained_delta_notional"] = 0.0
                    final["requested_target_weight"] = None
                output_rows.append(
                    {
                        key: value
                        for key, value in final.items()
                        if key != "fill_time"
                    }
                )
                if accepted:
                    accepted_rows.append(final)
                trace_rows.append(
                    {
                        "fill_time": row["fill_time"],
                        "symbol": row["symbol"],
                        "instruction_id": row["instruction_id"],
                        "outcome": "ACCEPTED" if accepted else "SUPPRESSED",
                        "winner_instruction_id": winner_id,
                        "original_reason_code": original_reason,
                        "final_reason_code": final_reason,
                        "priority": row["priority"],
                    }
                )
        instructions = _frame(
            output_rows, INSTRUCTION_ARTIFACT_SCHEMA
        ).sort(["decision_time", "priority", "symbol", "instruction_id"])
        accepted_intents = _frame(accepted_rows, INTENT_SCHEMA).sort(
            ["fill_time", "symbol", "priority", "instruction_id"]
        )
        trace = _frame(trace_rows, ARBITRATION_TRACE_SCHEMA).sort(
            ["fill_time", "symbol", "priority", "instruction_id"]
        )
        accepted_count = accepted_intents.height
        return ArbitrationBatch(
            instructions=instructions,
            accepted_intents=accepted_intents,
            trace=trace,
            input_count=normalized.height,
            accepted_count=accepted_count,
            suppressed_count=normalized.height - accepted_count,
        )

    def link_trades(
        self,
        trades: pl.DataFrame | pl.LazyFrame,
        accepted_intents: pl.DataFrame | pl.LazyFrame,
    ) -> TradeLinkBatch:
        trade_frame = _materialize(trades)
        required = {"fill_time", "symbol", "status", "run_id"}
        missing = required - set(trade_frame.columns)
        if missing:
            raise EventArbitrationError(
                f"trade input is missing columns: {sorted(missing)}"
            )
        # Accepted intents are historical rows accumulated across the whole
        # chunk, not simultaneously pending state. The pending-state bound is
        # enforced on each arbitration call before rows become accepted.
        intents = self.normalize(
            accepted_intents, enforce_pending_limit=False
        ).filter(
            pl.col("constrained_delta_notional").fill_null(0.0).abs() > EPSILON
        )
        keys = ["fill_time", "symbol"]
        if intents.group_by(keys).len().filter(pl.col("len") > 1).height:
            raise EventArbitrationError(
                "accepted intents contain ambiguous fill_time/symbol keys"
            )
        filled = trade_frame.filter(pl.col("status") == "FILLED")
        if filled.group_by(keys).len().filter(pl.col("len") > 1).height:
            raise EventArbitrationError(
                "partial or duplicate trades are outside the A17 fill contract"
            )
        links = intents.select(
            *keys,
            "instruction_id",
            "source_event_id",
            "priority",
            pl.col("reason_code").alias("instruction_reason_code"),
        )
        linked = trade_frame.join(links, on=keys, how="left")
        if linked.filter(
            (pl.col("status") == "FILLED")
            & pl.col("instruction_id").is_null()
        ).height:
            raise EventArbitrationError(
                "every filled trade must link to one accepted instruction"
            )
        if intents.join(filled.select(keys), on=keys, how="anti").height:
            raise EventArbitrationError(
                "every accepted non-zero instruction must link to one filled trade"
            )
        linked = linked.with_columns(pl.lit(self.run_id).alias("run_id"))
        return TradeLinkBatch(
            trades=linked,
            linked_instruction_count=filled.height,
            linked_risk_event_count=linked.filter(
                pl.col("source_event_id").is_not_null()
            ).height,
        )


def link_risk_event_fills(
    risk_events: pl.DataFrame | pl.LazyFrame,
    linked_trades: pl.DataFrame | pl.LazyFrame,
    *,
    run_id: str,
    position_instructions: pl.DataFrame | pl.LazyFrame | None = None,
) -> pl.DataFrame:
    """Resolve each risk event to one fill, suppression, or end-of-data outcome."""

    events = _materialize(risk_events)
    trades = _materialize(linked_trades)
    required_events = {"event_id", "fill_time", "reason_code", "run_id"}
    required_trades = {"fill_time", "source_event_id", "status"}
    if required_events - set(events.columns):
        raise EventArbitrationError("risk event input is missing link columns")
    if required_trades - set(trades.columns):
        raise EventArbitrationError(
            "linked trade input is missing risk references"
        )
    raw_links = trades.filter(
        (pl.col("status") == "FILLED")
        & pl.col("source_event_id").is_not_null()
    ).select(
        pl.col("source_event_id").alias("event_id"),
        pl.col("fill_time").alias("linked_fill_time"),
    )
    links = raw_links.group_by("event_id").agg(
        pl.col("linked_fill_time").n_unique().alias("_fill_time_count"),
        pl.col("linked_fill_time").first(),
    )
    if links.filter(pl.col("_fill_time_count") > 1).height:
        raise EventArbitrationError(
            "one risk event cannot link to different fill times"
        )
    linked = events.join(
        links.drop("_fill_time_count"), on="event_id", how="left"
    )
    if position_instructions is not None:
        instructions = _materialize(position_instructions)
        required = {"source_event_id", "reason_code"}
        if required - set(instructions.columns):
            raise EventArbitrationError(
                "position instructions are missing risk outcome columns"
            )
        raw_outcomes = instructions.filter(
            pl.col("source_event_id").is_not_null()
        ).select(
            pl.col("source_event_id").alias("event_id"),
            pl.col("reason_code").alias("instruction_reason_code"),
        )
        outcomes = raw_outcomes.group_by("event_id").agg(
            pl.col("instruction_reason_code").n_unique().alias("_reason_count"),
            pl.col("instruction_reason_code").first(),
        )
        if outcomes.filter(pl.col("_reason_count") > 1).height:
            raise EventArbitrationError(
                "one risk event cannot have inconsistent instruction outcomes"
            )
        linked = linked.join(
            outcomes.drop("_reason_count"), on="event_id", how="left"
        )
        linked = linked.with_columns(
            pl.when(
                pl.col("instruction_reason_code").is_in(
                    [
                        V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value,
                        V2ReasonCode.COOLDOWN_ACTIVE.value,
                    ]
                )
            )
            .then(pl.col("instruction_reason_code"))
            .otherwise(pl.col("reason_code"))
            .alias("reason_code")
        ).drop("instruction_reason_code")

    linked = (
        linked.with_columns(
            pl.coalesce("linked_fill_time", "fill_time").alias("fill_time"),
            pl.lit(run_id).alias("run_id"),
        )
        .drop("linked_fill_time")
    )
    terminal_without_fill = [
        V2ReasonCode.END_OF_DATA_UNFILLED.value,
        V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value,
        V2ReasonCode.COOLDOWN_ACTIVE.value,
    ]
    if linked.filter(
        pl.col("fill_time").is_null()
        & ~pl.col("reason_code").is_in(terminal_without_fill)
    ).height:
        raise EventArbitrationError(
            "triggered risk events must link to a fill, suppression, "
            "or END_OF_DATA_UNFILLED"
        )
    return linked


def link_risk_event_fills_lazy(
    risk_events: pl.LazyFrame,
    linked_trades: pl.LazyFrame,
    *,
    run_id: str,
    position_instructions: pl.LazyFrame | None = None,
) -> pl.LazyFrame:
    """Resolve risk outcomes without collecting the audit ledgers."""

    required_events = {"event_id", "fill_time", "reason_code", "run_id"}
    required_trades = {"fill_time", "source_event_id", "status"}
    if required_events - set(risk_events.collect_schema().names()):
        raise EventArbitrationError("risk event input is missing link columns")
    if required_trades - set(linked_trades.collect_schema().names()):
        raise EventArbitrationError(
            "linked trade input is missing risk references"
        )
    raw_links = linked_trades.filter(
        (pl.col("status") == "FILLED")
        & pl.col("source_event_id").is_not_null()
    ).select(
        pl.col("source_event_id").alias("event_id"),
        pl.col("fill_time").alias("linked_fill_time"),
    )
    links = raw_links.group_by("event_id").agg(
        pl.col("linked_fill_time").n_unique().alias("_fill_time_count"),
        pl.col("linked_fill_time").first(),
    )
    ambiguous = links.filter(pl.col("_fill_time_count") > 1).select(
        pl.len()
    ).collect(engine="streaming").item()
    if ambiguous:
        raise EventArbitrationError(
            "one risk event cannot link to different fill times"
        )
    linked = risk_events.join(
        links.drop("_fill_time_count"), on="event_id", how="left"
    )
    if position_instructions is not None:
        required = {"source_event_id", "reason_code"}
        if required - set(position_instructions.collect_schema().names()):
            raise EventArbitrationError(
                "position instructions are missing risk outcome columns"
            )
        raw_outcomes = position_instructions.filter(
            pl.col("source_event_id").is_not_null()
        ).select(
            pl.col("source_event_id").alias("event_id"),
            pl.col("reason_code").alias("instruction_reason_code"),
        )
        outcomes = raw_outcomes.group_by("event_id").agg(
            pl.col("instruction_reason_code").n_unique().alias("_reason_count"),
            pl.col("instruction_reason_code").first(),
        )
        inconsistent = outcomes.filter(pl.col("_reason_count") > 1).select(
            pl.len()
        ).collect(engine="streaming").item()
        if inconsistent:
            raise EventArbitrationError(
                "one risk event cannot have inconsistent instruction outcomes"
            )
        linked = linked.join(
            outcomes.drop("_reason_count"), on="event_id", how="left"
        ).with_columns(
            pl.when(
                pl.col("instruction_reason_code").is_in(
                    [
                        V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value,
                        V2ReasonCode.COOLDOWN_ACTIVE.value,
                    ]
                )
            )
            .then(pl.col("instruction_reason_code"))
            .otherwise(pl.col("reason_code"))
            .alias("reason_code")
        ).drop("instruction_reason_code")
    linked = linked.with_columns(
        pl.coalesce("linked_fill_time", "fill_time").alias("fill_time"),
        pl.lit(run_id).alias("run_id"),
    ).drop("linked_fill_time")
    terminal_without_fill = [
        V2ReasonCode.END_OF_DATA_UNFILLED.value,
        V2ReasonCode.SUPPRESSED_BY_HIGHER_PRIORITY.value,
        V2ReasonCode.COOLDOWN_ACTIVE.value,
    ]
    unresolved = linked.filter(
        pl.col("fill_time").is_null()
        & ~pl.col("reason_code").is_in(terminal_without_fill)
    ).select(pl.len()).collect(engine="streaming").item()
    if unresolved:
        raise EventArbitrationError(
            "triggered risk events must link to a fill, suppression, "
            "or END_OF_DATA_UNFILLED"
        )
    return linked
