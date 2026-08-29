"""Point-in-time intrabar EMA ratios on a slower candle clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite

import polars as pl

from bianbt.config.durations import duration_seconds
from bianbt.config.factor import FactorDefinition
from bianbt.factors.base import FactorError

UTC_MS = pl.Datetime("ms", "UTC")
STATE_COLUMNS = {
    "symbol": pl.String,
    "fast_ema": pl.Float64,
    "slow_ema": pl.Float64,
    "sample_count": pl.Int64,
    "last_bucket_end": UTC_MS,
}


def _span(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FactorError(f"{name} must be a positive integer")
    return value


def _parameters(
    definition: FactorDefinition, base_interval: str
) -> tuple[int, int, int, int]:
    fast = _span(definition.parameters.get("fast_span"), "fast_span")
    slow = _span(definition.parameters.get("slow_span"), "slow_span")
    if fast >= slow:
        raise FactorError("fast_span must be less than slow_span")
    source = definition.parameters.get("source_interval")
    if not isinstance(source, str):
        raise FactorError("source_interval must be a duration string")
    source_seconds = duration_seconds(source)
    base_seconds = duration_seconds(base_interval)
    if source_seconds <= base_seconds or source_seconds % base_seconds:
        raise FactorError(
            "source_interval must be a multiple of and longer than base_interval"
        )
    return fast, slow, source_seconds, base_seconds


def _state_rows(state: pl.DataFrame | None) -> dict[str, dict[str, object]]:
    if state is None:
        return {}
    missing = set(STATE_COLUMNS) - set(state.columns)
    if missing:
        raise FactorError(f"EMA state is missing columns: {sorted(missing)}")
    rows: dict[str, dict[str, object]] = {}
    for row in state.select(tuple(STATE_COLUMNS)).to_dicts():
        symbol = str(row["symbol"])
        fast = float(row["fast_ema"])
        slow = float(row["slow_ema"])
        count = int(row["sample_count"])
        last = row["last_bucket_end"]
        if (
            symbol in rows
            or not isfinite(fast)
            or not isfinite(slow)
            or fast <= 0
            or slow <= 0
            or count < 1
            or not isinstance(last, datetime)
        ):
            raise FactorError("EMA state contains an invalid or duplicate row")
        rows[symbol] = {
            "fast_ema": fast,
            "slow_ema": slow,
            "sample_count": count,
            "last_bucket_end": last,
        }
    return rows


def _state_frame(states: dict[str, dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "symbol": symbol,
                **states[symbol],
            }
            for symbol in sorted(states)
        ],
        schema=STATE_COLUMNS,
    )


def intrabar_ema_ratio_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
    initial_state: pl.DataFrame | None = None,
    state_start: datetime | None = None,
) -> tuple[pl.LazyFrame, pl.DataFrame]:
    """Return minute-live EMA ratios and the last committed candle state.

    The current slower candle's close is revised every base bar. Its provisional
    EMA is always derived from the preceding *closed* slower-candle EMA, so the
    same slower candle is never recursively counted more than once.
    """

    fast_span, slow_span, source_seconds, base_seconds = _parameters(
        definition, base_interval
    )
    if (initial_state is None) != (state_start is None):
        raise FactorError("initial_state and state_start must be supplied together")
    source_ms = source_seconds * 1_000
    base_ms = base_seconds * 1_000
    expected_rows = source_seconds // base_seconds
    working = bars
    if state_start is not None:
        if int(state_start.timestamp()) % source_seconds:
            raise FactorError("state_start must align to source_interval")
        working = working.filter(pl.col("open_time") >= pl.lit(state_start))
    bucket_end_ms = (
        ((pl.col("close_time").cast(pl.Int64) - 1) // source_ms + 1)
        * source_ms
    )
    minute = working.with_columns(
        bucket_end_ms.cast(UTC_MS).alias("bucket_end")
    ).sort(["symbol", "close_time"])
    summaries = (
        minute.group_by("symbol", "bucket_end")
        .agg(
            pl.len().alias("row_count"),
            pl.col("is_complete").cast(pl.Int64).sum().alias("complete_count"),
            pl.col("close_time").min().alias("first_close_time"),
            pl.col("close_time").max().alias("last_close_time"),
            pl.col("close").sort_by("close_time").last().alias("closing_price"),
        )
        .sort(["symbol", "bucket_end"])
        .collect(engine="streaming")
        .to_dicts()
    )

    states = _state_rows(initial_state)
    prior_rows: list[dict[str, object]] = []
    alpha_fast = 2.0 / (fast_span + 1.0)
    alpha_slow = 2.0 / (slow_span + 1.0)
    source_delta = timedelta(seconds=source_seconds)
    first_offset = timedelta(seconds=source_seconds - base_seconds)
    for row in summaries:
        symbol = str(row["symbol"])
        bucket_end = row["bucket_end"]
        assert isinstance(bucket_end, datetime)
        prior = states.get(symbol)
        if prior is not None and (
            prior["last_bucket_end"] + source_delta != bucket_end
        ):
            prior = None
            states.pop(symbol, None)
        prior_rows.append(
            {
                "symbol": symbol,
                "bucket_end": bucket_end,
                "prior_fast_ema": (
                    float(prior["fast_ema"]) if prior is not None else None
                ),
                "prior_slow_ema": (
                    float(prior["slow_ema"]) if prior is not None else None
                ),
                "prior_sample_count": (
                    int(prior["sample_count"]) if prior is not None else 0
                ),
            }
        )
        closing_price = float(row["closing_price"])
        complete = (
            int(row["row_count"]) == expected_rows
            and int(row["complete_count"]) == expected_rows
            and row["first_close_time"] == bucket_end - first_offset
            and row["last_close_time"] == bucket_end
            and isfinite(closing_price)
            and closing_price > 0
        )
        if not complete:
            states.pop(symbol, None)
            continue
        if prior is None:
            fast_ema = slow_ema = closing_price
            sample_count = 1
        else:
            fast_ema = (
                alpha_fast * closing_price
                + (1.0 - alpha_fast) * float(prior["fast_ema"])
            )
            slow_ema = (
                alpha_slow * closing_price
                + (1.0 - alpha_slow) * float(prior["slow_ema"])
            )
            sample_count = int(prior["sample_count"]) + 1
        states[symbol] = {
            "fast_ema": fast_ema,
            "slow_ema": slow_ema,
            "sample_count": sample_count,
            "last_bucket_end": bucket_end,
        }

    prior_schema = {
        "symbol": pl.String,
        "bucket_end": UTC_MS,
        "prior_fast_ema": pl.Float64,
        "prior_slow_ema": pl.Float64,
        "prior_sample_count": pl.Int64,
    }
    prior_frame = pl.DataFrame(prior_rows, schema=prior_schema).lazy()
    enriched = (
        minute.join(prior_frame, on=["symbol", "bucket_end"], how="left")
        .with_columns(
            pl.col("close_time").cum_count()
            .over(["symbol", "bucket_end"])
            .alias("rows_so_far"),
            pl.col("is_complete").cast(pl.Int64).cum_sum()
            .over(["symbol", "bucket_end"])
            .alias("complete_so_far"),
            (
                (
                    pl.col("close_time").cast(pl.Int64)
                    - (pl.col("bucket_end").cast(pl.Int64) - source_ms)
                )
                // base_ms
            ).alias("expected_rows_so_far"),
        )
        .with_columns(
            pl.when(pl.col("prior_sample_count") > 0)
            .then(
                alpha_fast * pl.col("close")
                + (1.0 - alpha_fast) * pl.col("prior_fast_ema")
            )
            .otherwise(pl.col("close"))
            .alias("live_fast_ema"),
            pl.when(pl.col("prior_sample_count") > 0)
            .then(
                alpha_slow * pl.col("close")
                + (1.0 - alpha_slow) * pl.col("prior_slow_ema")
            )
            .otherwise(pl.col("close"))
            .alias("live_slow_ema"),
        )
    )
    valid = (
        pl.col("is_complete")
        & (pl.col("rows_so_far") == pl.col("expected_rows_so_far"))
        & (pl.col("complete_so_far") == pl.col("expected_rows_so_far"))
        & (pl.col("expected_rows_so_far") >= 1)
        & (pl.col("expected_rows_so_far") <= expected_rows)
        & (pl.col("prior_sample_count") + 1 >= slow_span)
        & pl.col("close").is_finite()
        & (pl.col("close") > 0)
        & pl.col("live_fast_ema").is_finite()
        & pl.col("live_slow_ema").is_finite()
        & (pl.col("live_slow_ema") > 0)
    )
    raw = enriched.select(
        pl.col("close_time").alias("timestamp"),
        "symbol",
        pl.when(valid)
        .then(pl.col("live_fast_ema") / pl.col("live_slow_ema") - 1.0)
        .otherwise(None)
        .alias("raw_value"),
    )
    if initial_state is not None:
        assert state_start is not None
        opening = initial_state.filter(
            (pl.col("last_bucket_end") == pl.lit(state_start))
            & (pl.col("sample_count") >= slow_span)
        ).select(
            pl.lit(state_start).cast(UTC_MS).alias("timestamp"),
            "symbol",
            (pl.col("fast_ema") / pl.col("slow_ema") - 1.0).alias(
                "raw_value"
            ),
        )
        raw = pl.concat([opening.lazy(), raw], how="vertical")
    return raw.sort(["timestamp", "symbol"]), _state_frame(states)
