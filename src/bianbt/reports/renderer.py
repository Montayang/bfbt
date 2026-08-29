"""Deterministic bilingual HTML report rebuilt only from run artifacts."""

from __future__ import annotations

import html
import json
from bisect import bisect_left
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import polars as pl

from bianbt.factors.registry import FACTOR_REGISTRY

REPORT_VERSION = "a38-report-v18-complete-execution-audit"
NA = "暂无 / N/A"


class ReportError(ValueError):
    """A report cannot be reconstructed from the run directory."""


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _get(value: object, *keys: str, default: Any = None) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _text(value: object) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, bool):
        return "是 / Yes" if value else "否 / No"
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _number(value: object, digits: int = 4) -> str:
    return NA if value is None else f"{float(value):,.{digits}f}"


def _percent(value: object, digits: int = 2) -> str:
    return NA if value is None else f"{float(value) * 100:,.{digits}f}%"


def _currency(value: object, currency: str) -> str:
    return NA if value is None else f"{float(value):,.4f} {currency}"


def _table(rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>"
        for label, value in rows
    )
    return f'<table class="definition"><tbody>{body}</tbody></table>'


def _data_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{html.escape(value)}</th>" for value in headers)
    body = "".join(
        "<tr>"
        + "".join(
            f'<td data-label="{html.escape(headers[index])}">'
            f"{html.escape(value)}</td>"
            for index, value in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<div class="table-wrap"><table class="data-table">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _equity_svg(
    equities: list[float],
    drawdowns: list[float],
    timestamps: list[object],
) -> str:
    width, height, padding = 960, 300, 30
    if not equities:
        raise ReportError("returns artifact is empty")
    minimum, maximum = min(equities), max(equities)
    span = maximum - minimum or 1.0
    denominator = max(1, len(equities) - 1)
    indexes = list(range(len(equities)))
    if len(indexes) > 1_600:
        step = (len(indexes) - 1) / 1_599
        indexes = sorted({round(index * step) for index in range(1_600)})
    points = []
    for index in indexes:
        x = padding + index / denominator * (width - 2 * padding)
        y = padding + (maximum - equities[index]) / span * (height - 2 * padding)
        points.append(f"{x:.2f},{y:.2f}")
    worst = min(drawdowns) if drawdowns else 0.0
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Equity curve; maximum drawdown {worst:.4%}">'
        '<rect width="100%" height="100%" fill="#0d1722" rx="14"/>'
        '<line x1="30" y1="270" x2="930" y2="270" stroke="#263849"/>'
        f'<polyline points="{" ".join(points)}" fill="none" '
        'stroke="#36c9a6" stroke-width="3"/>'
        f'<text x="30" y="292" fill="#8ba1b4" font-size="12">'
        f'{html.escape(_text(timestamps[0]))}</text>'
        f'<text x="930" y="292" text-anchor="end" fill="#8ba1b4" '
        f'font-size="12">{html.escape(_text(timestamps[-1]))}</text>'
        f'<text x="30" y="20" fill="#8ba1b4" font-size="12">'
        f'最大回撤 / Max drawdown {worst:.2%}</text>'
        f'<text x="930" y="20" text-anchor="end" fill="#8ba1b4" '
        f'font-size="12">期末净值 / Ending equity {equities[-1]:.4f}</text>'
        "</svg>"
    )



def _sample_indexes(length: int, limit: int) -> list[int]:
    if length <= 0:
        return []
    if length <= limit:
        return list(range(length))
    step = (length - 1) / (limit - 1)
    return sorted({round(index * step) for index in range(limit)})


def _bounded_lazy_sample(
    frame: pl.LazyFrame,
    *,
    limit: int,
    extra_index: int | None = None,
) -> pl.DataFrame:
    count = int(frame.select(pl.len()).collect(engine="streaming").item())
    if count == 0:
        return frame.limit(0).collect(engine="streaming")
    indexes = set(_sample_indexes(count, limit - (extra_index is not None)))
    if extra_index is not None:
        indexes.add(extra_index)
    return (
        frame.with_row_index("_sample_row")
        .filter(pl.col("_sample_row").is_in(sorted(indexes)))
        .drop("_sample_row")
        .collect(engine="streaming")
    )


def _bounded_returns(root: Path, *, limit: int = 1_360) -> tuple[pl.DataFrame, int]:
    lazy = pl.scan_parquet(
        root / "tables" / "returns.parquet", hive_partitioning=False
    ).sort("timestamp")
    summary = lazy.select(
        pl.len().alias("rows"),
        pl.col("drawdown").arg_min().alias("worst_index"),
    ).collect(engine="streaming").row(0, named=True)
    rows = int(summary["rows"])
    if rows == 0:
        raise ReportError("returns artifact is empty")
    sample = _bounded_lazy_sample(
        lazy,
        limit=limit,
        extra_index=int(summary["worst_index"]),
    )
    return sample.sort("timestamp"), rows


def _bounded_event_times(frame: pl.LazyFrame, *, limit: int) -> list[datetime]:
    sampled = _bounded_lazy_sample(
        frame.sort("event_time"), limit=limit
    )
    return sampled["event_time"].to_list() if sampled.height else []


def _interactive_payload(
    root: Path,
    returns: pl.DataFrame,
    *,
    source_return_rows: int,
) -> str:
    """Build a compact curve without dropping any execution audit point."""

    full_returns = (
        pl.scan_parquet(
            root / "tables" / "returns.parquet", hive_partitioning=False
        )
        .sort("timestamp")
        .collect(engine="streaming")
    )
    timestamps = full_returns["timestamp"].to_list()
    if not timestamps:
        raise ReportError("returns artifact is empty")
    uniform_snapshots = set(_sample_indexes(len(timestamps), 180))
    uniform_snapshots.add(int(full_returns["drawdown"].arg_min()))
    timestamp_type = pl.Datetime("ms", "UTC")
    table_specs = (
        ("trades", "fill_time", None),
        ("rankings", "timestamp", 80),
        ("position_instructions", "decision_time", 240),
        ("risk_events", "evaluation_time", None),
    )
    event_times: list[datetime] = []
    for table, column, event_limit in table_specs:
        path = root / "tables" / f"{table}.parquet"
        if path.is_file() and column in pl.scan_parquet(path).collect_schema().names():
            events = (
                pl.scan_parquet(path)
                .select(pl.col(column).cast(timestamp_type).alias("event_time"))
                .filter(pl.col("event_time").is_not_null())
            )
            if event_limit is None:
                event_times.extend(
                    events.unique().sort("event_time").collect(engine="streaming")[
                        "event_time"
                    ].to_list()
                )
            else:
                event_times.extend(
                    _bounded_event_times(events, limit=event_limit)
                )
    event_times = sorted(set(event_times))
    selected_event_times = event_times
    event_to_index: dict[str, int] = {}
    for event in selected_event_times:
        index = min(bisect_left(timestamps, event), len(timestamps) - 1)
        uniform_snapshots.add(index)
        event_to_index[_text(event)] = index
    snapshot_indexes = sorted(uniform_snapshots)
    snapshot_times = [timestamps[index] for index in snapshot_indexes]
    snapshot_keys = {_text(value) for value in snapshot_times}
    position_context_times = sorted(
        {
            timestamps[neighbor]
            for index in snapshot_indexes
            for neighbor in (
                max(0, index - 1),
                index,
                min(len(timestamps) - 1, index + 1),
            )
        }
    )
    position_context_values = pl.Series(
        "timestamp", position_context_times, dtype=timestamp_type
    )
    event_values = pl.Series(
        "event_time", selected_event_times, dtype=timestamp_type
    )
    snapshots: dict[str, dict[str, object]] = {
        _text(value): {
            "positions": [],
            "positions_before": [],
            "positions_after": [],
            "position_before_time": None,
            "position_after_time": None,
            "trades": [],
            "rankings": [],
            "instructions": [],
            "risk_events": [],
        }
        for value in snapshot_times
    }

    affected_symbols_by_index: dict[int, set[str]] = {}
    trades_path = root / "tables" / "trades.parquet"
    if trades_path.is_file():
        trade_keys = (
            pl.scan_parquet(trades_path)
            .select(
                pl.col("fill_time").cast(timestamp_type),
                pl.col("symbol").cast(pl.String),
            )
            .filter(pl.col("fill_time").is_not_null())
            .collect(engine="streaming")
        )
        for fill_time, symbol in trade_keys.iter_rows():
            index = min(bisect_left(timestamps, fill_time), len(timestamps) - 1)
            affected_symbols_by_index.setdefault(index, set()).add(str(symbol))

    positions_path = root / "tables" / "positions.parquet"
    if positions_path.is_file():
        lazy = pl.scan_parquet(positions_path).with_columns(
            pl.col("timestamp").cast(timestamp_type)
        )
        available = set(lazy.collect_schema().names())
        columns = [
            column
            for column in (
                "timestamp",
                "symbol",
                "quantity",
                "actual_weight",
                "mark_price",
                "signed_notional",
                "unrealized_pnl",
                "average_entry_price",
                "used_margin",
                "available_margin",
                "stop_loss_level",
                "take_profit_level",
                "trailing_stop_level",
                "consecutive_adds",
            )
            if column in available
        ]
        position_rows_per_time = max(
            1, min(80, 4_000 // max(1, len(position_context_times)))
        )
        positions = (
            lazy.filter(
                pl.col("timestamp")
                .cast(timestamp_type)
                .is_in(position_context_values.implode())
            )
            .select(columns)
            .sort(["timestamp", "symbol"])
            .group_by("timestamp", maintain_order=True)
            .head(position_rows_per_time)
            .collect()
        )
        positions_by_time: dict[str, list[dict[str, object]]] = {}
        for row in positions.iter_rows(named=True):
            key = _text(row.pop("timestamp"))
            bucket = positions_by_time.setdefault(key, [])
            if len(bucket) < position_rows_per_time:
                bucket.append(row)
        precise_positions: dict[tuple[str, str], dict[str, object]] = {}
        requested_pairs = {
            (timestamps[neighbor], symbol)
            for index, symbols in affected_symbols_by_index.items()
            for neighbor in (
                max(0, index - 1),
                min(len(timestamps) - 1, index + 1),
            )
            for symbol in symbols
        }
        if requested_pairs:
            requested = pl.DataFrame(
                {
                    "timestamp": [value[0] for value in requested_pairs],
                    "symbol": [value[1] for value in requested_pairs],
                },
                schema_overrides={"timestamp": timestamp_type},
            )
            precise = (
                lazy.select(columns)
                .join(requested.lazy(), on=["timestamp", "symbol"], how="inner")
                .sort(["timestamp", "symbol"])
                .collect(engine="streaming")
            )
            for row in precise.iter_rows(named=True):
                timestamp = _text(row.pop("timestamp"))
                precise_positions[(timestamp, str(row["symbol"]))] = row
        for index in snapshot_indexes:
            key = _text(timestamps[index])
            before_key = _text(timestamps[max(0, index - 1)])
            after_key = _text(timestamps[min(len(timestamps) - 1, index + 1)])
            affected = sorted(affected_symbols_by_index.get(index, ()))
            if affected:
                snapshots[key]["positions"] = positions_by_time.get(key, [])
                snapshots[key]["positions_before"] = [
                    precise_positions[(before_key, symbol)]
                    for symbol in affected
                    if (before_key, symbol) in precise_positions
                ]
                snapshots[key]["positions_after"] = [
                    precise_positions[(after_key, symbol)]
                    for symbol in affected
                    if (after_key, symbol) in precise_positions
                ]
            else:
                snapshots[key]["positions"] = positions_by_time.get(key, [])
                snapshots[key]["positions_before"] = positions_by_time.get(
                    before_key, []
                )
                snapshots[key]["positions_after"] = positions_by_time.get(
                    after_key, []
                )
            snapshots[key]["position_before_time"] = before_key
            snapshots[key]["position_after_time"] = after_key

    def attach(
        *,
        table: str,
        time_column: str,
        payload_key: str,
        fields: tuple[str, ...],
        preserve_all_rows: bool = False,
    ) -> None:
        path = root / "tables" / f"{table}.parquet"
        if not path.is_file() or not selected_event_times:
            return
        lazy = pl.scan_parquet(path)
        available = set(lazy.collect_schema().names())
        if time_column not in available:
            return
        selected = [time_column] + [
            field for field in fields if field in available and field != time_column
        ]
        sort_columns = [time_column]
        if "sequence" in available:
            sort_columns.append("sequence")
        elif "symbol" in available:
            sort_columns.append("symbol")
        event_rows_per_time = (
            None
            if preserve_all_rows
            else max(1, min(80, 4_000 // max(1, len(selected_event_times))))
        )
        selected_rows = (
            lazy.filter(
                pl.col(time_column)
                .cast(timestamp_type)
                .is_in(event_values.implode())
            )
            .select(selected)
            .sort(sort_columns)
        )
        if event_rows_per_time is not None:
            selected_rows = selected_rows.group_by(
                time_column, maintain_order=True
            ).head(event_rows_per_time)
        rows = selected_rows.collect(engine="streaming")
        for row in rows.iter_rows(named=True):
            event_key = _text(row.pop(time_column))
            row = {
                key: _text(value) if isinstance(value, datetime) else value
                for key, value in row.items()
            }
            index = event_to_index.get(event_key)
            if index is None:
                continue
            snapshot_key = _text(timestamps[index])
            row[time_column] = event_key
            if event_rows_per_time is None or len(
                snapshots[snapshot_key][payload_key]
            ) < event_rows_per_time:
                snapshots[snapshot_key][payload_key].append(row)

    attach(
        table="trades",
        time_column="fill_time",
        payload_key="trades",
        fields=(
            "symbol",
            "sequence",
            "side",
            "old_weight",
            "target_weight",
            "filled_weight",
            "fill_price",
            "notional",
            "instruction_reason_code",
        ),
        preserve_all_rows=True,
    )
    attach(
        table="rankings",
        time_column="timestamp",
        payload_key="rankings",
        fields=(
            "symbol",
            "factor_name",
            "raw_score",
            "ordinal_rank",
            "percentile_rank",
            "sample_count",
        ),
    )
    attach(
        table="position_instructions",
        time_column="decision_time",
        payload_key="instructions",
        fields=(
            "instruction_id",
            "rank_source_time",
            "symbol",
            "side",
            "instruction_mode",
            "requested_delta_notional",
            "constrained_delta_notional",
            "requested_target_weight",
            "source_event_id",
            "reason_code",
            "priority",
        ),
    )
    attach(
        table="risk_events",
        time_column="evaluation_time",
        payload_key="risk_events",
        fields=(
            "event_id",
            "trigger_time",
            "fill_time",
            "symbol",
            "event_type",
            "direction",
            "entry_price",
            "trigger_level",
            "observed_price",
            "action",
            "reason_code",
        ),
        preserve_all_rows=True,
    )

    curve_indexes = set(_sample_indexes(len(timestamps), 1_000))
    curve_indexes.update(snapshot_indexes)
    points = []
    fields = (
        "equity",
        "drawdown",
        "gross_price_return",
        "fee_cost",
        "slippage_cost",
        "funding_return",
        "net_return",
        "gross_exposure",
        "net_exposure",
        "turnover",
    )
    for index in sorted(curve_indexes):
        row = full_returns.row(index, named=True)
        timestamp = _text(row["timestamp"])
        snapshot = snapshots.get(timestamp, {})
        point: dict[str, object] = {
            "timestamp": timestamp,
            "snapshot": timestamp if timestamp in snapshot_keys else None,
            "trade_event": bool(
                snapshot.get("trades")
                or snapshot.get("instructions")
                or snapshot.get("risk_events")
            ),
        }
        for field in fields:
            value = row.get(field)
            point[field] = None if value is None else float(value)
        points.append(point)
    preserved_trade_rows = sum(
        len(snapshot["trades"]) for snapshot in snapshots.values()
    )
    row_schemas: dict[str, list[str]] = {}
    for payload_key in (
        "positions",
        "positions_before",
        "positions_after",
        "trades",
        "rankings",
        "instructions",
        "risk_events",
    ):
        schema = next(
            (
                list(rows[0])
                for snapshot in snapshots.values()
                if (rows := snapshot[payload_key])
            ),
            [],
        )
        row_schemas[payload_key] = schema
        if not schema:
            continue
        for snapshot in snapshots.values():
            snapshot[payload_key] = [
                [row.get(field) for field in schema]
                for row in snapshot[payload_key]
            ]

    payload = {
        "limits": {
            "curve_points": 1_000,
            "event_times": None,
            "rows_per_snapshot_table": 80,
            "return_sample_rows": len(returns),
            "source_return_rows": source_return_rows,
            "preserved_event_times": len(selected_event_times),
            "preserved_trade_rows": preserved_trade_rows,
            "interactive_points": len(points),
        },
        "points": points,
        "row_schemas": row_schemas,
        "snapshots": snapshots,
    }
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).replace("</", "<\\/")



def _factor_context(
    root: Path,
    config: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    names = metadata.get("factor_names")
    name = str(names[0]) if isinstance(names, list) and names else ""
    factor_path = root / "tables" / "factor_values.parquet"
    if not name and factor_path.is_file():
        frame = (
            pl.scan_parquet(factor_path)
            .select("factor_name")
            .unique()
            .sort("factor_name")
            .collect()
        )
        if frame.height:
            name = str(frame.item(0, "factor_name"))
    definitions = _get(config, "factor", "factors", default=[])
    definitions = definitions if isinstance(definitions, list) else []
    if not name and len(definitions) == 1:
        name = str(_map(definitions[0]).get("name", ""))
    definition = next(
        (
            _map(item)
            for item in definitions
            if str(_map(item).get("name", "")) == name
        ),
        {},
    )
    return name, definition


def _factor_html(name: str, definition: Mapping[str, Any]) -> str:
    registered = FACTOR_REGISTRY.get(name)
    zh = registered.display_name_zh if registered else name or "未登记因子"
    en = registered.display_name_en if registered else name or "Unregistered factor"
    formula = (
        registered.formula
        if registered and registered.formula
        else "未在因子注册表中提供 / Not documented in factor registry"
    )
    description = (
        registered.description_zh
        if registered and registered.description_zh
        else "该因子尚未提供中文说明，请在因子注册信息中补充。"
    )
    labels = {
        "lookback": "回看窗口 / Lookback",
        "skip_recent": "跳过近期 / Skip Recent",
        "window": "滚动窗口 / Rolling Window",
        "horizon": "预测周期 / Horizon",
        "source_interval": "EMA K线周期 / EMA Candle Interval",
        "fast_span": "快速 EMA 周期 / Fast EMA Span",
        "slow_span": "慢速 EMA 周期 / Slow EMA Span",
    }
    parameters = _map(definition.get("parameters"))
    if name == "momentum":
        lookback = _text(parameters.get("lookback"))
        skip = parameters.get("skip_recent")
        formula = (
            f"close(t) / close(t-{lookback}) - 1"
            if skip is None
            else (
                f"close(t-{_text(skip)}) / "
                f"close(t-{_text(skip)}-{lookback}) - 1"
            )
        )
    elif name == "intrabar_ema_ratio":
        fast = _text(parameters.get("fast_span"))
        slow = _text(parameters.get("slow_span"))
        source = _text(parameters.get("source_interval"))
        formula = (
            f"EMA{fast}_{source}_live(t) / "
            f"EMA{slow}_{source}_live(t) - 1"
        )
    rows = [
        ("因子标识 / Factor ID", name or NA),
        ("因子版本 / Factor Version", _text(definition.get("version"))),
        ("计算频率 / Compute Interval", _text(definition.get("compute_interval"))),
    ]
    rows.extend(
        (labels.get(key, f"{key} / Parameter"), _text(parameters[key]))
        for key in sorted(parameters)
    )
    preprocess = definition.get("preprocess")
    preprocess = preprocess if isinstance(preprocess, list) else []
    names = [
        str(_map(item).get("name"))
        for item in preprocess
        if _map(item).get("name")
    ]
    translations = {
        "rank": "截面排名到 0–1 / Cross-sectional rank to 0–1",
        "zscore": "截面标准化 / Cross-sectional z-score",
        "winsorize": "截面缩尾 / Cross-sectional winsorization",
    }
    rows.append(
        (
            "截面预处理 / Preprocess",
            "；".join(translations.get(item, item) for item in names)
            if names
            else "无 / None",
        )
    )
    return f"""
<section class="card">
<div class="section-heading"><div><p class="eyebrow">FACTOR</p>
<h2>因子说明 / Factor Definition</h2></div>
<span class="pill">{html.escape(zh)} / {html.escape(en)}</span></div>
<p class="description">{html.escape(description)}</p>
<div class="formula"><span>因子公式 / Factor Formula</span>
<code>{html.escape(formula)}</code></div>
<section class="subsection parameter-grid"><h3>因子参数 / Factor Parameters</h3>
{_table(rows)}</section>
</section>"""


def _portfolio_rule(config: Mapping[str, Any]) -> str:
    portfolio = _map(_get(config, "backtest", "portfolio", default={}))
    rank_selection = _map(portfolio.get("selection"))
    if rank_selection.get("mode") == "factor_crossover":
        crossover = _map(rank_selection.get("crossover"))
        return (
            f"逐合约独立跟踪因子穿越：从不高于 "
            f"{_text(crossover.get('entry_threshold'))} 穿越到其上方时开多，"
            f"从不低于 {_text(crossover.get('exit_threshold'))} 穿越到其下方时平多；"
            "首次有效值不触发，数据缺口重置。"
        )
    if rank_selection.get("mode") == "rank_descent":
        descent = _map(rank_selection.get("descent"))
        return (
            f"跟踪每个合约从 Rank ≥ {_text(descent.get('start_rank_at_least'))} "
            f"开始的非上升路径（持平保留、Rank 数字变大重置）；首次到达 "
            f"Rank {_text(descent.get('entry_rank'))} 时做多。"
            "全账户仅持有一个合约，新信号先平旧仓再开新仓。"
        )
    if portfolio.get("construction") == "long_short_count":
        selection = (
            f"做多得分最高的 {_text(portfolio.get('long_count'))} 个合约，"
            f"做空得分最低的 {_text(portfolio.get('short_count'))} 个合约"
        )
    elif portfolio.get("construction") == "long_short_quantile":
        selection = (
            f"做多最高 {_percent(portfolio.get('long_quantile'))} 分位，"
            f"做空最低 {_percent(portfolio.get('short_quantile'))} 分位"
        )
    else:
        selection = "按照配置选择多空合约"
    weighting = {
        "equal": "等权 / Equal weight",
        "score": "按得分加权 / Score weighted",
        "inverse_volatility": "波动率倒数加权 / Inverse volatility",
    }.get(str(portfolio.get("weighting")), _text(portfolio.get("weighting")))
    return f"{selection}；{weighting}。"


def _rank_factor_label(config: Mapping[str, Any]) -> str:
    factors = _get(config, "factor", "factors", default=[])
    first = factors[0] if isinstance(factors, list) and factors else {}
    name = str(_map(first).get("name") or "factor")
    registered = FACTOR_REGISTRY.get(name)
    display = (
        registered.display_name_zh
        if registered is not None and registered.display_name_zh
        else name
    )
    return f"{display}（{name}）"


def _execution_html(config: Mapping[str, Any], actual_end: object) -> str:
    backtest = _map(config.get("backtest"))
    schedule = _map(backtest.get("schedule"))
    portfolio = _map(backtest.get("portfolio"))
    execution = _map(backtest.get("execution"))
    fee = _map(execution.get("fee"))
    slippage = _map(execution.get("slippage"))
    funding = _map(execution.get("funding"))
    valuation = _map(backtest.get("valuation"))
    risk = _map(backtest.get("risk"))
    selection_mode = _get(
        config, "backtest", "portfolio", "selection", "mode", default=""
    )
    fill = {
        "next_bar_open": "信号后下一根基础 K 线开盘价 / Next base-bar open"
    }.get(str(execution.get("fill_price")), _text(execution.get("fill_price")))
    mark = {
        "mark_close": "标记价格收盘价 / Mark-price close",
        "trade_close": "成交价格收盘价 / Trade-price close",
    }.get(str(valuation.get("price")), _text(valuation.get("price")))
    funding_text = (
        f"启用 / Enabled；缺失策略 / Missing policy: "
        f"{_text(funding.get('missing_policy'))}"
        if funding.get("enabled")
        else "关闭 / Disabled"
    )
    rows = [
        ("组合与下单规则 / Portfolio Rule", _portfolio_rule(config)),
        ("因子计算频率 / Factor Interval", _text(schedule.get("factor_interval"))),
        ("调仓频率 / Rebalance Interval", _text(schedule.get("rebalance_interval"))),
        ("基础 K 线 / Base Bar", _text(_get(config, "data", "time", "base_interval"))),
        (
            "信号延迟 / Signal Delay",
            f"{_text(schedule.get('signal_delay_bars'))} 根基础 K 线 / base bar(s)",
        ),
        ("成交位置 / Fill Location", fill),
        ("目标总敞口 / Gross Exposure", _percent(portfolio.get("gross_exposure"))),
        ("目标净敞口 / Net Exposure", _percent(portfolio.get("net_exposure"))),
        ("杠杆上限 / Leverage Limit", _text(risk.get("leverage"))),
        (
            "手续费 / Fee",
            f"{_text(fee.get('model'))}；taker "
            f"{NA if fee.get('taker_bps') is None else _number(fee.get('taker_bps'), 2) + ' bps'}",
        ),
        (
            "滑点 / Slippage",
            f"{_text(slippage.get('model'))}；"
            f"{NA if slippage.get('bps') is None else _number(slippage.get('bps'), 2) + ' bps'}",
        ),
        ("资金费率 / Funding", funding_text),
        ("持仓估值 / Position Valuation", mark),
        ("账本结束时间 / Ledger End", _text(actual_end)),
    ]
    return f"""
<section class="card"><p class="eyebrow">EXECUTION</p>
<h2>信号、下单与持仓 / Signal, Orders & Positions</h2>
<p class="strategy-line">{html.escape(_portfolio_rule(config))}</p>
<div class="flow">
<div><b>1</b><span>仅用已收盘数据计算因子<br>Closed bars only</span></div>
<div><b>2</b><span>{"逐合约检测因子穿越<br>Detect per-symbol crossings" if selection_mode == "factor_crossover" else "在合格合约中截面排序<br>Rank eligible symbols"}</span></div>
<div><b>3</b><span>下一根开盘执行调仓<br>Fill at next bar open</span></div>
<div><b>4</b><span>标记估值并计入全部成本<br>Mark and charge costs</span></div>
</div>
<section class="subsection parameter-grid"><h3>完整执行参数 / Execution Parameters</h3>
{_table(rows)}</section>
<p class="notice">持仓会持续到后续调仓将目标权重改为零或反向。当前引擎在回测边界不会
额外生成强制平仓交易；期末持仓是最后估值时刻仍在账面的持仓。 /
Positions remain until a later rebalance changes the target. No synthetic
forced-close trade is added at the backtest boundary.</p></section>"""


def _v2_audit_html(config: Mapping[str, Any]) -> str:
    """Render direct V2 semantics while keeping detailed limits collapsible."""

    backtest = _map(config.get("backtest"))
    if backtest.get("config_version") != "v2":
        return ""
    schedule = _map(backtest.get("schedule"))
    portfolio = _map(backtest.get("portfolio"))
    selection = _map(portfolio.get("selection"))
    sizing = _map(portfolio.get("sizing"))
    constraints = _map(portfolio.get("constraints"))
    risk = _map(backtest.get("risk"))
    capital = _map(backtest.get("capital"))
    long_rule = _map(selection.get("long"))
    short_rule = _map(selection.get("short"))
    descent = _map(selection.get("descent"))
    crossover = _map(selection.get("crossover"))
    holding = _map(portfolio.get("holding"))
    symbol_exits = _map(risk.get("symbol_exits"))
    enabled_risks = []
    for key, label in (
        ("stop_loss", "止损 / Stop Loss"),
        ("take_profit", "止盈 / Take Profit"),
        ("trailing_stop", "移动止损 / Trailing Stop"),
    ):
        rule = _map(symbol_exits.get(key))
        if rule.get("enabled"):
            detail = f"{label} {_percent(rule.get('distance'))}"
            if (
                key == "trailing_stop"
                and rule.get("activation_distance") is not None
            ):
                detail += f"（盈利 {_percent(rule.get('activation_distance'))} 后激活）"
            enabled_risks.append(detail)
    risk_fill = {
        "next_bar_open": "下一根 K 线开盘 / Next bar open",
        "same_bar_trigger": "触发 K 线内按保守触发价 / Same-bar conservative trigger",
    }.get(str(risk.get("fill_model")), _text(risk.get("fill_model")))
    if selection.get("mode") == "factor_crossover":
        natural = (
            f"每分钟更新{_rank_factor_label(config)}；上一分钟因子值不高于 "
            f"{_text(crossover.get('entry_threshold'))}、当前分钟升至其上方时开多，"
            f"上一分钟不低于 {_text(crossover.get('exit_threshold'))}、当前分钟降至其下方时平多。"
            "首次有效值不触发，缺口重置；各合约独立持仓，信号在下一分钟开盘执行。"
        )
        rank_rows = [
            ("选择模式 / Selection Mode", "逐合约因子穿越 / Per-symbol factor crossover"),
            ("开仓阈值 / Entry Threshold", _text(crossover.get("entry_threshold"))),
            ("平仓阈值 / Exit Threshold", _text(crossover.get("exit_threshold"))),
            ("开仓方向 / Entry Crossing", _text(crossover.get("entry_when"))),
            ("平仓方向 / Exit Crossing", _text(crossover.get("exit_when"))),
            ("初始状态 / Initial State", _text(crossover.get("initial_policy"))),
            ("缺口政策 / Gap Policy", _text(crossover.get("gap_policy"))),
        ]
    elif selection.get("mode") == "rank_descent":
        natural = (
            f"每分钟按{_rank_factor_label(config)}原始分数降序排名；合约先到 Rank ≥ "
            f"{_text(descent.get('start_rank_at_least'))}，随后 Rank 非上升并首次到达 "
            f"{_text(descent.get('entry_rank'))} 时做多。持平保留、上升重置。"
            f"仓位采用 {_text(sizing.get('mode'))}，全账户单持仓并先平后开；"
            f"风险退出采用 {risk_fill}。"
        )
        rank_rows = [
            ("序列起点 / Sequence Start", f"Rank ≥ {_text(descent.get('start_rank_at_least'))}"),
            ("触发 Rank / Entry Rank", _text(descent.get("entry_rank"))),
            ("持平处理 / Equal Rank", _text(descent.get("equal_policy"))),
            ("上升处理 / Rank Increase", _text(descent.get("increase_policy"))),
            ("发布 Rank / Published Rank", f"Top {_text(selection.get('audit_top_n'))}"),
        ]
    else:
        natural = (
            f"按 {selection.get('clock', NA)} 时钟引用 lag={selection.get('lag', 0)} "
            f"的 Rank；做多 Rank {_text(long_rule.get('ranks'))}，"
            f"做空 Rank {_text(short_rule.get('ranks'))}。"
            f"仓位采用 {_text(sizing.get('mode'))}；风险成交为 {risk_fill}。"
        )
        rank_rows = [
            ("历史 Rank 延迟 / Rank Lag", _text(selection.get("lag"))),
            ("多头 Rank / Long Ranks", _text(long_rule.get("ranks"))),
            ("空头 Rank / Short Ranks", _text(short_rule.get("ranks"))),
        ]
    selection_rows = (
        [("因子时钟 / Factor Clock", _text(selection.get("clock")))]
        if selection.get("mode") == "factor_crossover"
        else [
            ("Rank 排序 / Rank Order", "原始分数降序，最高分为 Rank 1 / Raw score DESC"),
            ("Rank 时钟 / Rank Clock", _text(selection.get("clock"))),
        ]
    )
    rows = [
        *selection_rows,
        *rank_rows,
        ("仓位模式 / Position Sizing", _text(sizing.get("mode"))),
        ("仓位比例 / Sizing Fraction", _text(sizing.get("fraction"))),
        ("固定保证金 / Fixed Margin", _text(sizing.get("margin_amount"))),
        (
            "滚仓初始保证金 / Rolling Initial Margin",
            _text(sizing.get("rolling_initial_margin")),
        ),
        (
            "滚仓重置保证金 / Rolling Reset Margin",
            _text(sizing.get("rolling_reset_margin")),
        ),
        (
            "滚仓保留区间 / Rolling Retention Range",
            f"[{_text(sizing.get('rolling_min_margin'))}, {_text(sizing.get('rolling_max_margin'))}]",
        ),
        ("持仓政策 / Holding Policy", _text(holding)),
        ("初始资金 / Initial Equity", _text(capital.get("initial_equity"))),
        ("保证金模型 / Margin Model", _text(capital.get("margin_model"))),
        ("杠杆 / Leverage", _text(risk.get("leverage"))),
        (
            "风险规则 / Risk Rules",
            "；".join(enabled_risks) if enabled_risks else "未启用 / Disabled",
        ),
        ("风险触发价格 / Risk Trigger Price", _text(risk.get("trigger_price"))),
        ("风险成交 / Risk Fill", risk_fill),
        ("跳空政策 / Gap Policy", _text(risk.get("gap_policy"))),
        ("盘中冲突 / Intrabar Conflict", _text(risk.get("intrabar_conflict"))),
        ("重新入场 / Re-entry", _text(risk.get("reentry_policy"))),
        ("敞口与资金约束 / Exposure & Capital Limits", _text(constraints)),
    ]
    return f"""
<section class="card"><p class="eyebrow">V2 AUDIT</p>
<h2>第二版策略与风险执行 / V2 Strategy & Risk Execution</h2>
<p class="strategy-line">{html.escape(natural)}</p>
<div class="flow">
<div><b>F</b><span>因子频率 / Factor<br>{html.escape(_text(schedule.get("factor_interval")))}</span></div>
<div><b>R</b><span>{"交叉状态 / Crossing State" if selection.get("mode") == "factor_crossover" else "Rank 时钟 / Rank"}<br>{html.escape(_text(selection.get("clock")))}</span></div>
<div><b>B</b><span>调仓频率 / Rebalance<br>{html.escape(_text(schedule.get("rebalance_interval")))}</span></div>
<div><b>S</b><span>风险检查 / Risk<br>{html.escape(_text(risk.get("evaluation_interval")))}</span></div>
</div>
<p class="notice">同一成交时刻先执行风险退出，再执行 universe 强制退出，最后处理定时
策略；被高优先级抑制的请求保留为零成交指令。 /
Risk exits precede universe exits and scheduled strategy intents at the same fill time;
suppressed requests remain auditable as zero-delta instructions.</p>
<section class="subsection parameter-grid"><h3>选择、仓位、保证金与风险参数 /
Selection, Sizing, Margin & Risk Parameters</h3>{_table(rows)}</section>
</section>"""



METRICS = {
    "performance": (
        "表现指标 / Performance",
        [
            ("total_return", "总收益率 / Total Return", "percent"),
            ("ending_equity", "期末净值 / Ending Equity", "number"),
            ("max_drawdown", "最大回撤 / Maximum Drawdown", "percent"),
            ("annualized_return", "年化收益率 / Annualized Return", "percent"),
            ("annualized_volatility", "年化波动率 / Annualized Volatility", "percent"),
            ("sharpe_ratio", "夏普比率 / Sharpe Ratio", "number"),
            ("sortino_ratio", "索提诺比率 / Sortino Ratio", "number"),
            ("calmar_ratio", "卡玛比率 / Calmar Ratio", "number"),
            ("hit_rate", "正收益周期占比 / Positive-period Rate", "percent"),
            ("observations", "观测数量 / Observations", "integer"),
            ("start_time", "结果开始时间 / Result Start", "text"),
            ("end_time", "结果结束时间 / Result End", "text"),
        ],
    ),
    "risk": (
        "风险与敞口 / Risk & Exposure",
        [
            ("average_gross_exposure", "平均总敞口 / Average Gross Exposure", "percent"),
            ("maximum_gross_exposure", "最大总敞口 / Maximum Gross Exposure", "percent"),
            ("average_absolute_net_exposure", "平均绝对净敞口 / Average Absolute Net Exposure", "percent"),
            ("maximum_absolute_net_exposure", "最大绝对净敞口 / Maximum Absolute Net Exposure", "percent"),
            ("average_turnover", "平均换手率 / Average Turnover", "percent"),
            ("maximum_turnover", "最大换手率 / Maximum Turnover", "percent"),
            ("total_turnover", "累计换手 / Total Turnover", "number"),
        ],
    ),
    "attribution": (
        "收益归因 / Return Attribution",
        [
            ("gross_price_contribution", "价格收益贡献 / Gross Price Contribution", "percent"),
            ("fee_contribution", "手续费拖累 / Fee Contribution", "percent"),
            ("fee_cost_amount", "手续费实际扣除 / Actual Fee Deduction", "currency"),
            ("slippage_contribution", "滑点拖累 / Slippage Contribution", "percent"),
            (
                "slippage_cost_amount",
                "滑点实际扣除 / Actual Slippage Deduction",
                "currency",
            ),
            ("funding_contribution", "资金费率贡献 / Funding Contribution", "percent"),
            ("net_contribution", "净收益贡献 / Net Contribution", "percent"),
            ("net_profit_loss_amount", "总盈利/亏损 / Total Profit & Loss", "currency"),
            ("maximum_identity_error", "收益恒等式最大误差 / Maximum Identity Error", "scientific"),
        ],
    ),
}


def _metrics_html(metrics: Mapping[str, Any], *, currency: str) -> str:
    groups = []
    for group, (title, specifications) in METRICS.items():
        values = _map(metrics.get(group))
        rows = []
        for key, label, kind in specifications:
            value = values.get(key)
            if kind == "percent":
                display = _percent(value)
            elif kind == "number":
                display = _number(value)
            elif kind == "integer":
                display = NA if value is None else f"{int(value):,}"
            elif kind == "scientific":
                display = NA if value is None else f"{float(value):.3e}"
            elif kind == "currency":
                display = _currency(value, currency)
            else:
                display = _text(value)
            rows.append((label, display))
        groups.append(f"<div class='metric-group'><h3>{title}</h3>{_table(rows)}</div>")
    return "".join(groups)


def _report_metrics(
    metrics: Mapping[str, Any],
    config: Mapping[str, Any],
    trade_stats: Mapping[str, Any],
) -> dict[str, Any]:
    """Add display-only currency amounts derived from immutable artifacts."""

    enriched = {
        group: dict(_map(values)) for group, values in metrics.items()
    }
    performance = enriched.setdefault("performance", {})
    attribution = enriched.setdefault("attribution", {})
    initial = performance.get("initial_equity")
    ending = performance.get("ending_equity")
    attribution["net_profit_loss_amount"] = (
        None
        if initial is None or ending is None
        else float(ending) - float(initial)
    )

    total_notional = trade_stats.get("total_notional")
    execution = _map(_get(config, "backtest", "execution", default={}))
    fee = _map(execution.get("fee"))
    slippage = _map(execution.get("slippage"))

    def actual_cost(cost: Mapping[str, Any], bps_field: str) -> float | None:
        if total_notional is None:
            return None
        if cost.get("model") == "zero":
            return 0.0
        bps = cost.get(bps_field)
        return (
            None
            if bps is None
            else float(total_notional) * float(bps) / 10_000.0
        )

    attribution["fee_cost_amount"] = actual_cost(fee, "taker_bps")
    attribution["slippage_cost_amount"] = actual_cost(slippage, "bps")
    return enriched


def _trade_data(root: Path) -> tuple[Mapping[str, Any], str]:
    path = root / "tables" / "trades.parquet"
    if not path.is_file():
        return {}, "<p class='empty'>未保存成交表 / Trades table was not saved.</p>"
    trades = pl.scan_parquet(path)
    stats = trades.select(
        pl.len().alias("count"),
        (pl.col("side") == "BUY").sum().alias("buys"),
        (pl.col("side") == "SELL").sum().alias("sells"),
        pl.col("symbol").n_unique().alias("symbols"),
        pl.col("fill_time").min().alias("first_fill"),
        pl.col("fill_time").max().alias("last_fill"),
        pl.col("notional").abs().sum().alias("total_notional"),
    ).collect().row(0, named=True)
    recent = (
        trades.sort(["fill_time", "sequence"], descending=[True, True])
        .limit(10)
        .collect()
    )
    rows = []
    for row in recent.iter_rows(named=True):
        side = {"BUY": "买入 / BUY", "SELL": "卖出 / SELL"}.get(
            str(row["side"]), _text(row["side"])
        )
        rows.append(
            [
                _text(row["fill_time"]),
                _text(row["symbol"]),
                side,
                _percent(row["old_weight"]),
                _percent(row["filled_weight"]),
                _number(row["reference_price"]),
                _number(row["fill_price"]),
                _number(row["notional"]),
            ]
        )
    return stats, _data_table(
        [
            "成交时间 / Fill Time",
            "合约 / Symbol",
            "方向 / Side",
            "原权重 / Old Weight",
            "成交后权重 / Filled Weight",
            "参考价 / Reference",
            "成交价 / Fill",
            "标准化名义价值 / Normalized Notional",
        ],
        rows,
    )


def _position_data(root: Path) -> tuple[object, int, str]:
    path = root / "tables" / "positions.parquet"
    if not path.is_file():
        return None, 0, "<p class='empty'>未保存持仓表 / Positions table was not saved.</p>"
    positions = pl.scan_parquet(path)
    timestamp = positions.select(pl.col("timestamp").max()).collect().item()
    if timestamp is None:
        return None, 0, "<p class='empty'>期末没有持仓 / No ending positions.</p>"
    ending = (
        positions.filter(pl.col("timestamp") == timestamp)
        .with_columns(pl.col("actual_weight").abs().alias("_absolute_weight"))
        .sort("_absolute_weight", descending=True)
        .collect()
    )
    rows = []
    for row in ending.iter_rows(named=True):
        rows.append(
            [
                _text(row["symbol"]),
                "多头 / LONG" if float(row["quantity"]) > 0 else "空头 / SHORT",
                _number(row["quantity"], 8),
                _percent(row["actual_weight"]),
                _number(row["mark_price"]),
                _number(row["signed_notional"]),
                _number(row["unrealized_pnl"], 6),
            ]
        )
    return timestamp, ending.height, _data_table(
        [
            "合约 / Symbol",
            "方向 / Position",
            "数量 / Quantity",
            "实际权重 / Actual Weight",
            "标记价格 / Mark Price",
            "标准化名义价值 / Normalized Notional",
            "未实现盈亏 / Unrealized PnL",
        ],
        rows,
    )


def _panel_stats(root: Path) -> Mapping[str, Any]:
    result: dict[str, Any] = {}
    path = root / "tables" / "universe.parquet"
    if path.is_file():
        result.update(
            pl.scan_parquet(path)
            .select(
                pl.col("symbol").n_unique().alias("symbols"),
                pl.col("symbol")
                .filter(pl.col("is_eligible"))
                .n_unique()
                .alias("eligible"),
            )
            .collect()
            .row(0, named=True)
        )
    path = root / "tables" / "factor_values.parquet"
    if path.is_file():
        result.update(
            pl.scan_parquet(path)
            .select(
                pl.len().alias("factor_rows"),
                pl.col("is_valid").sum().alias("valid_rows"),
            )
            .collect()
            .row(0, named=True)
        )
    if "symbols" not in result:
        path = root / "tables" / "rankings.parquet"
        if path.is_file():
            result.update(
                pl.scan_parquet(path)
                .select(
                    pl.col("sample_count").min().alias("ranked_min"),
                    pl.col("sample_count").max().alias("ranked_max"),
                )
                .collect()
                .row(0, named=True)
            )
    return result



def _interactive_html(payload: str) -> str:
    return (
        """
<section class="card chart-card focus-card">
<div class="chart-heading">
<div><p class="eyebrow">PRIMARY WORKSPACE</p>
<h2>净值走势与时点审计 / Equity & Point-in-time Audit</h2>
</div>
<div class="chart-legend" aria-label="图例 Chart legend">
<span><i class="legend-line"></i>账户权益 / Equity</span>
<span><i class="legend-event"></i>事件 / Event</span>
<span><i class="legend-selected"></i>已选择 / Selected</span>
</div></div>
<div class="chart-workbench">
<div class="chart-main">
<div class="chart-toolbar" aria-label="曲线与事件导航 Chart and event navigation">
<div class="toolbar-group"><span>曲线 / Curve</span>
<button type="button" data-chart-action="start">起点</button>
<button type="button" data-chart-action="worst">最大回撤</button>
<button type="button" data-chart-action="end">期末</button></div>
<div class="toolbar-group"><span>成交 / Trades</span>
<button type="button" data-chart-action="previous-trade">← 上一笔</button>
<button type="button" data-chart-action="next-trade">下一笔 →</button></div>
<div class="toolbar-group"><span>持仓 / Positions</span>
<button type="button" data-chart-action="previous-position">← 上一处</button>
<button type="button" data-chart-action="next-position">下一处 →</button></div>
<div class="toolbar-group"><span>审计点 / Audit</span>
<button type="button" data-chart-action="previous">← 上一个</button>
<button type="button" data-chart-action="next">下一个 →</button></div>
</div>
<div class="chart-shell">
<svg id="interactive-equity" viewBox="0 0 1280 650" role="img"
aria-label="账户权益曲线，纵轴单位 USDT / Equity curve in USDT"></svg>
<div id="chart-tooltip" class="chart-tooltip" hidden></div>
</div></div>
<aside class="selection-panel" aria-live="polite">
<div class="selected-heading"><div><p class="eyebrow">SELECTED POINT</p>
<h3>选中时点 / Selected Point</h3></div>
<span id="snapshot-note" class="status-chip"></span></div>
<div class="point-kpis">
<div class="point-primary point-time-card"><span>曲线时刻 / Curve Time</span><strong id="point-time">—</strong></div>
<div class="point-primary point-time-card"><span>关联审计时刻 / Audit Time</span><strong id="point-audit-time">—</strong></div>
<div class="point-primary point-equity-card"><span>账户权益 / Equity (USDT)</span><strong id="point-equity">—</strong></div>
<div><span>当期净收益 / Net Return</span><strong id="point-return">—</strong></div>
<div><span>回撤 / Drawdown</span><strong id="point-drawdown">—</strong></div>
<div><span>总敞口 / Gross Exposure</span><strong id="point-gross">—</strong></div>
<div><span>净敞口 / Net Exposure</span><strong id="point-net">—</strong></div>
<div><span>换手率 / Turnover</span><strong id="point-turnover">—</strong></div>
<div class="point-wide"><span>成本与资金费率 / Costs & Funding</span>
<strong id="point-costs">—</strong></div>
</div>

</aside></div>
<div class="snapshot-heading"><div><p class="eyebrow">SNAPSHOT</p>
<h3>事件与持仓记录 / Event & Position Record</h3></div>
<p class="muted">每笔成交均保留；成交点显示完整账本中真实相邻时点的受影响持仓状态。</p></div>
<div class="snapshot-primary-grid">
<section class="snapshot-block"><div class="snapshot-block-title">
<h4>持仓变化 / Position State</h4><span id="position-context">快照时点 / At snapshot</span></div>
<div id="snapshot-positions" class="snapshot-table-host"></div></section>
<section class="snapshot-block"><div class="snapshot-block-title">
<h4>关联成交 / Trades</h4><span>精确成交时刻</span></div>
<div id="snapshot-trades" class="snapshot-table-host"></div></section>
</div>
<div class="audit-tabs" role="tablist" aria-label="次级审计信息 Secondary audit">
<button type="button" class="active" data-snapshot-tab="rankings" role="tab"
aria-selected="true">Rank 来源 / Rank Sources</button>
<button type="button" data-snapshot-tab="instructions" role="tab"
aria-selected="false">仓位指令与抑制 / Position Instructions & Suppression</button>
<button type="button" data-snapshot-tab="risk-events" role="tab"
aria-selected="false">风险事件 / Risk Events</button>
</div>
<div class="audit-panel active" data-snapshot-panel="rankings" role="tabpanel">
<div id="snapshot-rankings" class="snapshot-table-host"></div></div>
<div class="audit-panel" data-snapshot-panel="instructions" role="tabpanel">
<div id="snapshot-instructions" class="snapshot-table-host"></div></div>
<div class="audit-panel" data-snapshot-panel="risk-events" role="tabpanel">
<div id="snapshot-risk-events" class="snapshot-table-host"></div></div>
<script id="interactive-report-data" type="application/json">"""
        + payload
        + """</script>
<script>
(function () {
  "use strict";
  var payloadNode = document.getElementById("interactive-report-data");
  var data = JSON.parse(payloadNode.textContent);
  var points = data.points || [];
  var snapshotPoints = points.filter(function (point) { return point.snapshot; });
  var svg = document.getElementById("interactive-equity");
  var tooltip = document.getElementById("chart-tooltip");
  var namespace = "http://www.w3.org/2000/svg";
  var width = 1280, height = 650, left = 112, right = 32, top = 38, bottom = 54;
  var plotWidth = width - left - right, plotHeight = height - top - bottom;
  var selectedPoint = snapshotPoints[snapshotPoints.length - 1] || null;
  function snapshotFor(point) {
    return (point && data.snapshots[point.snapshot]) || {};
  }
  var tradePoints = snapshotPoints.filter(function (point) {
    return (snapshotFor(point).trades || []).length > 0;
  });
  var positionPoints = snapshotPoints.filter(function (point) {
    var value = snapshotFor(point);
    return (value.trades || []).length > 0
      || (value.positions || []).length > 0
      || (value.positions_before || []).length > 0
      || (value.positions_after || []).length > 0;
  });

  function node(name, attributes) {
    var value = document.createElementNS(namespace, name);
    Object.keys(attributes || {}).forEach(function (key) {
      value.setAttribute(key, String(attributes[key]));
    });
    return value;
  }
  function epoch(point) { return Date.parse(point.timestamp); }
  function percent(value) {
    return value == null ? "N/A" : (value * 100).toFixed(3) + "%";
  }
  function number(value, digits) {
    return value == null ? "N/A" : Number(value).toLocaleString(
      "zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }
    );
  }
  function dateLabel(value) {
    return new Date(value).toLocaleDateString(
      "zh-CN", { month: "2-digit", day: "2-digit", timeZone: "UTC" }
    );
  }
  function setText(id, value) {
    document.getElementById(id).textContent = value;
  }
  function nearestByTime(values, target) {
    var low = 0, high = values.length - 1;
    while (low < high) {
      var middle = Math.floor((low + high) / 2);
      if (epoch(values[middle]) < target) { low = middle + 1; } else { high = middle; }
    }
    if (low > 0 && Math.abs(epoch(values[low - 1]) - target)
        <= Math.abs(epoch(values[low]) - target)) { return values[low - 1]; }
    return values[low];
  }
  function renderTable(containerId, headers, rows) {
    var container = document.getElementById(containerId);
    container.replaceChildren();
    if (!rows.length) {
      var empty = document.createElement("p");
      empty.className = "empty snapshot-empty";
      empty.textContent = "该快照无记录 / No records at this snapshot.";
      container.appendChild(empty);
      return;
    }
    var table = document.createElement("table");
    table.className = "data-table snapshot-table";
    var thead = document.createElement("thead");
    var headerRow = document.createElement("tr");
    headers.forEach(function (header) {
      var th = document.createElement("th");
      th.textContent = header;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    var tbody = document.createElement("tbody");
    rows.forEach(function (values) {
      var row = document.createElement("tr");
      values.forEach(function (value, index) {
        var td = document.createElement("td");
        td.setAttribute("data-label", headers[index]);
        td.textContent = value;
        row.appendChild(td);
      });
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    container.appendChild(table);
  }
  function snapshotRows(snapshot, key) {
    var rows = (snapshot && snapshot[key]) || [];
    var fields = (data.row_schemas || {})[key] || [];
    if (!fields.length || !rows.length || !Array.isArray(rows[0])) { return rows; }
    return rows.map(function (values) {
      var row = {};
      fields.forEach(function (field, index) { row[field] = values[index]; });
      return row;
    });
  }
  function positionSignature(rows) {
    return (rows || []).map(function (row) {
      return row.symbol + ":" + number(row.quantity, 8);
    }).sort().join("|");
  }
  function positionRows(rows, phase, timestamp) {
    return (rows || []).map(function (row) {
      return [
        phase, timestamp, row.symbol,
        row.quantity > 0 ? "多头 / LONG" : "空头 / SHORT",
        number(row.quantity, 8), percent(row.actual_weight),
        number(row.mark_price, 6), number(row.unrealized_pnl, 4)
      ];
    });
  }
  function renderSelection(point) {
    var snapshotPoint = nearestByTime(snapshotPoints, epoch(point));
    selectedPoint = point;
    var snapshot = data.snapshots[snapshotPoint.snapshot] || {
      positions: [], positions_before: [], positions_after: [],
      trades: [], rankings: [], instructions: [], risk_events: []
    };
    var trades = snapshotRows(snapshot, "trades");
    var rankings = snapshotRows(snapshot, "rankings");
    var instructions = snapshotRows(snapshot, "instructions");
    var riskEvents = snapshotRows(snapshot, "risk_events");
    var exact = point.timestamp === snapshotPoint.snapshot;
    var kind = "抽样 / Sample";
    if (rankings.length) { kind = "Rank / Rank"; }
    if (instructions.length) { kind = "指令 / Instruction"; }
    if (riskEvents.length) { kind = "风险 / Risk"; }
    if (trades.length) { kind = "成交 / Trade"; }
    if (trades.length) {
      exact = trades.some(function (row) {
        return row.fill_time === point.timestamp;
      });
    }
    setText("point-time", point.timestamp);
    setText("point-audit-time", snapshotPoint.snapshot);
    setText("point-equity", number(point.equity, 2) + " USDT");
    setText("point-return", percent(point.net_return));
    setText("point-drawdown", percent(point.drawdown));
    setText("point-gross", percent(point.gross_exposure));
    setText("point-net", percent(point.net_exposure));
    setText("point-turnover", percent(point.turnover));
    setText(
      "point-costs",
      "fee " + percent(point.fee_cost)
      + " · slip " + percent(point.slippage_cost)
      + " · funding " + percent(point.funding_return)
    );
    setText("snapshot-note", kind + (exact ? " · 精确时点" : " · 邻近账本"));
    var before = snapshotRows(snapshot, "positions_before");
    var after = snapshotRows(snapshot, "positions_after");
    var stateChanged = trades.length > 0
      || positionSignature(before) !== positionSignature(after);
    var displayedPositions;
    if (stateChanged) {
      displayedPositions = positionRows(
        before, "成交前 / Before", snapshot.position_before_time || "N/A"
      ).concat(positionRows(
        after, "成交后 / After", snapshot.position_after_time || "N/A"
      ));
      setText("position-context", "成交前后 / Before & After");
    } else {
      displayedPositions = positionRows(
        snapshotRows(snapshot, "positions"), "快照 / At", snapshotPoint.snapshot
      );
      setText("position-context", "快照时点 / At snapshot");
    }
    renderTable(
      "snapshot-positions",
      [
        "阶段 / Phase", "账本时间 / Ledger Time", "合约 / Symbol",
        "方向 / Side", "数量 / Quantity", "权重 / Weight",
        "Mark", "未实现盈亏 / Unrealized PnL"
      ],
      displayedPositions
    );
    renderTable(
      "snapshot-trades",
      [
        "成交时间 / Fill Time", "顺序 / Seq", "合约 / Symbol",
        "方向 / Side", "成交前权重 / Before", "目标权重 / Target",
        "成交后权重 / After", "成交价 / Fill", "名义价值 / Notional"
      ],
      trades.map(function (row) {
        return [
          row.fill_time, row.sequence == null ? "N/A" : String(row.sequence), row.symbol,
          row.side === "BUY" ? "买入 / BUY" : "卖出 / SELL",
          percent(row.old_weight), percent(row.target_weight),
          percent(row.filled_weight), number(row.fill_price, 6),
          number(row.notional, 2)
        ];
      })
    );
    renderTable(
      "snapshot-rankings",
      [
        "Rank 时间 / Rank Time", "合约 / Symbol", "原始分数 / Raw Score",
        "名次 / Rank", "样本数 / Sample", "百分位 / Percentile"
      ],
      rankings.map(function (row) {
        return [
          row.timestamp, row.symbol, number(row.raw_score, 6),
          row.ordinal_rank == null ? "N/A" : String(row.ordinal_rank),
          row.sample_count == null ? "N/A" : String(row.sample_count),
          number(row.percentile_rank, 6)
        ];
      })
    );
    renderTable(
      "snapshot-instructions",
      [
        "决策时间 / Decision", "Rank 来源 / Rank Source", "合约 / Symbol",
        "方向 / Side", "请求名义 / Requested", "约束后 / Constrained",
        "原因 / Reason", "优先级 / Priority", "指令 ID / Instruction ID"
      ],
      instructions.map(function (row) {
        return [
          row.decision_time, row.rank_source_time || "N/A", row.symbol, row.side,
          number(row.requested_delta_notional, 2),
          number(row.constrained_delta_notional, 2),
          row.reason_code, String(row.priority), row.instruction_id
        ];
      })
    );
    renderTable(
      "snapshot-risk-events",
      [
        "检查时间 / Evaluation", "合约 / Symbol", "事件 / Event",
        "触发线 / Trigger", "观察值 / Observed", "动作 / Action",
        "成交时间 / Fill", "原因 / Reason"
      ],
      riskEvents.map(function (row) {
        return [
          row.evaluation_time, row.symbol || "组合 / PORTFOLIO", row.event_type,
          number(row.trigger_level, 6), number(row.observed_price, 6),
          row.action, row.fill_time || "未成交 / UNFILLED", row.reason_code
        ];
      })
    );
    updateSelectedMarker(point);
  }
  function updateSelectedMarker(point) {
    var marker = document.getElementById("selected-marker");
    marker.setAttribute("cx", xScale(epoch(point)));
    marker.setAttribute("cy", yScale(point.equity));
    marker.removeAttribute("hidden");
  }

  if (!points.length || !snapshotPoints.length) {
    svg.replaceWith(document.createTextNode("没有可交互数据 / No interactive data."));
    return;
  }
  var times = points.map(epoch);
  var equities = points.map(function (point) { return point.equity; });
  var minimumTime = Math.min.apply(null, times);
  var maximumTime = Math.max.apply(null, times);
  var minimumEquity = Math.min.apply(null, equities);
  var maximumEquity = Math.max.apply(null, equities);
  var equityPadding = (maximumEquity - minimumEquity || 1) * 0.06;
  minimumEquity -= equityPadding;
  maximumEquity += equityPadding;
  function xScale(value) {
    return left + (value - minimumTime) / (maximumTime - minimumTime || 1) * plotWidth;
  }
  function yScale(value) {
    return top + (maximumEquity - value) / (maximumEquity - minimumEquity) * plotHeight;
  }

  var definitions = node("defs");
  var gradient = node("linearGradient", {
    id: "equity-area", x1: "0", x2: "0", y1: "0", y2: "1"
  });
  gradient.appendChild(node("stop", {
    offset: "0%", "stop-color": "#238c72", "stop-opacity": ".20"
  }));
  gradient.appendChild(node("stop", {
    offset: "100%", "stop-color": "#238c72", "stop-opacity": "0"
  }));
  definitions.appendChild(gradient);
  svg.appendChild(definitions);
  svg.appendChild(node("rect", {
    x: 0, y: 0, width: width, height: height, rx: 16, fill: "#ffffff"
  }));
  var axisTitle = node("text", {
    x: left, y: 18, fill: "#68776f", "font-size": 14
  });
  axisTitle.textContent = "账户权益 / Equity (USDT)";
  svg.appendChild(axisTitle);
  for (var grid = 0; grid <= 4; grid += 1) {
    var y = top + grid / 4 * plotHeight;
    svg.appendChild(node("line", {
      x1: left, y1: y, x2: width - right, y2: y,
      stroke: "#d9e2dd", "stroke-width": 1
    }));
    var label = node("text", {
      x: left - 14, y: y + 4, fill: "#68776f",
      "font-size": 14, "text-anchor": "end"
    });
    label.textContent = number(
      maximumEquity - grid / 4 * (maximumEquity - minimumEquity), 0
    );
    svg.appendChild(label);
  }
  for (var timeGrid = 0; timeGrid <= 4; timeGrid += 1) {
    var tickTime = minimumTime + timeGrid / 4 * (maximumTime - minimumTime);
    var tickX = xScale(tickTime);
    if (timeGrid > 0 && timeGrid < 4) {
      svg.appendChild(node("line", {
        x1: tickX, y1: top, x2: tickX, y2: height - bottom,
        stroke: "#edf1ee", "stroke-width": 1
      }));
    }
    var tickLabel = node("text", {
      x: tickX, y: height - 22, fill: "#68776f", "font-size": 14,
      "text-anchor": timeGrid === 0 ? "start" : (timeGrid === 4 ? "end" : "middle")
    });
    tickLabel.textContent = dateLabel(tickTime);
    svg.appendChild(tickLabel);
  }
  var linePath = points.map(function (point, index) {
    return (index ? "L" : "M") + xScale(epoch(point)).toFixed(2)
      + "," + yScale(point.equity).toFixed(2);
  }).join(" ");
  var areaPath = linePath
    + " L" + xScale(maximumTime).toFixed(2) + "," + (height - bottom)
    + " L" + xScale(minimumTime).toFixed(2) + "," + (height - bottom) + " Z";
  svg.appendChild(node("path", { d: areaPath, fill: "url(#equity-area)" }));
  svg.appendChild(node("path", {
    d: linePath, fill: "none", stroke: "#238c72", "stroke-width": 2.25,
    "stroke-linecap": "round", "stroke-linejoin": "round"
  }));
  points.filter(function (point) { return point.trade_event; }).forEach(function (point) {
    svg.appendChild(node("circle", {
      cx: xScale(epoch(point)), cy: yScale(point.equity), r: 3.2,
      fill: "#cb8930", stroke: "#ffffff", "stroke-width": 1.5,
      class: "event-marker"
    }));
  });
  var crosshair = node("line", {
    x1: left, y1: top, x2: left, y2: height - bottom,
    stroke: "#75837c", "stroke-width": 1, "stroke-dasharray": "3 5", hidden: true
  });
  var hoverMarker = node("circle", {
    cx: left, cy: top, r: 4.5, fill: "#ffffff", stroke: "#238c72",
    "stroke-width": 2, hidden: true
  });
  var selectedMarker = node("circle", {
    id: "selected-marker", cx: left, cy: top, r: 7, fill: "#ffffff",
    stroke: "#cb8930", "stroke-width": 2.5, hidden: true
  });
  svg.appendChild(crosshair);
  svg.appendChild(hoverMarker);
  svg.appendChild(selectedMarker);
  var overlay = node("rect", {
    x: left, y: top, width: plotWidth, height: plotHeight,
    fill: "transparent", class: "chart-overlay"
  });
  svg.appendChild(overlay);

  function pointFromEvent(event) {
    var box = svg.getBoundingClientRect();
    var x = (event.clientX - box.left) / box.width * width;
    var target = minimumTime + (x - left) / plotWidth * (maximumTime - minimumTime);
    return nearestByTime(points, target);
  }
  overlay.addEventListener("mousemove", function (event) {
    var point = pointFromEvent(event);
    var x = xScale(epoch(point)), y = yScale(point.equity);
    crosshair.setAttribute("x1", x); crosshair.setAttribute("x2", x);
    crosshair.removeAttribute("hidden");
    hoverMarker.setAttribute("cx", x); hoverMarker.setAttribute("cy", y);
    hoverMarker.removeAttribute("hidden");
    tooltip.hidden = false;
    tooltip.textContent = point.timestamp + " · " + number(point.equity, 2)
      + " USDT · Return " + percent(point.net_return)
      + " · DD " + percent(point.drawdown);
    var shellWidth = svg.parentElement.clientWidth;
    tooltip.style.left = Math.max(8, Math.min(event.offsetX + 14, shellWidth - 330)) + "px";
    tooltip.style.top = Math.max(event.offsetY - 42, 8) + "px";
  });
  overlay.addEventListener("mouseleave", function () {
    crosshair.setAttribute("hidden", "true");
    hoverMarker.setAttribute("hidden", "true");
    tooltip.hidden = true;
  });
  overlay.addEventListener("click", function (event) {
    renderSelection(pointFromEvent(event));
  });
  function moveTo(values, direction) {
    if (!values.length) { return; }
    var current = epoch(selectedPoint || values[0]);
    var target = direction < 0 ? values[0] : values[values.length - 1];
    if (direction < 0) {
      for (var previousIndex = values.length - 1; previousIndex >= 0; previousIndex -= 1) {
        if (epoch(values[previousIndex]) < current) {
          target = values[previousIndex]; break;
        }
      }
    } else {
      for (var nextIndex = 0; nextIndex < values.length; nextIndex += 1) {
        if (epoch(values[nextIndex]) > current) {
          target = values[nextIndex]; break;
        }
      }
    }
    renderSelection(target);
  }
  document.querySelectorAll("[data-chart-action]").forEach(function (button) {
    var action = button.getAttribute("data-chart-action");
    if (action.indexOf("trade") >= 0 && !tradePoints.length) { button.disabled = true; }
    if (action.indexOf("position") >= 0 && !positionPoints.length) { button.disabled = true; }
    button.addEventListener("click", function () {
      if (action === "start") { renderSelection(points[0]); return; }
      if (action === "end") { renderSelection(points[points.length - 1]); return; }
      if (action === "previous") { moveTo(snapshotPoints, -1); return; }
      if (action === "next") { moveTo(snapshotPoints, 1); return; }
      if (action === "previous-trade") { moveTo(tradePoints, -1); return; }
      if (action === "next-trade") { moveTo(tradePoints, 1); return; }
      if (action === "previous-position") { moveTo(positionPoints, -1); return; }
      if (action === "next-position") { moveTo(positionPoints, 1); return; }
      if (action === "worst") {
        var worst = points.reduce(function (current, point) {
          return point.drawdown < current.drawdown ? point : current;
        }, points[0]);
        renderSelection(worst);
      }
    });
  });
  document.querySelectorAll("[data-snapshot-tab]").forEach(function (button) {
    button.addEventListener("click", function () {
      var target = button.getAttribute("data-snapshot-tab");
      document.querySelectorAll("[data-snapshot-tab]").forEach(function (item) {
        var active = item === button;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", active ? "true" : "false");
      });
      document.querySelectorAll("[data-snapshot-panel]").forEach(function (panel) {
        panel.classList.toggle(
          "active", panel.getAttribute("data-snapshot-panel") === target
        );
      });
    });
  });
  renderSelection(snapshotPoints[snapshotPoints.length - 1]);
}());
</script></section>
"""
    )


def render_report_from_artifacts(
    run_directory: Path,
    *,
    output_path: Path | None = None,
) -> Path:
    """Read immutable run artifacts and write deterministic standalone HTML."""

    root = run_directory.resolve()
    try:
        metrics = _map(json.loads((root / "metrics.json").read_text("utf-8")))
        metadata = _map(
            json.loads((root / "run_metadata.json").read_text("utf-8"))
        )
        config = _map(
            json.loads((root / "resolved_config.json").read_text("utf-8"))
        )
        returns, source_return_rows = _bounded_returns(root)
        trade_stats, trade_table = _trade_data(root)
        report_metrics = _report_metrics(metrics, config, trade_stats)
        ending_time, position_count, position_table = _position_data(root)
        panel = _panel_stats(root)
        interactive_payload = _interactive_payload(
            root,
            returns,
            source_return_rows=source_return_rows,
        )
    except (OSError, json.JSONDecodeError, pl.exceptions.PolarsError) as exc:
        raise ReportError(f"cannot read report artifacts: {exc}") from exc
    run_id = str(metadata.get("run_id", ""))
    if not run_id:
        raise ReportError("run_metadata.json is missing run_id")
    if returns.is_empty():
        raise ReportError("returns artifact is empty")

    timestamps = returns["timestamp"].to_list()
    factor_name, definition = _factor_context(root, config, metadata)
    run = _map(_get(config, "backtest", "run", default={}))
    market = _map(_get(config, "data", "market", default={}))
    data_time = _map(_get(config, "data", "time", default={}))
    performance = _map(metrics.get("performance"))
    capital = _map(_get(config, "backtest", "capital", default={}))
    engine = _map(_get(config, "backtest", "engine", default={}))
    account_currency = str(capital.get("currency") or "USDT")
    warnings = []
    warning_path = root / "warnings.json"
    if warning_path.is_file():
        try:
            value = json.loads(warning_path.read_text("utf-8"))
            warnings = value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportError(f"cannot read report warnings: {exc}") from exc

    configured_range = f"[{_text(run.get('start'))}, {_text(run.get('end'))})"
    actual_range = f"{_text(timestamps[0])} → {_text(timestamps[-1])}"
    coverage = (
        _percent(float(panel["valid_rows"]) / float(panel["factor_rows"]))
        if panel.get("factor_rows")
        else NA
    )
    symbol_summary = (
        f"{_text(panel.get('eligible'))} eligible / "
        f"{_text(panel.get('symbols'))} total"
        if panel.get("symbols") is not None
        else (
            f"{_text(panel.get('ranked_min'))}–"
            f"{_text(panel.get('ranked_max'))} ranked per minute / "
            f"{_text(panel.get('ranked_max'))} maximum"
            if panel.get("ranked_max") is not None
            else f"{NA} eligible / {NA} total"
        )
    )
    overview = [
        ("运行名称 / Run Name", _text(run.get("name"))),
        ("正式运行 ID / Run ID", run_id),
        ("数据集 / Dataset", _text(metadata.get("dataset_id"))),
        ("数据版本 / Dataset Version", _text(metadata.get("dataset_version"))),
        (
            "市场 / Market",
            f"{_text(market.get('venue'))} / {_text(market.get('segment'))} / "
            f"{_text(market.get('contract_type'))}",
        ),
        (
            "计价与保证金 / Quote & Margin",
            f"{_text(market.get('quote_asset'))} / {_text(market.get('margin_asset'))}",
        ),
        ("配置回测区间 / Configured Range", configured_range),
        ("实际账本区间 / Actual Ledger Range", actual_range),
        ("区间语义 / Range Semantics", _text(data_time.get("range_semantics"))),
        ("时区 / Timezone", _text(data_time.get("timezone"))),
        ("执行模式 / Execution Mode", _text(metadata.get("execution_mode"))),
        (
            "来源矩阵研究 / Source Matrix Research",
            _text(engine.get("source_matrix_run_id")),
        ),
        (
            "逐时点等价审计 / Equivalence Audit",
            "required" if engine.get("equivalence_audit") else "not requested",
        ),
        (
            "合约数量 / Symbols",
            symbol_summary,
        ),
        ("有效因子覆盖 / Valid Factor Coverage", coverage),
    ]
    summary = [
        ("总收益率", "Total Return", _percent(performance.get("total_return"))),
        ("最大回撤", "Maximum Drawdown", _percent(performance.get("max_drawdown"))),
        ("夏普比率", "Sharpe Ratio", _number(performance.get("sharpe_ratio"))),
        ("成交笔数", "Trades", _text(trade_stats.get("count"))),
    ]
    cards = "".join(
        f"<div class='kpi'><span>{zh}<small>{en}</small></span>"
        f"<strong>{html.escape(value)}</strong></div>"
        for zh, en, value in summary
    )
    trade_summary = [
        ("成交总数 / Total Trades", _text(trade_stats.get("count"))),
        ("买入 / Buy Trades", _text(trade_stats.get("buys"))),
        ("卖出 / Sell Trades", _text(trade_stats.get("sells"))),
        ("交易合约数 / Traded Symbols", _text(trade_stats.get("symbols"))),
        ("第一笔成交 / First Fill", _text(trade_stats.get("first_fill"))),
        ("最后一笔成交 / Last Fill", _text(trade_stats.get("last_fill"))),
        ("期末持仓数 / Ending Positions", str(position_count)),
        ("期末估值时刻 / Ending Position Time", _text(ending_time)),
    ]
    warning_html = (
        "<ul>"
        + "".join(f"<li>{html.escape(_text(item))}</li>" for item in warnings)
        + "</ul>"
        if warnings
        else "<p class='empty'>无警告 / No warnings.</p>"
    )

    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>bianbt 回测报告 / Backtest Report {html.escape(run_id)}</title>
<style>
:root{{--bg:#f3f5f2;--panel:#ffffff;--panel2:#f7f9f7;--panel3:#edf3ef;
--line:#d3ddd7;--line-soft:#e3e9e5;--text:#1b2722;--muted:#68776f;
--green:#238c72;--green-deep:#e0f1ea;--amber:#cb8930;--blue:#3f709f}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:linear-gradient(180deg,#f8faf7 0,#f2f5f1 100%);
color:var(--text);font:14px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans SC",sans-serif}}
button{{font:inherit}}main{{width:min(1660px,calc(100% - 32px));margin:0 auto;padding:22px 0 54px}}
h1{{font-size:clamp(26px,3vw,40px);line-height:1.08;margin:5px 0}}h2{{font-size:20px;margin:3px 0 14px}}
h3{{font-size:16px;margin:0}}h4{{font-size:14px;margin:0}}.subtitle,.muted{{color:var(--muted)}}
.eyebrow{{margin:0;color:var(--green);font-size:10px;font-weight:760;letter-spacing:.15em}}
.run-id{{font-family:ui-monospace,monospace;color:var(--blue)}}.report-header{{display:flex;
align-items:flex-end;justify-content:space-between;gap:24px;padding:6px 4px 13px}}
.header-context{{display:grid;grid-template-columns:repeat(2,minmax(170px,auto));gap:8px 22px;
color:var(--muted);font-size:12px}}.header-context span{{display:block;color:var(--text);font-size:13px}}
.report-nav{{position:sticky;top:0;z-index:20;display:flex;gap:6px;padding:8px;margin-bottom:10px;align-items:center;justify-content:space-between;
background:rgba(248,250,247,.94);border:1px solid var(--line);border-radius:13px;
backdrop-filter:blur(14px)}}.report-nav button,.chart-toolbar button,.audit-tabs button{{border:1px solid
transparent;background:transparent;color:var(--muted);border-radius:9px;padding:8px 13px;cursor:pointer}}
.report-nav button:hover,.chart-toolbar button:hover,.audit-tabs button:hover{{color:var(--text);background:var(--panel3)}}
.report-nav button.active{{color:#fff;background:var(--green);font-weight:720}}
.report-tabs{{display:flex;gap:5px;min-width:max-content}}.nav-kpis{{display:grid;grid-template-columns:repeat(4,minmax(105px,1fr));gap:0;margin-left:auto}}
.nav-kpis .kpi{{display:flex;align-items:baseline;justify-content:space-between;gap:8px;padding:2px 12px;background:transparent;border:0;border-left:1px solid var(--line-soft);border-radius:0}}
.nav-kpis .kpi span{{font-size:10px;white-space:nowrap}}.nav-kpis .kpi small{{display:none}}.nav-kpis .kpi strong{{font-size:14px;margin:0;color:var(--text);white-space:nowrap}}
.report-view{{display:none}}.report-view.active{{display:block}}
.local-nav{{display:flex;gap:5px;margin:0 0 10px;padding:5px;background:#fff;border:1px solid var(--line);border-radius:11px}}
.local-nav button{{border:0;background:transparent;color:var(--muted);border-radius:8px;padding:8px 13px;cursor:pointer}}
.local-nav button:hover{{background:var(--panel3);color:var(--text)}}.local-nav button.active{{background:var(--green-deep);color:#276f5e;font-weight:700}}
.local-panel{{display:none}}.local-panel.active{{display:block}}.local-panel.card{{margin:0}}.kpis{{display:grid;
grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:0 0 12px}}
.kpi,.card{{background:var(--panel);border:1px solid var(--line-soft);border-radius:14px}}
.kpi{{padding:15px 17px}}.kpi span{{display:block;color:var(--muted);font-size:12px}}
.kpi small{{display:block}}.kpi strong{{display:block;margin-top:6px;font-size:22px;font-weight:650}}
.card{{padding:20px;margin:12px 0;overflow:hidden}}.section-heading,.chart-heading,.snapshot-heading,
.selected-heading{{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}}
.pill,.status-chip{{background:var(--green-deep);color:#276f5e;border:1px solid #b9dacf;
border-radius:999px;padding:5px 10px;height:max-content;font-size:11px;white-space:nowrap}}
.description{{font-size:15px;max-width:900px}}.formula{{background:var(--panel2);border-left:3px solid
var(--green);padding:13px 15px;margin:15px 0}}.formula span{{display:block;color:var(--muted);font-size:11px}}
.formula code{{display:block;margin-top:5px;color:#276f5e;white-space:normal;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:8px 10px;border-bottom:1px solid var(--line-soft);
text-align:left;vertical-align:top;overflow-wrap:anywhere}}.definition th{{color:var(--muted);width:38%}}
.metric-groups{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}}
.metric-group{{background:var(--panel2);border-radius:11px;padding:12px}}.flow{{display:grid;
grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:15px 0}}.flow div{{background:var(--panel2);
border:1px solid var(--line-soft);border-radius:10px;padding:11px;display:flex;gap:9px}}
.flow b{{display:grid;place-items:center;flex:0 0 24px;height:24px;border-radius:50%;background:var(--green-deep);
color:#276f5e}}.flow span{{font-size:12px}}.notice{{border:1px solid #ead9b7;background:#fff8ea;
color:#795c28;border-radius:10px;padding:11px 13px}}.table-wrap,.snapshot-table-host{{width:100%;overflow:visible}}
.data-table{{width:100%;table-layout:auto}}.data-table th{{color:var(--muted);font-size:11px;white-space:normal}}
.data-table td{{white-space:normal;font-variant-numeric:tabular-nums;font-size:12px}}
.empty{{color:var(--muted);font-style:italic}}details.card{{padding:0}}details.card>summary{{cursor:pointer;
padding:17px 19px;font-size:15px;font-weight:680;list-style:none}}details.card>summary::-webkit-details-marker{{display:none}}
details.card>summary::after{{content:"＋";float:right;color:var(--green)}}details.card[open]>summary::after{{content:"－"}}
details.card[open]>summary{{border-bottom:1px solid var(--line-soft)}}.details-body{{padding:18px 19px}}
.subsection{{margin-top:14px;padding-top:14px;border-top:1px solid var(--line-soft)}}
.subsection>h3{{margin-bottom:9px}}.strategy-line{{font-size:16px}}
.parameter-grid .definition tbody,.overview-grid .definition tbody{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}}
.parameter-grid .definition tr,.overview-grid .definition tr{{display:grid;grid-template-columns:minmax(150px,38%) minmax(0,1fr);background:var(--panel2);border:1px solid var(--line-soft);border-radius:8px}}
.parameter-grid .definition th,.parameter-grid .definition td,.overview-grid .definition th,.overview-grid .definition td{{width:auto;border:0;padding:7px 9px}}
.content-divider{{margin:18px 0 8px;padding-top:14px;border-top:1px solid var(--line-soft)}}
.focus-card{{padding:14px}}.chart-heading{{align-items:flex-end}}
.chart-legend{{display:flex;flex-wrap:wrap;gap:14px;color:var(--muted);font-size:12px;white-space:nowrap}}
.chart-legend span{{display:flex;align-items:center;gap:6px}}.chart-legend i{{display:inline-block}}
.legend-line{{width:20px;height:2px;background:var(--green)}}.legend-event{{width:7px;height:7px;border-radius:50%;
background:var(--amber)}}.legend-selected{{width:9px;height:9px;border:2px solid var(--amber);border-radius:50%}}
.chart-workbench{{display:grid;grid-template-columns:minmax(0,1fr) 460px;gap:10px;margin-top:8px;align-items:stretch}}
.chart-main,.selection-panel,.snapshot-block{{min-width:0;background:var(--panel2);border:1px solid var(--line-soft);
border-radius:12px}}.chart-main{{padding:8px}}.chart-toolbar{{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 8px}}
.toolbar-group{{display:flex;align-items:center;gap:3px;padding:3px;background:#fff;border:1px solid var(--line-soft);border-radius:9px}}
.toolbar-group>span{{padding:0 5px;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.06em}}
.chart-toolbar button{{padding:6px 9px;font-size:11px;border-color:transparent;background:var(--panel2)}}
.chart-toolbar button:disabled{{opacity:.35;cursor:not-allowed}}.chart-shell{{position:relative;
min-height:330px}}svg{{display:block;width:100%;height:auto;min-height:330px}}.chart-tooltip{{position:absolute;
z-index:5;max-width:320px;background:#ffffff;border:1px solid #aebdb5;border-radius:8px;padding:7px 9px;
font-size:11px;pointer-events:none;box-shadow:0 8px 28px rgba(31,51,42,.14)}}.chart-overlay{{cursor:crosshair}}
.selection-panel{{display:flex;flex-direction:column;padding:14px;align-self:stretch}}
.point-kpis{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-rows:minmax(58px,1fr);
flex:1;gap:8px;margin:11px 0 0}}.point-kpis>div{{background:var(--panel3);border:1px solid var(--line-soft);
border-radius:9px;padding:11px}}.point-kpis .point-time-card{{grid-column:span 2}}
.point-kpis .point-wide{{grid-column:span 2}}.point-kpis span{{display:block;color:var(--muted);font-size:11px;line-height:1.35}}
.point-kpis strong{{display:block;font-size:14px;margin-top:2px;overflow-wrap:anywhere}}
.point-kpis .point-primary strong{{font-size:15px}}.selected-heading h3{{font-size:18px}}
.snapshot-heading{{align-items:flex-end;
margin:11px 2px 7px}}.snapshot-heading p{{margin:0}}.snapshot-primary-grid{{display:grid;
grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}}.snapshot-block{{padding:10px}}
.snapshot-block-title{{display:flex;justify-content:space-between;gap:10px;margin-bottom:8px}}
.snapshot-block-title span{{color:var(--muted);font-size:10px}}.snapshot-empty{{margin:12px 2px}}
.audit-tabs{{display:flex;gap:5px;margin-top:12px;border-bottom:1px solid var(--line-soft)}}
.audit-tabs button{{border-radius:8px 8px 0 0;font-size:11px}}.audit-tabs button.active{{color:var(--text);
background:var(--panel3);border-color:var(--line-soft);border-bottom-color:var(--panel3)}}
.audit-panel{{display:none;padding:12px;background:var(--panel3);border:1px solid var(--line-soft);
border-top:0;border-radius:0 0 10px 10px}}.audit-panel.active{{display:block}}.snapshot-marker{{pointer-events:none}}
.secondary-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}}
.secondary-grid>.card{{margin:0}}footer{{color:var(--muted);font-size:11px;margin-top:18px;padding:0 4px}}
@media(max-width:1180px){{.chart-workbench{{grid-template-columns:1fr}}.selection-panel{{width:100%;height:auto}}
.selection-panel>.point-kpis{{grid-template-columns:repeat(6,minmax(0,1fr))}}
.point-kpis .point-time-card,.point-kpis .point-wide{{grid-column:span 2}}}}
@media(max-width:900px){{main{{width:min(100% - 20px,1660px);padding-top:12px}}.report-header{{display:block}}
.header-context{{margin-top:12px}}.kpis,.metric-groups,.flow,.secondary-grid{{grid-template-columns:1fr 1fr}}
.parameter-grid .definition tbody,.overview-grid .definition tbody{{grid-template-columns:1fr}}
.snapshot-primary-grid{{grid-template-columns:1fr}}.chart-heading{{display:block}}.chart-legend{{margin-top:10px}}
.selection-panel{{display:block}}.selection-panel>.point-kpis{{margin:11px 0}}}}
@media(max-width:680px){{main{{width:calc(100% - 12px)}}h1{{font-size:27px}}.report-header{{padding:8px 4px 14px}}
.header-context,.kpis,.metric-groups,.flow,.secondary-grid{{grid-template-columns:1fr}}.report-nav{{display:block;border-radius:10px}}
.report-tabs,.local-nav{{overflow-x:auto}}.local-nav button{{flex:0 0 auto}}.report-nav button{{flex:0 0 auto}}.nav-kpis{{grid-template-columns:repeat(2,1fr);margin:7px 0 0}}
.nav-kpis .kpi{{border-top:1px solid var(--line-soft);border-left:0;padding:4px 8px}}.card{{padding:14px;border-radius:11px}}
.focus-card{{padding:12px}}.chart-toolbar{{overflow-x:auto;flex-wrap:nowrap}}.chart-toolbar button{{flex:0 0 auto}}
.chart-shell,svg{{min-height:230px}}.point-kpis{{grid-template-columns:1fr 1fr}}.audit-tabs{{overflow-x:auto}}
.audit-tabs button{{flex:0 0 auto}}.data-table thead{{display:none}}.data-table,.data-table tbody,
.data-table tr,.data-table td{{display:block;width:100%}}.data-table tr{{padding:7px 0;border-bottom:1px solid var(--line)}}
.data-table td{{display:grid;grid-template-columns:minmax(118px,40%) minmax(0,1fr);gap:8px;border:0;
padding:4px 3px;text-align:right}}.data-table td::before{{content:attr(data-label);color:var(--muted);
font-size:10px;text-align:left}}.definition tr{{display:grid;grid-template-columns:1fr}}.definition th,
.definition td{{display:block;width:100%}}.definition th{{padding-bottom:2px;border-bottom:0}}.definition td{{padding-top:2px}}
.section-heading,.snapshot-heading{{display:block}}.pill{{display:inline-block;margin-top:8px}}}}
@media print{{body{{background:#fff;color:#111}}main{{width:100%;padding:0}}.report-nav{{display:none}}
.report-view,.local-panel{{display:block!important}}.card,.kpi{{break-inside:avoid;background:#fff;border-color:#ccc}}}}
</style></head><body><main>
<header class="report-header"><div><p class="eyebrow">BIANBT · {REPORT_VERSION}</p>
<h1>回测报告 / Backtest Report</h1>
<p class="subtitle">运行编号 / Run ID <span class="run-id">{html.escape(run_id)}</span></p></div>
<div class="header-context"><div>配置区间 / Range<span>{html.escape(configured_range)}</span></div>
<div>数据集 / Dataset<span>{html.escape(_text(metadata.get('dataset_id')))}</span></div></div></header>
<nav class="report-nav" aria-label="报告页面 / Report pages">
<div class="report-tabs"><button type="button" class="active" data-report-target="analysis" aria-selected="true">走势与交易 / Analysis</button>
<button type="button" data-report-target="strategy" aria-selected="false">策略说明 / Strategy</button>
<button type="button" data-report-target="details" aria-selected="false">完整记录 / Details</button></div>
<div class="nav-kpis">{cards}</div>
</nav>
<section class="report-view active" data-report-view="analysis">
{_interactive_html(interactive_payload)}
</section>
<section class="report-view" data-report-view="strategy">
<div class="local-workspace">
<nav class="local-nav" aria-label="策略说明分类 / Strategy sections">
<button type="button" class="active" data-local-target="factor" aria-selected="true">因子定义 / Factor</button>
<button type="button" data-local-target="execution" aria-selected="false">执行规则 / Execution</button>
<button type="button" data-local-target="risk" aria-selected="false">仓位与风险 / Position & Risk</button>
</nav>
<div class="local-panel active" data-local-panel="factor">{_factor_html(factor_name, definition)}</div>
<div class="local-panel" data-local-panel="execution">{_execution_html(config, timestamps[-1])}</div>
<div class="local-panel" data-local-panel="risk">{_v2_audit_html(config)}</div>
</div></section>
<section class="report-view" data-report-view="details">
<div class="local-workspace">
<nav class="local-nav" aria-label="完整记录分类 / Detail sections">
<button type="button" class="active" data-local-target="overview" aria-selected="true">运行概览 / Overview</button>
<button type="button" data-local-target="metrics" aria-selected="false">表现指标 / Metrics</button>
<button type="button" data-local-target="trades" aria-selected="false">成交记录 / Trades</button>
<button type="button" data-local-target="positions" aria-selected="false">期末持仓 / Positions</button>
<button type="button" data-local-target="warnings" aria-selected="false">运行警告 / Warnings</button>
</nav>
<section class="local-panel active card overview-grid" data-local-panel="overview">
<p class="eyebrow">OVERVIEW</p><h2>数据与运行概览 / Data & Run Overview</h2>{_table(overview)}</section>
<section class="local-panel card" data-local-panel="metrics">
<p class="eyebrow">METRICS</p><h2>全部表现指标 / All Performance Metrics</h2>
<div class="metric-groups">{_metrics_html(report_metrics, currency=account_currency)}</div></section>
<section class="local-panel card" data-local-panel="trades">
<p class="eyebrow">TRADES</p><h2>成交详情 / Trade Details</h2>{_table(trade_summary)}
<h3 class="content-divider">最近十笔成交 / Latest 10 Trades</h3>{trade_table}</section>
<section class="local-panel card" data-local-panel="positions">
<p class="eyebrow">POSITIONS</p><h2>期末持仓 / Ending Positions</h2>
<p class="muted">最后估值时刻仍在账面的持仓；没有额外执行强制平仓。 /
Positions remaining at the final valuation timestamp; no forced close is implied.</p>
{position_table}</section>
<section class="local-panel card" data-local-panel="warnings">
<p class="eyebrow">WARNINGS</p><h2>运行警告 / Run Warnings</h2>{warning_html}</section>
</div></section>
<footer>由不可变运行产物确定性生成。收益不构成投资建议。 /
Deterministically generated from immutable run artifacts. Not investment advice.</footer>
<script>
(function () {{
  "use strict";
  var buttons = document.querySelectorAll("[data-report-target]");
  var views = document.querySelectorAll("[data-report-view]");
  function activate(name, updateHash) {{
    var exists = false;
    views.forEach(function (view) {{
      var active = view.getAttribute("data-report-view") === name;
      view.classList.toggle("active", active);
      if (active) {{ exists = true; }}
    }});
    if (!exists) {{ name = "analysis"; return activate(name, updateHash); }}
    buttons.forEach(function (button) {{
      var active = button.getAttribute("data-report-target") === name;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    }});
    if (updateHash && window.history && window.history.replaceState) {{
      window.history.replaceState(null, "", "#" + name);
    }}
    window.scrollTo({{ top: 0, behavior: "instant" }});
  }}
  buttons.forEach(function (button) {{
    button.addEventListener("click", function () {{
      activate(button.getAttribute("data-report-target"), true);
    }});
  }});
  document.querySelectorAll(".local-workspace").forEach(function (workspace) {{
    var localButtons = workspace.querySelectorAll("[data-local-target]");
    var localPanels = workspace.querySelectorAll("[data-local-panel]");
    localButtons.forEach(function (button) {{
      button.addEventListener("click", function () {{
        var target = button.getAttribute("data-local-target");
        localButtons.forEach(function (item) {{
          var active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
        }});
        localPanels.forEach(function (panel) {{
          panel.classList.toggle(
            "active", panel.getAttribute("data-local-panel") === target
          );
        }});
      }});
    }});
  }});
  activate(window.location.hash.slice(1) || "analysis", false);
}}());
</script></main></body></html>
"""
    destination = (output_path or root / "report.html").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")
    return destination
