"""Disk codec for the bounded V2 execution checkpoint."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from bfbt.config.common import as_utc
from bfbt.engine.v2 import V2ExecutionCheckpoint
from bfbt.performance.recovery import V2ChunkTransaction, V2WorkspaceError
from bfbt.portfolio.instructions import PositionCheckpoint
from bfbt.risk.state_machine import RiskCheckpoint

CHECKPOINT_CODEC_VERSION = "a30-v2-state/v2"


def _time(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_time(value: Any, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise V2WorkspaceError(f"checkpoint {field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        checked = as_utc(parsed)
    except ValueError as exc:
        raise V2WorkspaceError(
            f"checkpoint {field} must be an aware UTC datetime"
        ) from exc
    assert checked is not None
    return checked


def write_v2_execution_checkpoint(
    transaction: V2ChunkTransaction,
    checkpoint: V2ExecutionCheckpoint,
) -> None:
    """Write all scalar and tabular state into a private chunk transaction."""

    transaction.write_state_json(
        "engine",
        {
            "checkpoint_codec_version": CHECKPOINT_CODEC_VERSION,
            "run_id": checkpoint.run_id,
            "sequence": checkpoint.sequence,
            "previous_equity": checkpoint.previous_equity,
            "peak_equity": checkpoint.peak_equity,
            "warnings": list(checkpoint.warnings),
            "max_position_state_rows_observed": (
                checkpoint.max_position_state_rows_observed
            ),
            "max_risk_state_rows_observed": (
                checkpoint.max_risk_state_rows_observed
            ),
            "max_pending_risk_intents_observed": (
                checkpoint.max_pending_risk_intents_observed
            ),
            "input_trade_bar_rows": checkpoint.input_trade_bar_rows,
            "input_risk_bar_rows": checkpoint.input_risk_bar_rows,
            "last_close_marks": checkpoint.last_close_marks,
        },
    )
    transaction.write_state_json(
        "position",
        {
            "cash_balance": checkpoint.position.cash_balance,
            "sequence": checkpoint.position.sequence,
            "last_decision_time": _time(
                checkpoint.position.last_decision_time
            ),
            "rolling_margin": checkpoint.position.rolling_margin,
            "rolling_active_margin": checkpoint.position.rolling_active_margin,
            "rolling_round_net_pnl": checkpoint.position.rolling_round_net_pnl,
            "rolling_reset_count": checkpoint.position.rolling_reset_count,
            "rolling_last_reset_reason": checkpoint.position.rolling_last_reset_reason,
        },
    )
    transaction.write_state_frame(
        "position_rows", checkpoint.position.positions
    )
    transaction.write_state_json(
        "risk",
        {
            "evaluation_count": checkpoint.risk.evaluation_count,
            "sequence": checkpoint.risk.sequence,
            "last_open_time": _time(checkpoint.risk.last_open_time),
            "last_close_time": _time(checkpoint.risk.last_close_time),
            "portfolio_peak_equity": checkpoint.risk.portfolio_peak_equity,
        },
    )
    transaction.write_state_frame(
        "risk_positions", checkpoint.risk.risk_positions
    )
    transaction.write_state_frame("risk_cooldowns", checkpoint.risk.cooldowns)
    transaction.write_state_frame(
        "risk_pending", checkpoint.risk.pending_intents
    )


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V2WorkspaceError(f"cannot read V2 state file: {path}") from exc
    if not isinstance(payload, dict):
        raise V2WorkspaceError(f"V2 state file must contain an object: {path}")
    return payload


def _frame(path: Path) -> pl.DataFrame:
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise V2WorkspaceError(f"cannot read V2 state table: {path}") from exc


def read_v2_execution_checkpoint(chunk_directory: Path) -> V2ExecutionCheckpoint:
    """Restore state only after A19 has validated the committed directory."""

    state = chunk_directory / "state"
    engine = _json(state / "engine.json")
    position = _json(state / "position.json")
    risk = _json(state / "risk.json")
    if engine.get("checkpoint_codec_version") != CHECKPOINT_CODEC_VERSION:
        raise V2WorkspaceError("unsupported V2 checkpoint codec version")
    warnings = engine.get("warnings")
    if not isinstance(warnings, list) or any(
        not isinstance(item, str) for item in warnings
    ):
        raise V2WorkspaceError("checkpoint warnings must be a string list")
    last_close_marks = engine.get("last_close_marks")
    if not isinstance(last_close_marks, dict):
        raise V2WorkspaceError("checkpoint last_close_marks must be an object")
    try:
        return V2ExecutionCheckpoint(
            run_id=str(engine["run_id"]),
            position=PositionCheckpoint(
                cash_balance=float(position["cash_balance"]),
                sequence=int(position["sequence"]),
                last_decision_time=_parse_time(
                    position.get("last_decision_time"),
                    field="position.last_decision_time",
                ),
                positions=_frame(state / "position_rows.parquet"),
                rolling_margin=(
                    None
                    if position.get("rolling_margin") is None
                    else float(position["rolling_margin"])
                ),
                rolling_active_margin=(
                    None
                    if position.get("rolling_active_margin") is None
                    else float(position["rolling_active_margin"])
                ),
                rolling_round_net_pnl=float(
                    position.get("rolling_round_net_pnl", 0.0)
                ),
                rolling_reset_count=int(position.get("rolling_reset_count", 0)),
                rolling_last_reset_reason=(
                    None
                    if position.get("rolling_last_reset_reason") is None
                    else str(position["rolling_last_reset_reason"])
                ),
            ),
            risk=RiskCheckpoint(
                evaluation_count=int(risk["evaluation_count"]),
                sequence=int(risk["sequence"]),
                last_open_time=_parse_time(
                    risk.get("last_open_time"), field="risk.last_open_time"
                ),
                last_close_time=_parse_time(
                    risk.get("last_close_time"), field="risk.last_close_time"
                ),
                portfolio_peak_equity=float(risk["portfolio_peak_equity"]),
                risk_positions=_frame(state / "risk_positions.parquet"),
                cooldowns=_frame(state / "risk_cooldowns.parquet"),
                pending_intents=_frame(state / "risk_pending.parquet"),
            ),
            sequence=int(engine["sequence"]),
            previous_equity=float(engine["previous_equity"]),
            peak_equity=float(engine["peak_equity"]),
            warnings=tuple(sorted(set(warnings))),
            max_position_state_rows_observed=int(
                engine["max_position_state_rows_observed"]
            ),
            max_risk_state_rows_observed=int(
                engine["max_risk_state_rows_observed"]
            ),
            max_pending_risk_intents_observed=int(
                engine["max_pending_risk_intents_observed"]
            ),
            input_trade_bar_rows=int(engine["input_trade_bar_rows"]),
            input_risk_bar_rows=int(engine["input_risk_bar_rows"]),
            last_close_marks={
                str(symbol): float(mark)
                for symbol, mark in last_close_marks.items()
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V2WorkspaceError("V2 checkpoint scalar values are invalid") from exc
