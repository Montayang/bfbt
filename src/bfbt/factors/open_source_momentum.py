"""Source-pinned open-source trend and momentum factor formulas."""

from __future__ import annotations

from math import isfinite

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.config.factor import FactorDefinition
from bfbt.factors.base import FactorError


SUPPORTED_FACTORS = (
    "oss_qlib_beta",
    "oss_qlib_signed_rsqr",
    "oss_qlib_rsv",
    "oss_qlib_imxd",
    "oss_qlib_cntd",
    "oss_ta_trix",
    "oss_ta_tsi",
    "oss_ta_kst",
    "oss_ta_kama_distance",
    "oss_ta_vortex_diff",
    "oss_ta_vpt_roll",
    "oss_qlib_roc_mom",
    "oss_qlib_sumd",
    "oss_qlib_cord",
)

_DEFAULT_WINDOWS = {
    "oss_qlib_beta": 20,
    "oss_qlib_signed_rsqr": 20,
    "oss_qlib_rsv": 20,
    "oss_qlib_imxd": 20,
    "oss_qlib_cntd": 20,
    "oss_ta_trix": 15,
    "oss_ta_vortex_diff": 14,
    "oss_ta_vpt_roll": 20,
    "oss_qlib_roc_mom": 20,
    "oss_qlib_sumd": 20,
    "oss_qlib_cord": 20,
}


def _positive_int(value: object, name: str, *, minimum: int = 1) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
    ):
        raise FactorError(f"{name} must be an integer >= {minimum}")
    return value


def _window(definition: FactorDefinition, factor: str) -> int:
    return _positive_int(
        definition.parameters.get("window_bars", _DEFAULT_WINDOWS[factor]),
        "window_bars",
        minimum=2,
    )


def _volume_field(definition: FactorDefinition) -> str:
    value = definition.parameters.get("volume_field", "quote_volume")
    if value not in {"quote_volume", "volume"}:
        raise FactorError("volume_field must be quote_volume or volume")
    return str(value)


def _prepared(
    bars: pl.LazyFrame,
    *,
    base_interval: str,
    value_columns: tuple[str, ...],
) -> tuple[pl.LazyFrame, list[str]]:
    expected_ms = duration_seconds(base_interval) * 1_000
    valid = pl.col("is_complete")
    for column in value_columns:
        candidate = pl.col(column)
        valid &= candidate.is_not_null() & candidate.is_finite()
        if column in {"open", "high", "low", "close"}:
            valid &= candidate > 0
        else:
            valid &= candidate >= 0
    previous_time = pl.col("close_time").shift(1).over("symbol")
    previous_valid = valid.shift(1).over("symbol")
    new_segment = (
        previous_time.is_null()
        | (
            pl.col("close_time").cast(pl.Int64)
            - previous_time.cast(pl.Int64)
            != expected_ms
        )
        | ~valid
        | ~previous_valid.fill_null(False)
    )
    prepared = bars.with_columns(
        valid.alias("_row_valid"),
        new_segment.cast(pl.UInt32).cum_sum().over("symbol").alias("_segment"),
    )
    return prepared, ["symbol", "_segment"]


def _rolling_regression(
    close: pl.Expr, window: int, groups: list[str]
) -> tuple[pl.Expr, pl.Expr]:
    center = (window - 1) / 2.0
    weighted = pl.sum_horizontal(
        [
            (position - center)
            * close.shift(window - 1 - position).over(groups)
            for position in range(window)
        ]
    )
    weight_square_sum = window * (window * window - 1) / 12.0
    total = close.rolling_sum(window, min_samples=window).over(groups)
    square_total = (
        (close * close).rolling_sum(window, min_samples=window).over(groups)
    )
    centered_square_sum = square_total - total * total / window
    ready = close.shift(window - 1).over(groups).is_not_null()
    slope = pl.when(ready).then(weighted / weight_square_sum)
    rsquare = weighted * weighted / (
        weight_square_sum * centered_square_sum
    )
    return slope, pl.when(centered_square_sum > 0).then(rsquare)


def _extreme_position(
    value: pl.Expr,
    extreme: pl.Expr,
    window: int,
    groups: list[str],
) -> pl.Expr:
    """Return Qlib-compatible 1-based oldest-first arg-extreme position."""

    return pl.min_horizontal(
        [
            pl.when(value.shift(window - position).over(groups) == extreme)
            .then(position)
            .otherwise(window + 1)
            for position in range(1, window + 1)
        ]
    )


def _kama_parameters(definition: FactorDefinition) -> tuple[int, int, int]:
    efficiency = _positive_int(
        definition.parameters.get("efficiency_bars", 10),
        "efficiency_bars",
        minimum=2,
    )
    fast = _positive_int(
        definition.parameters.get("fast_span", 2), "fast_span"
    )
    slow = _positive_int(
        definition.parameters.get("slow_span", 30), "slow_span"
    )
    if fast >= slow:
        raise FactorError("fast_span must be less than slow_span")
    return efficiency, fast, slow


def _kama_group(
    frame: pl.DataFrame, *, efficiency: int, fast: int, slow: int
) -> pl.DataFrame:
    closes = frame["close"].to_list()
    output: list[float | None] = [None] * len(closes)
    if len(closes) > efficiency:
        fast_constant = 2.0 / (fast + 1.0)
        slow_constant = 2.0 / (slow + 1.0)
        kama = float(closes[efficiency - 1])
        changes = [
            abs(float(closes[index]) - float(closes[index - 1]))
            for index in range(1, len(closes))
        ]
        for index in range(efficiency, len(closes)):
            noise = sum(changes[index - efficiency : index])
            if noise <= 0:
                continue
            efficiency_ratio = abs(
                float(closes[index]) - float(closes[index - efficiency])
            ) / noise
            smoothing = (
                efficiency_ratio * (fast_constant - slow_constant)
                + slow_constant
            ) ** 2
            close = float(closes[index])
            kama += smoothing * (close - kama)
            value = close / kama - 1.0
            output[index] = value if isfinite(value) else None
    return pl.DataFrame(
        {
            "timestamp": frame["close_time"],
            "symbol": frame["symbol"],
            "raw_value": output,
        },
        schema={
            "timestamp": pl.Datetime("ms", "UTC"),
            "symbol": pl.String,
            "raw_value": pl.Float64,
        },
    )


def _kama_raw(
    prepared: pl.LazyFrame,
    definition: FactorDefinition,
) -> pl.LazyFrame:
    efficiency, fast, slow = _kama_parameters(definition)
    schema = {
        "timestamp": pl.Datetime("ms", "UTC"),
        "symbol": pl.String,
        "raw_value": pl.Float64,
    }
    return (
        prepared.filter(pl.col("_row_valid"))
        .select("close_time", "symbol", "close", "_segment")
        .group_by("symbol", "_segment", maintain_order=True)
        .map_groups(
            lambda group: _kama_group(
                group, efficiency=efficiency, fast=fast, slow=slow
            ),
            schema=schema,
        )
        .sort(["timestamp", "symbol"])
    )


def open_source_momentum_raw(
    bars: pl.LazyFrame,
    definition: FactorDefinition,
    *,
    base_interval: str,
    factor: str,
) -> pl.LazyFrame:
    """Compute a selected causal formula using literal source-bar counts."""

    if factor not in SUPPORTED_FACTORS:
        raise FactorError(f"unsupported open-source factor: {factor}")
    required = (
        ("close", "high", "low")
        if factor in {"oss_qlib_rsv", "oss_qlib_imxd", "oss_ta_vortex_diff"}
        else (
            ("close", _volume_field(definition))
            if factor in {"oss_ta_vpt_roll", "oss_qlib_cord"}
            else ("close",)
        )
    )
    prepared, groups = _prepared(
        bars, base_interval=base_interval, value_columns=required
    )
    if factor == "oss_ta_kama_distance":
        return _kama_raw(prepared, definition)

    close = pl.col("close")
    if factor in {"oss_qlib_beta", "oss_qlib_signed_rsqr"}:
        window = _window(definition, factor)
        slope, rsquare = _rolling_regression(close, window, groups)
        normalized_slope = slope / close
        if factor == "oss_qlib_beta":
            raw = normalized_slope
        else:
            raw = pl.when(rsquare.is_not_null()).then(
                pl.when(normalized_slope > 0)
                .then(rsquare)
                .when(normalized_slope < 0)
                .then(-rsquare)
                .otherwise(0.0)
            )
    elif factor == "oss_qlib_rsv":
        window = _window(definition, factor)
        lowest = pl.col("low").rolling_min(window).over(groups)
        highest = pl.col("high").rolling_max(window).over(groups)
        span = highest - lowest
        raw = pl.when(span > 0).then((close - lowest) / span)
    elif factor == "oss_qlib_imxd":
        window = _window(definition, factor)
        highest = pl.col("high").rolling_max(window).over(groups)
        lowest = pl.col("low").rolling_min(window).over(groups)
        maximum_position = _extreme_position(
            pl.col("high"), highest, window, groups
        )
        minimum_position = _extreme_position(
            pl.col("low"), lowest, window, groups
        )
        ready = close.shift(window - 1).over(groups).is_not_null()
        raw = pl.when(ready).then(
            (maximum_position - minimum_position) / window
        )
    elif factor == "oss_qlib_cntd":
        window = _window(definition, factor)
        previous = close.shift(1).over(groups)
        up = (close > previous).cast(pl.Float64)
        down = (close < previous).cast(pl.Float64)
        raw = (
            up.rolling_mean(window, min_samples=window).over(groups)
            - down.rolling_mean(window, min_samples=window).over(groups)
        )
    elif factor == "oss_qlib_sumd":
        window = _window(definition, factor)
        change = close - close.shift(1).over(groups)
        up = pl.when(change > 0).then(change).otherwise(0.0)
        down = pl.when(change < 0).then(-change).otherwise(0.0)
        up_sum = up.rolling_sum(window, min_samples=window).over(groups)
        down_sum = down.rolling_sum(window, min_samples=window).over(groups)
        movement = up_sum + down_sum
        raw = pl.when(movement > 0).then((up_sum - down_sum) / movement)
    elif factor == "oss_qlib_roc_mom":
        window = _window(definition, factor)
        delayed = close.shift(window).over(groups)
        raw = close / delayed - 1.0
    elif factor == "oss_qlib_cord":
        window = _window(definition, factor)
        volume = pl.col(_volume_field(definition))
        price_ratio = close / close.shift(1).over(groups)
        volume_ratio = volume / volume.shift(1).over(groups)
        raw = pl.rolling_corr(
            price_ratio,
            (volume_ratio + 1.0).log(),
            window_size=window,
            min_samples=window,
        ).over(groups)
    elif factor == "oss_ta_trix":
        window = _window(definition, factor)
        first = close.ewm_mean(
            span=window,
            adjust=False,
            min_samples=window,
            ignore_nulls=False,
        ).over(groups)
        second = first.ewm_mean(
            span=window,
            adjust=False,
            min_samples=window,
            ignore_nulls=False,
        ).over(groups)
        third = second.ewm_mean(
            span=window,
            adjust=False,
            min_samples=window,
            ignore_nulls=False,
        ).over(groups)
        delayed = third.shift(1).over(groups)
        raw = pl.when(delayed > 0).then((third / delayed - 1.0) * 100.0)
    elif factor == "oss_ta_tsi":
        slow = _positive_int(
            definition.parameters.get("slow_span", 25), "slow_span"
        )
        fast = _positive_int(
            definition.parameters.get("fast_span", 13), "fast_span"
        )
        if fast >= slow:
            raise FactorError("fast_span must be less than slow_span")
        change = close - close.shift(1).over(groups)
        numerator = change.ewm_mean(
            span=slow,
            adjust=False,
            min_samples=slow,
            ignore_nulls=False,
        ).over(groups).ewm_mean(
            span=fast,
            adjust=False,
            min_samples=fast,
            ignore_nulls=False,
        ).over(groups)
        denominator = change.abs().ewm_mean(
            span=slow,
            adjust=False,
            min_samples=slow,
            ignore_nulls=False,
        ).over(groups).ewm_mean(
            span=fast,
            adjust=False,
            min_samples=fast,
            ignore_nulls=False,
        ).over(groups)
        raw = pl.when(denominator > 0).then(numerator / denominator * 100.0)
    elif factor == "oss_ta_kst":
        roc_bars = definition.parameters.get("roc_bars", [10, 15, 20, 30])
        smooth_bars = definition.parameters.get(
            "smooth_bars", [10, 10, 10, 15]
        )
        if not isinstance(roc_bars, list) or not isinstance(smooth_bars, list):
            raise FactorError("roc_bars and smooth_bars must be four-item lists")
        if len(roc_bars) != 4 or len(smooth_bars) != 4:
            raise FactorError("roc_bars and smooth_bars must be four-item lists")
        rocs = [
            _positive_int(value, f"roc_bars[{index}]")
            for index, value in enumerate(roc_bars)
        ]
        smooths = [
            _positive_int(value, f"smooth_bars[{index}]")
            for index, value in enumerate(smooth_bars)
        ]
        components = []
        for index, (roc_window, smooth_window) in enumerate(
            zip(rocs, smooths)
        ):
            roc = close / close.shift(roc_window).over(groups) - 1.0
            components.append(
                (index + 1)
                * roc.rolling_mean(
                    smooth_window, min_samples=smooth_window
                ).over(groups)
            )
        raw = 100.0 * pl.sum_horizontal(components, ignore_nulls=False)
    elif factor == "oss_ta_vortex_diff":
        window = _window(definition, factor)
        previous_close = close.shift(1).over(groups)
        previous_low = pl.col("low").shift(1).over(groups)
        previous_high = pl.col("high").shift(1).over(groups)
        true_range = pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - previous_close).abs(),
            (pl.col("low") - previous_close).abs(),
        )
        positive = (pl.col("high") - previous_low).abs()
        negative = (pl.col("low") - previous_high).abs()
        range_sum = true_range.rolling_sum(
            window, min_samples=window
        ).over(groups)
        positive_sum = positive.rolling_sum(
            window, min_samples=window
        ).over(groups)
        negative_sum = negative.rolling_sum(
            window, min_samples=window
        ).over(groups)
        raw = pl.when(range_sum > 0).then(
            (positive_sum - negative_sum) / range_sum
        )
    else:  # oss_ta_vpt_roll
        window = _window(definition, factor)
        volume = pl.col(_volume_field(definition))
        returns = close / close.shift(1).over(groups) - 1.0
        weighted = (volume * returns).rolling_sum(
            window, min_samples=window
        ).over(groups)
        total_volume = volume.rolling_sum(
            window, min_samples=window
        ).over(groups)
        raw = pl.when(total_volume > 0).then(weighted / total_volume)

    return prepared.select(
        pl.col("close_time").alias("timestamp"),
        "symbol",
        pl.when(pl.col("_row_valid") & raw.is_finite())
        .then(raw)
        .otherwise(None)
        .alias("raw_value"),
    )
