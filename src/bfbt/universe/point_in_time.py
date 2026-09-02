"""Lazy point-in-time universe construction without future metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from bfbt.config.common import as_utc
from bfbt.config.durations import duration_seconds, is_integer_multiple
from bfbt.config.universe import UniverseConfig
from bfbt.data.hashing import content_sha256
from bfbt.universe.contracts import prepare_contract_history
from bfbt.universe.filters import UniverseReason

UNIVERSE_CODE_VERSION = "a06-universe-v1"


class UniverseBuildError(ValueError):
    """A point-in-time universe cannot be built from the supplied history."""


@dataclass(frozen=True)
class UniverseBuildResult:
    frame: pl.LazyFrame
    universe_version: str
    bars_dataset_version: str
    contracts_dataset_version: str
    base_interval: str


def build_schedule(*, start: datetime, end: datetime, interval: str) -> pl.LazyFrame:
    """Build an epoch-aligned UTC schedule with left-closed/right-open bounds."""
    checked_start, checked_end = as_utc(start), as_utc(end)
    assert checked_start is not None and checked_end is not None
    if checked_end <= checked_start:
        raise UniverseBuildError("schedule end must be greater than start")
    seconds = duration_seconds(interval)
    if checked_start.microsecond or int(checked_start.timestamp()) % seconds:
        raise UniverseBuildError("schedule start must align to its UTC interval")
    values, cursor = [], checked_start
    while cursor < checked_end:
        values.append(cursor)
        cursor += timedelta(seconds=seconds)
    return pl.DataFrame(
        {"timestamp": values}, schema={"timestamp": pl.Datetime("ms", "UTC")}
    ).lazy()


def _validate_inputs(
    bars: pl.LazyFrame,
    *,
    base_interval: str,
    bars_dataset_version: str,
    contracts_dataset_version: str,
    config: UniverseConfig,
) -> None:
    required = {
        "open_time", "close_time", "symbol", "interval", "quote_volume",
        "is_complete", "dataset_version",
    }
    missing = required - set(bars.collect_schema().names())
    if missing:
        raise UniverseBuildError(f"bar input is missing columns: {sorted(missing)}")
    for name, value in (
        ("bars_dataset_version", bars_dataset_version),
        ("contracts_dataset_version", contracts_dataset_version),
    ):
        if not value or value.lower() == "latest":
            raise UniverseBuildError(f"{name} must be explicit")
    if not config.point_in_time.enabled:
        raise UniverseBuildError("A06 requires point_in_time.enabled=true")
    for window in (
        config.filters.rolling_quote_volume.window,
        config.filters.max_missing_ratio.window,
    ):
        if not is_integer_multiple(window, base_interval):
            raise UniverseBuildError(
                f"rolling window {window!r} must be a multiple of {base_interval!r}"
            )


def _bar_metrics(
    bars: pl.LazyFrame,
    *,
    base_interval: str,
    bars_dataset_version: str,
    history_offsets: pl.LazyFrame | None = None,
) -> pl.LazyFrame:
    timestamp_type = pl.Datetime("ms", "UTC")
    metrics = (
        bars.filter(
            (pl.col("interval") == base_interval)
            & (pl.col("dataset_version") == bars_dataset_version)
        )
        .with_columns(
            pl.col("open_time").cast(timestamp_type),
            pl.col("close_time").cast(timestamp_type),
        )
        .sort(["symbol", "close_time"])
        .with_columns(
            pl.col("is_complete").cast(pl.Int64).alias("_complete"),
            pl.when(pl.col("is_complete"))
            .then(pl.col("quote_volume"))
            .otherwise(0.0)
            .alias("_valid_quote"),
        )
        .with_columns(
            pl.col("_complete").cum_sum().over("symbol").alias("history_bars"),
            pl.col("open_time").min().over("symbol").alias("first_bar_open"),
            pl.col("_valid_quote")
            .cum_sum()
            .over("symbol")
            .alias("_cumulative_quote"),
            pl.col("_complete")
            .cum_sum()
            .over("symbol")
            .alias("_cumulative_complete"),
        )
        .select(
            "symbol",
            "close_time",
            "first_bar_open",
            "history_bars",
            "_cumulative_quote",
            "_cumulative_complete",
        )
        .sort(["symbol", "close_time"])
    )
    if history_offsets is None:
        return metrics
    required = {"symbol", "history_bars_offset", "prior_first_bar_open"}
    missing = required - set(history_offsets.collect_schema().names())
    if missing:
        raise UniverseBuildError(
            f"history offsets are missing columns: {sorted(missing)}"
        )
    offsets = history_offsets.select(
        "symbol",
        pl.col("history_bars_offset").cast(pl.Int64),
        pl.col("prior_first_bar_open").cast(timestamp_type),
    )
    return (
        metrics.join(offsets, on="symbol", how="left")
        .with_columns(
            (
                pl.col("history_bars")
                + pl.col("history_bars_offset").fill_null(0)
            ).alias("history_bars"),
            pl.coalesce("prior_first_bar_open", "first_bar_open").alias(
                "first_bar_open"
            ),
        )
        .drop("history_bars_offset", "prior_first_bar_open")
    )


def _window_baseline(metrics: pl.LazyFrame, prefix: str) -> pl.LazyFrame:
    return metrics.select(
        "symbol",
        pl.col("close_time").alias(f"_{prefix}_baseline_time"),
        pl.col("_cumulative_quote").alias(f"_{prefix}_baseline_quote"),
        pl.col("_cumulative_complete").alias(f"_{prefix}_baseline_complete"),
    ).sort(["symbol", f"_{prefix}_baseline_time"])


def _reason_expression(config: UniverseConfig) -> pl.Expr:
    reason = pl.when(pl.col("symbol").is_in(config.filters.exclude_symbols)).then(
        pl.lit(UniverseReason.EXPLICITLY_EXCLUDED.value)
    )
    if config.point_in_time.use_contract_snapshots:
        reason = (
            reason.when(pl.col("snapshot_time").is_null())
            .then(pl.lit(UniverseReason.NO_CONTRACT_SNAPSHOT.value))
            .when(pl.col("contract_type").str.to_uppercase() != "PERPETUAL")
            .then(pl.lit(UniverseReason.NOT_PERPETUAL.value))
            .when(pl.col("quote_asset") != config.market.quote_asset)
            .then(pl.lit(UniverseReason.WRONG_QUOTE_ASSET.value))
            .when(pl.col("margin_asset") != config.market.margin_asset)
            .then(pl.lit(UniverseReason.WRONG_MARGIN_ASSET.value))
        )
    reason = (
        reason.when(
            pl.col("_listing_time").is_null()
            | (pl.col("_listing_time") > pl.col("timestamp"))
        )
        .then(pl.lit(UniverseReason.NOT_LISTED.value))
        .when(
            pl.col("delivery_time").is_not_null()
            & (pl.col("delivery_time") <= pl.col("timestamp"))
        )
        .then(pl.lit(UniverseReason.DELISTED.value))
    )
    if config.filters.trading_status_only and config.point_in_time.use_contract_snapshots:
        reason = reason.when(pl.col("status") != "TRADING").then(
            pl.lit(UniverseReason.NOT_TRADING.value)
        )
    reason = reason.when(
        pl.col("listing_age_days").is_null()
        | (pl.col("listing_age_days") < config.filters.min_listing_age_days)
    ).then(pl.lit(UniverseReason.WARMUP.value))
    if config.filters.min_history_bars is not None:
        reason = reason.when(
            pl.col("history_bars") < config.filters.min_history_bars
        ).then(pl.lit(UniverseReason.INSUFFICIENT_HISTORY.value))
    if config.filters.max_missing_ratio.maximum is not None:
        reason = reason.when(
            pl.col("missing_ratio") > config.filters.max_missing_ratio.maximum
        ).then(pl.lit(UniverseReason.MISSING_DATA.value))
    if config.filters.rolling_quote_volume.minimum is not None:
        reason = reason.when(
            pl.col("rolling_quote_volume")
            < config.filters.rolling_quote_volume.minimum
        ).then(pl.lit(UniverseReason.ILLIQUID.value))
    return reason.otherwise(pl.lit(UniverseReason.ELIGIBLE.value))


def build_point_in_time_universe(
    bars: pl.LazyFrame,
    contracts: pl.LazyFrame,
    schedule: pl.LazyFrame,
    *,
    config: UniverseConfig,
    base_interval: str,
    bars_dataset_version: str,
    contracts_dataset_version: str,
    history_offsets: pl.LazyFrame | None = None,
) -> UniverseBuildResult:
    """Return one audited eligibility row per schedule timestamp and symbol."""
    _validate_inputs(
        bars,
        base_interval=base_interval,
        bars_dataset_version=bars_dataset_version,
        contracts_dataset_version=contracts_dataset_version,
        config=config,
    )
    if schedule.collect_schema().names() != ["timestamp"]:
        raise UniverseBuildError("schedule must contain only a timestamp column")
    metrics = _bar_metrics(
        bars,
        base_interval=base_interval,
        bars_dataset_version=bars_dataset_version,
        history_offsets=history_offsets,
    )
    history = prepare_contract_history(contracts).filter(
        pl.col("dataset_version") == contracts_dataset_version
    )
    bar_symbols = bars.filter(
        (pl.col("dataset_version") == bars_dataset_version)
        & (pl.col("interval") == base_interval)
    ).select("symbol")
    symbols = (
        pl.concat([bar_symbols, history.select("symbol")])
        if config.point_in_time.use_contract_snapshots
        else bar_symbols
    ).unique()
    panel = (
        schedule.join(symbols, how="cross")
        .sort(["symbol", "timestamp"])
        .join_asof(
            history,
            left_on="timestamp", right_on="snapshot_time", by="symbol",
            strategy="backward", allow_exact_matches=True,
            check_sortedness=False,
        )
        .sort(["symbol", "timestamp"])
        .join_asof(
            metrics,
            left_on="timestamp", right_on="close_time", by="symbol",
            strategy="backward", allow_exact_matches=True,
            check_sortedness=False,
        )
        .with_columns(
            (
                pl.col("timestamp")
                - pl.duration(
                    seconds=duration_seconds(
                        config.filters.rolling_quote_volume.window
                    )
                )
            ).alias("_volume_start"),
            (
                pl.col("timestamp")
                - pl.duration(
                    seconds=duration_seconds(
                        config.filters.max_missing_ratio.window
                    )
                )
            ).alias("_missing_start"),
        )
        .sort(["symbol", "_volume_start"])
        .join_asof(
            _window_baseline(metrics, "volume"),
            left_on="_volume_start",
            right_on="_volume_baseline_time",
            by="symbol",
            strategy="backward",
            allow_exact_matches=True,
            check_sortedness=False,
        )
        .sort(["symbol", "_missing_start"])
        .join_asof(
            _window_baseline(metrics, "missing"),
            left_on="_missing_start",
            right_on="_missing_baseline_time",
            by="symbol",
            strategy="backward",
            allow_exact_matches=True,
            check_sortedness=False,
        )
    )
    listing = (
        pl.coalesce("onboard_time", "first_bar_open")
        if config.point_in_time.use_first_last_valid_bar
        else pl.col("onboard_time")
    )
    panel = panel.with_columns(
        listing.alias("_listing_time"),
        pl.col("history_bars").fill_null(0).cast(pl.Int64),
        (
            pl.col("_cumulative_quote").fill_null(0.0)
            - pl.col("_volume_baseline_quote").fill_null(0.0)
        ).alias("rolling_quote_volume"),
        (
            1.0
            - (
                pl.col("_cumulative_complete").fill_null(0)
                - pl.col("_missing_baseline_complete").fill_null(0)
            )
            / (
                duration_seconds(config.filters.max_missing_ratio.window)
                // duration_seconds(base_interval)
            )
        )
        .clip(0.0, 1.0)
        .alias("missing_ratio"),
    ).with_columns(
        pl.when(pl.col("_listing_time").is_not_null())
        .then((pl.col("timestamp") - pl.col("_listing_time")).dt.total_days())
        .otherwise(None)
        .cast(pl.Int64)
        .alias("listing_age_days")
    )
    version_payload = {
        "code_version": UNIVERSE_CODE_VERSION,
        "bars_dataset_version": bars_dataset_version,
        "contracts_dataset_version": contracts_dataset_version,
        "base_interval": base_interval,
        "config": config.model_dump(mode="json"),
    }
    version = f"a06-{content_sha256(version_payload)[:24]}"
    output = (
        panel.with_columns(
            _reason_expression(config).alias("reason_code"),
            pl.lit(version).alias("universe_version"),
            pl.lit(bars_dataset_version).alias("bars_dataset_version"),
            pl.lit(contracts_dataset_version).alias("contracts_dataset_version"),
        )
        .with_columns(
            (pl.col("reason_code") == UniverseReason.ELIGIBLE.value)
            .alias("is_eligible")
        )
        .select(
            "timestamp", "symbol", "is_eligible", "reason_code",
            "listing_age_days", "history_bars", "rolling_quote_volume",
            "missing_ratio", pl.col("status").alias("contract_status"),
            "universe_version", "bars_dataset_version", "contracts_dataset_version",
        )
        .sort(["timestamp", "symbol"])
    )
    return UniverseBuildResult(
        frame=output,
        universe_version=version,
        bars_dataset_version=bars_dataset_version,
        contracts_dataset_version=contracts_dataset_version,
        base_interval=base_interval,
    )
