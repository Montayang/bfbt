"""Point-in-time contract snapshot projection."""

from __future__ import annotations

import polars as pl


class ContractHistoryError(ValueError):
    """Contract snapshots cannot support an unambiguous as-of join."""


CONTRACT_HISTORY_COLUMNS = (
    "snapshot_time",
    "symbol",
    "contract_type",
    "status",
    "quote_asset",
    "margin_asset",
    "onboard_time",
    "delivery_time",
    "dataset_version",
)


def prepare_contract_history(frame: pl.LazyFrame) -> pl.LazyFrame:
    """Project and sort the fields required by a backward as-of join."""

    missing = set(CONTRACT_HISTORY_COLUMNS) - set(frame.collect_schema().names())
    if missing:
        raise ContractHistoryError(
            f"contract input is missing columns: {sorted(missing)}"
        )
    timestamp_type = pl.Datetime("ms", "UTC")
    return (
        frame.select(CONTRACT_HISTORY_COLUMNS)
        .with_columns(
            pl.col("snapshot_time").cast(timestamp_type),
            pl.col("onboard_time").cast(timestamp_type),
            pl.col("delivery_time").cast(timestamp_type),
        )
        .sort(["symbol", "snapshot_time"])
    )
