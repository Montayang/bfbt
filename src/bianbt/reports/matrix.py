"""Readable reports for immutable Fast Matrix research runs."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping

import polars as pl

from bianbt.engine.fast_matrix.result import MatrixResult
from bianbt.engine.fast_matrix.target_schedule import TargetSchedule


MATRIX_REPORT_VERSION = "matrix-report/v2"


def _nested(source: Mapping[str, object], *path: str, default: object = "—") -> object:
    value: object = source
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return default
        value = value[key]
    return value


def _number(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{digits}f}"


def _percent(value: object, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}%}"


def _text(value: object) -> str:
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(str(value))


def matrix_report_metrics(
    result: MatrixResult,
    schedule: TargetSchedule,
    resolved_config: Mapping[str, object],
) -> dict[str, object]:
    """Derive compact, reproducible performance and execution metrics."""
    returns = result.returns
    rebalances = result.rebalance_summary
    initial_equity = float(_nested(resolved_config, "capital", "initial_equity", default=0.0))
    total_return = (
        float(returns.select((pl.col("net_return") + 1.0).product()).item()) - 1.0
        if returns.height
        else 0.0
    )
    gross_price_return = (
        float(returns.select((pl.col("gross_price_return") + 1.0).product()).item()) - 1.0
        if returns.height
        else 0.0
    )
    fee_amount = float(rebalances["fee_amount"].sum()) if rebalances.height else 0.0
    slippage_amount = (
        float(rebalances["slippage_amount"].sum()) if rebalances.height else 0.0
    )
    if returns.height:
        previous_equity = returns.select(
            (pl.col("equity") / (pl.col("net_return") + 1.0)).alias("previous_equity")
        )
        funding_amount = float(
            returns.with_columns(previous_equity["previous_equity"])
            .select((pl.col("funding_return") * pl.col("previous_equity")).sum())
            .item()
        )
        start = returns["timestamp"].min()
        end = returns["timestamp"].max()
        max_drawdown = float(returns["drawdown"].min())
        cumulative_turnover = float(returns["turnover"].sum())
        average_turnover = float(returns["turnover"].mean())
        average_gross = float(returns["gross_exposure"].mean())
        maximum_gross = float(returns["gross_exposure"].max())
        average_net = float(returns["net_exposure"].mean())
    else:
        funding_amount = max_drawdown = cumulative_turnover = average_turnover = 0.0
        average_gross = maximum_gross = average_net = 0.0
        start = end = None
    schedule_frame = schedule.frame
    rebalance_count = len(schedule.rebalance_times)
    side_counts = schedule_frame.select(
        (pl.col("target_weight") > 0).sum().alias("long_rows"),
        (pl.col("target_weight") < 0).sum().alias("short_rows"),
        pl.col("symbol").n_unique().alias("symbols"),
    ).row(0, named=True)
    return {
        "report_version": MATRIX_REPORT_VERSION,
        "run_id": result.run_id,
        "start": start,
        "end": end,
        "initial_equity": initial_equity,
        "terminal_equity": result.checkpoint.previous_equity,
        "total_return": total_return,
        "gross_price_return": gross_price_return,
        "max_drawdown": max_drawdown,
        "fee_amount": fee_amount,
        "slippage_amount": slippage_amount,
        "funding_amount": funding_amount,
        "cumulative_turnover": cumulative_turnover,
        "average_turnover": average_turnover,
        "average_gross_exposure": average_gross,
        "maximum_gross_exposure": maximum_gross,
        "average_net_exposure": average_net,
        "rows": returns.height,
        "valuation_rows": returns.height,
        "rebalance_count": rebalance_count,
        "adjustment_rows": rebalances.height,
        "target_rows": schedule_frame.height,
        "symbols": int(side_counts["symbols"]) if schedule_frame.height else 0,
        "average_long_count": int(side_counts["long_rows"]) / rebalance_count if rebalance_count else 0.0,
        "average_short_count": int(side_counts["short_rows"]) / rebalance_count if rebalance_count else 0.0,
    }


def _equity_svg(returns: pl.DataFrame) -> str:
    if returns.height < 2:
        return "<p class='empty'>净值点不足，无法绘图。</p>"
    series = returns.select("timestamp", "equity")
    if series.height > 600:
        step = max(1, series.height // 600)
        series = series.gather_every(step)
    values = [float(value) for value in series["equity"].to_list()]
    low, high = min(values), max(values)
    span = high - low or 1.0
    width, height_px = 1000.0, 260.0
    points = " ".join(
        f"{index * width / (len(values) - 1):.2f},{height_px - (value - low) * height_px / span:.2f}"
        for index, value in enumerate(values)
    )
    return (
        "<div class='chart'><svg viewBox='0 0 1000 260' role='img' "
        "aria-label='Equity curve'><polyline points='"
        + points
        + "'/></svg><div class='axis'><span>"
        + _number(low)
        + "</span><span>"
        + _number(high)
        + "</span></div></div>"
    )


def render_matrix_research_report(
    result: MatrixResult,
    schedule: TargetSchedule,
    *,
    resolved_config: Mapping[str, object],
    market_identity: str,
    research_context: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], str]:
    """Return expanded metrics and a self-contained simplified Event-style report."""
    context = dict(research_context or {})
    metrics = matrix_report_metrics(result, schedule, resolved_config)
    factor_versions = sorted(schedule.frame["factor_version"].unique().to_list())
    universe_versions = sorted(schedule.frame["universe_version"].unique().to_list())
    portfolio_versions = sorted(schedule.frame["portfolio_version"].unique().to_list())

    cards = (
        ("总收益 / Total Return", _percent(metrics["total_return"])),
        ("期末权益 / Terminal Equity", f"{_number(metrics['terminal_equity'])} USDT"),
        ("最大回撤 / Max Drawdown", _percent(metrics["max_drawdown"])),
        ("累计换手 / Turnover", _number(metrics["cumulative_turnover"], 1)),
        ("手续费 / Fee", f"{_number(metrics['fee_amount'])} USDT"),
        ("滑点 / Slippage", f"{_number(metrics['slippage_amount'])} USDT"),
        ("资金费 / Funding", f"{_number(metrics['funding_amount'])} USDT"),
        ("平均总敞口 / Avg Gross", _percent(metrics["average_gross_exposure"])),
    )
    card_html = "".join(
        f"<div class='card'><span>{html.escape(label)}</span><strong>{value}</strong></div>"
        for label, value in cards
    )
    factor_items = (
        ("研究索引键", context.get("index_key", result.run_id)),
        ("研究项目", context.get("study_id", "—")),
        ("候选因子", context.get("factor_code", "—")),
        ("因子名称", context.get("factor_name", "—")),
        ("因子说明", context.get("factor_description", "—")),
        ("因子参数", context.get("factor_parameters", "—")),
        ("方向", context.get("factor_direction", "—")),
        ("因子版本", factor_versions),
    )
    execution_items = (
        ("区间", f"{metrics['start']} → {metrics['end']}"),
        ("因子频率", _nested(resolved_config, "schedule", "factor_interval")),
        ("调仓频率", _nested(resolved_config, "schedule", "rebalance_interval")),
        ("信号延迟", _nested(resolved_config, "schedule", "signal_delay_bars")),
        ("组合", context.get("portfolio", _nested(resolved_config, "portfolio"))),
        ("目标总/净敞口", f"{_nested(resolved_config, 'portfolio', 'sizing', 'target_gross_exposure')} / {_nested(resolved_config, 'portfolio', 'sizing', 'target_net_exposure')}"),
        ("成交模型", _nested(resolved_config, "risk", "fill_model")),
        ("估值价格", _nested(resolved_config, "valuation", "price")),
        ("手续费模型", _nested(resolved_config, "execution", "fee")),
        ("滑点模型", _nested(resolved_config, "execution", "slippage")),
        ("资金费", _nested(resolved_config, "execution", "funding")),
    )
    audit_items = (
        ("Run ID", result.run_id),
        ("Target Schedule", schedule.schedule_id),
        ("父快照 SHA", schedule.parent_manifest_sha256),
        ("市场身份", market_identity),
        ("Universe 版本", universe_versions),
        ("Portfolio 版本", portfolio_versions),
        ("标的数量", metrics["symbols"]),
        ("调仓次数", metrics["rebalance_count"]),
        ("平均多/空数量", f"{metrics['average_long_count']:.1f} / {metrics['average_short_count']:.1f}"),
        ("调仓明细行", metrics["adjustment_rows"]),
        ("估值点", metrics["valuation_rows"]),
    )

    def definition(items: tuple[tuple[str, object], ...]) -> str:
        return "".join(
            f"<div><dt>{html.escape(label)}</dt><dd>{_text(value)}</dd></div>"
            for label, value in items
        )

    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fast Matrix · {_text(context.get('factor_code', result.run_id))}</title>
<style>
:root{{--ink:#10231f;--muted:#60736e;--line:#dbe6e1;--panel:#f5f9f7;--accent:#07836f;--warn:#9a6500}}
*{{box-sizing:border-box}}body{{margin:0;background:#f2f6f4;color:var(--ink);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1320px;margin:24px auto;padding:0 20px}}header,section{{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:14px}}
h1{{margin:4px 0 6px;font-size:25px}}h2{{font-size:17px;margin:0 0 14px}}.eyebrow{{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em}}
.warning{{color:var(--warn);font-weight:700}}.muted,dt{{color:var(--muted)}}.cards{{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:10px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px}}.card span{{display:block;color:var(--muted);font-size:12px}}.card strong{{display:block;font-size:18px;margin-top:4px}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}dl{{margin:0}}dl div{{display:grid;grid-template-columns:150px 1fr;border-bottom:1px solid var(--line);padding:8px 0;gap:12px}}dt,dd{{margin:0}}dd{{overflow-wrap:anywhere}}
.chart{{height:300px;background:linear-gradient(#fff,#f3faf7);border:1px solid var(--line);border-radius:10px;padding:15px}}svg{{width:100%;height:250px}}polyline{{fill:none;stroke:var(--accent);stroke-width:2;vector-effect:non-scaling-stroke}}.axis{{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}}
@media(max-width:800px){{.cards{{grid-template-columns:1fr 1fr}}.two{{grid-template-columns:1fr}}dl div{{grid-template-columns:1fr}}}}
</style></head><body><main>
<header><div class="eyebrow">FAST MATRIX · RESEARCH RUN</div><h1>{_text(context.get('factor_code', '矩阵组合研究'))}</h1>
<p>{_text(context.get('factor_description', '目标权重矩阵组合回测'))}</p><p class="warning">研究结果，未经过 Event 正式确认；也不是 V2 正式 run。</p><p class="muted">报告版本：{MATRIX_REPORT_VERSION} · Run：{_text(result.run_id)}</p></header>
<section><h2>核心结果 / Performance</h2><div class="cards">{card_html}</div></section>
<section><h2>净值路径 / Equity</h2>{_equity_svg(result.returns)}</section>
<div class="two"><section><h2>因子说明 / Factor</h2><dl>{definition(factor_items)}</dl></section>
<section><h2>执行口径 / Execution</h2><dl>{definition(execution_items)}</dl></section></div>
<section><h2>身份与审计 / Identity</h2><dl>{definition(audit_items)}</dl></section>
</main></body></html>\n"""
    return metrics, page
