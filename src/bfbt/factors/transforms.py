"""Eligible-universe cross-sectional factor transformations."""

from __future__ import annotations

import polars as pl

from bfbt.config.factor import PreprocessStep


def _invalidate_null(frame: pl.LazyFrame, reason: str) -> pl.LazyFrame:
    return frame.with_columns(
        (
            pl.col("is_valid")
            & pl.col("value").is_not_null()
            & pl.col("value").is_finite()
        ).alias("is_valid"),
        pl.when(
            pl.col("is_valid")
            & (pl.col("value").is_null() | ~pl.col("value").is_finite())
        )
        .then(pl.lit(reason))
        .otherwise(pl.col("invalid_reason"))
        .alias("invalid_reason"),
    )


def apply_preprocess(
    frame: pl.LazyFrame,
    steps: tuple[PreprocessStep, ...],
) -> pl.LazyFrame:
    """Apply ordered transforms within each timestamp's valid eligible rows."""

    result = frame
    for step in steps:
        if not step.cross_sectional:
            raise ValueError("A07 only supports cross-sectional transforms")
        valid_value = pl.when(pl.col("is_valid")).then(pl.col("value"))
        if step.name == "winsorize":
            lower = valid_value.quantile(step.lower).over("timestamp")
            upper = valid_value.quantile(step.upper).over("timestamp")
            result = result.with_columns(
                pl.when(pl.col("is_valid"))
                .then(pl.col("value").clip(lower, upper))
                .otherwise(pl.col("value"))
                .alias("value")
            )
            result = _invalidate_null(result, "INVALID_WINSORIZE")
        elif step.name == "rank":
            count = valid_value.count().over("timestamp")
            rank = valid_value.rank(method="average").over("timestamp")
            result = result.with_columns(
                pl.when(~pl.col("is_valid"))
                .then(pl.col("value"))
                .when(count == 1)
                .then(0.5)
                .otherwise((rank - 1.0) / (count - 1.0))
                .alias("value")
            )
            result = _invalidate_null(result, "INVALID_RANK")
        elif step.name == "zscore":
            mean = valid_value.mean().over("timestamp")
            std = valid_value.std(ddof=0).over("timestamp")
            result = result.with_columns(
                pl.when(pl.col("is_valid") & (std > 0))
                .then((pl.col("value") - mean) / std)
                .when(pl.col("is_valid"))
                .then(None)
                .otherwise(pl.col("value"))
                .alias("value")
            )
            result = _invalidate_null(result, "ZERO_CROSS_SECTION_VARIANCE")
        else:
            raise ValueError(f"unsupported preprocess step: {step.name}")
    return result
