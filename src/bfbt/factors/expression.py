"""A small causal factor-expression language without eval or arbitrary code."""

from __future__ import annotations

import ast
from dataclasses import dataclass

import polars as pl

from bfbt.config.durations import duration_seconds
from bfbt.data.hashing import content_sha256
from bfbt.factors.base import FactorError, FactorResult, require_columns


class FactorExpressionError(ValueError):
    """A factor expression is unsafe, non-causal, or unsupported."""


ALLOWED_FIELDS = frozenset({
    "open", "high", "low", "close", "volume", "quote_volume",
    "taker_buy_base_volume", "taker_buy_quote_volume", "trades",
})
ROLLING_FUNCTIONS = frozenset({
    "lag", "rolling_mean", "rolling_sum", "rolling_min", "rolling_max",
    "rolling_std", "ema",
})
UNARY_FUNCTIONS = frozenset({"abs", "log"})


@dataclass(frozen=True)
class CompiledFactorExpression:
    source: str
    canonical: str
    expression_id: str
    required_columns: tuple[str, ...]
    warmup_bars: int
    expression: pl.Expr


def _integer(node: ast.AST, label: str) -> int:
    if not isinstance(node, ast.Constant) or isinstance(node.value, bool):
        raise FactorExpressionError(f"{label} must be a positive integer literal")
    if not isinstance(node.value, int) or not 1 <= node.value <= 100_000:
        raise FactorExpressionError(f"{label} must be within 1..100000")
    return node.value


def _compile(
    node: ast.AST, group_columns: tuple[str, ...]
) -> tuple[pl.Expr, set[str], int]:
    if isinstance(node, ast.Name):
        if node.id not in ALLOWED_FIELDS:
            raise FactorExpressionError(f"field is not allowed: {node.id}")
        return pl.col(node.id).cast(pl.Float64), {node.id}, 0
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FactorExpressionError("constants must be finite numbers")
        value = float(node.value)
        if not (-1e100 < value < 1e100):
            raise FactorExpressionError("constant is outside the supported finite range")
        return pl.lit(value), set(), 0
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value, columns, warmup = _compile(node.operand, group_columns)
        return ((-value) if isinstance(node.op, ast.USub) else value), columns, warmup
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
    ):
        left, left_columns, left_warmup = _compile(node.left, group_columns)
        right, right_columns, right_warmup = _compile(node.right, group_columns)
        if isinstance(node.op, ast.Add):
            result = left + right
        elif isinstance(node.op, ast.Sub):
            result = left - right
        elif isinstance(node.op, ast.Mult):
            result = left * right
        else:
            result = left / right
        return result, left_columns | right_columns, max(left_warmup, right_warmup)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise FactorExpressionError("only named functions without keywords are allowed")
        name = node.func.id
        if name in UNARY_FUNCTIONS:
            if len(node.args) != 1:
                raise FactorExpressionError(f"{name} requires one argument")
            value, columns, warmup = _compile(node.args[0], group_columns)
            return (
                value.abs() if name == "abs" else value.log(), columns, warmup
            )
        if name not in ROLLING_FUNCTIONS or len(node.args) != 2:
            raise FactorExpressionError(f"unsupported function: {name}")
        value, columns, child_warmup = _compile(node.args[0], group_columns)
        window = _integer(node.args[1], f"{name} window")
        if name == "lag":
            result = value.shift(window).over(group_columns)
            added = window
        elif name == "rolling_mean":
            result = value.rolling_mean(
                window_size=window, min_samples=window
            ).over(group_columns)
            added = window - 1
        elif name == "rolling_sum":
            result = value.rolling_sum(
                window_size=window, min_samples=window
            ).over(group_columns)
            added = window - 1
        elif name == "rolling_min":
            result = value.rolling_min(
                window_size=window, min_samples=window
            ).over(group_columns)
            added = window - 1
        elif name == "rolling_max":
            result = value.rolling_max(
                window_size=window, min_samples=window
            ).over(group_columns)
            added = window - 1
        elif name == "rolling_std":
            result = value.rolling_std(
                window_size=window, min_samples=window
            ).over(group_columns)
            added = window - 1
        else:
            result = value.ewm_mean(
                span=window, adjust=False, min_samples=window
            ).over(group_columns)
            added = window - 1
        return result, columns, child_warmup + added
    raise FactorExpressionError(
        f"syntax is not allowed: {type(node).__name__}"
    )


def compile_factor_expression(
    source: str, *, group_columns: tuple[str, ...] = ("symbol",)
) -> CompiledFactorExpression:
    """Validate and compile one expression to a causal Polars expression."""

    if not source.strip() or len(source) > 2_000:
        raise FactorExpressionError("expression must contain 1..2000 characters")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise FactorExpressionError(f"invalid expression syntax: {exc.msg}") from exc
    if sum(1 for _ in ast.walk(tree)) > 200:
        raise FactorExpressionError("expression is too complex")
    if not group_columns or any(not value for value in group_columns):
        raise FactorExpressionError("group_columns must be non-empty")
    expression, columns, warmup = _compile(tree.body, group_columns)
    canonical = ast.dump(tree, annotate_fields=True, include_attributes=False)
    identity = {
        "language": "bfbt-factor-expression/v1",
        "canonical": canonical,
        "required_columns": sorted(columns),
        "warmup_bars": warmup,
    }
    return CompiledFactorExpression(
        source=source,
        canonical=canonical,
        expression_id=f"fx-{content_sha256(identity)[:24]}",
        required_columns=tuple(sorted(columns)),
        warmup_bars=warmup,
        expression=expression,
    )


def compute_expression_factor(
    bars: pl.LazyFrame,
    universe: pl.LazyFrame,
    *,
    source: str,
    factor_name: str,
    direction: str,
    base_interval: str,
    compute_interval: str,
    bars_dataset_version: str,
    universe_version: str,
) -> FactorResult:
    """Compute one sandboxed expression with gap-reset, point-in-time semantics."""

    if direction not in {"positive", "negative"}:
        raise FactorError("expression direction must be positive or negative")
    base_ms = duration_seconds(base_interval) * 1_000
    compute_ms = duration_seconds(compute_interval) * 1_000
    if compute_ms < base_ms or compute_ms % base_ms:
        raise FactorError("compute_interval must be a multiple of base_interval")
    prototype = compile_factor_expression(source)
    require_columns(
        bars,
        (
            "open_time", "close_time", "symbol", "interval", "is_complete",
            "dataset_version", *prototype.required_columns,
        ),
    )
    require_columns(
        universe, ("timestamp", "symbol", "is_eligible", "universe_version")
    )
    timestamp = pl.Datetime("ms", "UTC")
    prepared = (
        bars.filter(
            (pl.col("interval") == base_interval)
            & (pl.col("dataset_version") == bars_dataset_version)
        )
        .with_columns(
            pl.col("open_time").cast(timestamp),
            pl.col("close_time").cast(timestamp),
        )
        .sort(["symbol", "open_time"])
        .with_columns(
            (
                (
                    pl.col("open_time").cast(pl.Int64).diff().over("symbol")
                    != base_ms
                ).fill_null(False)
                | ~pl.col("is_complete")
            ).cast(pl.Int64).cum_sum().over("symbol").alias("_segment")
        )
    )
    compiled = compile_factor_expression(
        source, group_columns=("symbol", "_segment")
    )
    raw = (
        prepared.with_columns(compiled.expression.alias("raw_value"))
        .filter(
            pl.col("close_time").cast(pl.Int64) % compute_ms == 0
        )
        .select(pl.col("close_time").alias("timestamp"), "symbol", "raw_value")
    )
    eligible = (
        universe.filter(
            pl.col("is_eligible")
            & (pl.col("universe_version") == universe_version)
        )
        .filter(pl.col("timestamp").cast(pl.Int64) % compute_ms == 0)
        .select(pl.col("timestamp").cast(timestamp), "symbol")
    )
    sign = 1.0 if direction == "positive" else -1.0
    values = (
        eligible.join(raw, on=["timestamp", "symbol"], how="left")
        .with_columns(
            (pl.col("raw_value") * sign).alias("value"),
            (
                pl.col("raw_value").is_not_null()
                & pl.col("raw_value").is_finite()
            ).alias("is_valid"),
            pl.when(pl.col("raw_value").is_null())
            .then(pl.lit("INSUFFICIENT_OR_GAPPED_HISTORY"))
            .when(~pl.col("raw_value").is_finite())
            .then(pl.lit("NON_FINITE"))
            .otherwise(None)
            .alias("invalid_reason"),
        )
    )
    version = f"{compiled.expression_id}-{content_sha256({
        'bars_dataset_version': bars_dataset_version,
        'universe_version': universe_version,
        'base_interval': base_interval,
        'compute_interval': compute_interval,
        'direction': direction,
    })[:24]}"
    output = values.with_columns(
        pl.lit(factor_name).alias("factor_name"),
        pl.lit(version).alias("factor_version"),
        pl.lit(universe_version).alias("universe_version"),
        pl.lit(bars_dataset_version).alias("dataset_version"),
    ).select(
        "timestamp", "symbol", "factor_name", "factor_version", "raw_value",
        "value", "is_valid", "invalid_reason", "universe_version",
        "dataset_version",
    ).sort(["timestamp", "symbol"])
    return FactorResult(
        frame=output,
        factor_name=factor_name,
        factor_version=version,
        bars_dataset_version=bars_dataset_version,
        universe_version=universe_version,
        base_interval=base_interval,
    )
