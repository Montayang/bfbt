"""Independent, searchable reports for staged factor research studies."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import polars as pl

from bfbt.artifacts.matrix import MatrixResearchStore
from bfbt.engine.fast_matrix.result import MatrixCheckpoint, MatrixResult
from bfbt.engine.fast_matrix.target_schedule import TargetSchedule
from bfbt.reports.locales import variant_path, write_html_variants
from bfbt.reports.matrix import render_matrix_research_report


class ResearchStudyReportError(RuntimeError):
    pass


def _pct(value: object) -> str:
    return f"{float(value):.2%}"


def _number(value: object, digits: int = 3) -> str:
    return f"{float(value):,.{digits}f}"


def _page(title: str, eyebrow: str, body: str, *, script: str = "") -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
:root{{--ink:#10231f;--muted:#61736f;--line:#d9e5df;--panel:#f5f9f7;--accent:#07836f}}
*{{box-sizing:border-box}}body{{margin:0;background:#f2f6f4;color:var(--ink);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1500px;margin:22px auto;padding:0 18px}}header,.panel{{background:white;border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}}
h1{{margin:3px 0 6px;font-size:25px}}h2{{font-size:17px}}.eyebrow{{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em}}.muted{{color:var(--muted)}}
.toolbar{{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0}}input,select{{min-height:38px;border:1px solid var(--line);border-radius:8px;background:white;padding:7px 10px;color:var(--ink)}}input{{flex:1;min-width:260px}}
.table-wrap{{overflow:auto;max-height:72vh;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:separate;border-spacing:0;width:100%;white-space:nowrap}}th,td{{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;vertical-align:middle}}th{{position:sticky;top:0;background:#edf5f1;cursor:pointer;z-index:1}}th[data-type='number'],td.num{{text-align:right;font-variant-numeric:tabular-nums}}tbody tr:nth-child(even){{background:#f8fbfa}}tbody tr:hover{{background:#eaf7f2}}td.key{{width:390px;max-width:390px;overflow:hidden;text-overflow:ellipsis}}code{{font-size:12px}}a{{color:var(--accent);font-weight:650;text-decoration:none}}a:hover{{text-decoration:underline}}.legend{{background:var(--panel);border-left:3px solid var(--accent);padding:9px 12px;margin:10px 0;color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.nav-card{{display:block;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;color:var(--ink)}}.nav-card strong{{font-size:17px;display:block}}.nav-card span{{color:var(--muted)}}
@media(max-width:850px){{.cards{{grid-template-columns:1fr}}}}
</style></head><body><main><header><div class="eyebrow">{html.escape(eyebrow)}</div><h1>{html.escape(title)}</h1></header>{body}</main>{script}</body></html>\n"""


def _filter_script(filter_ids: tuple[str, ...]) -> str:
    filters = ",".join(json.dumps(value) for value in filter_ids)
    return f"""<script>
const q=document.getElementById('q'), table=document.getElementById('results');
const filterIds=[{filters}];
function apply(){{const query=q.value.trim().toLowerCase();const selected=Object.fromEntries(filterIds.map(id=>[id,document.getElementById(id).value]));
for(const row of table.tBodies[0].rows){{let visible=!query||row.dataset.search.includes(query);for(const [id,value] of Object.entries(selected)){{if(value&&row.dataset[id]!==value)visible=false}}row.hidden=!visible}}}}
q.addEventListener('input',apply);for(const id of filterIds)document.getElementById(id).addEventListener('change',apply);
for(const th of table.tHead.rows[0].cells)th.addEventListener('click',()=>{{const index=th.cellIndex, numeric=th.dataset.type==='number';const rows=[...table.tBodies[0].rows], ascending=th.dataset.order!=='asc';rows.sort((a,b)=>{{const x=a.cells[index].dataset.value??a.cells[index].textContent;const y=b.cells[index].dataset.value??b.cells[index].textContent;return (numeric?Number(x)-Number(y):x.localeCompare(y))* (ascending?1:-1)}});for(const row of rows)table.tBodies[0].appendChild(row);th.dataset.order=ascending?'asc':'desc'}});
</script>"""


def _select(identifier: str, values: set[str], label: str) -> str:
    options = "".join(
        f"<option value='{html.escape(value)}'>{html.escape(value)}</option>"
        for value in sorted(values)
    )
    return f"<select id='{identifier}'><option value=''>{html.escape(label)}：全部</option>{options}</select>"


def _cell(value: object, *, numeric: bool = False, display: str | None = None) -> str:
    cls = " class='num'" if numeric else ""
    raw = html.escape(str(value))
    shown = html.escape(display if display is not None else str(value))
    return f"<td{cls} data-value='{raw}'>{shown}</td>"


def _key_cell(value: str) -> str:
    escaped = html.escape(value)
    return f"<td class='key' data-value='{escaped}' title='{escaped}'><code>{escaped}</code></td>"


def _load_summary(study_root: Path) -> dict[str, Any]:
    path = study_root / "summary.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchStudyReportError(f"cannot read study summary: {path}") from exc
    if payload.get("status") != "succeeded" or not isinstance(payload.get("months"), dict):
        raise ResearchStudyReportError("study summary is not a succeeded factor study")
    return payload


def _quick_report(summary: dict[str, Any]) -> str:
    study_id = str(summary["study_id"])
    rows: list[str] = []
    periods: set[str] = set()
    factors: set[str] = set()
    horizons: set[str] = set()
    for period, month in sorted(summary["months"].items()):
        periods.add(period)
        for factor, values in sorted(month["factors"].items()):
            factors.add(factor)
            for horizon, metrics in sorted(values["quick_research"].items()):
                horizons.add(horizon)
                key = f"{study_id}|{period}|{factor}|{horizon}"
                search = " ".join((key, study_id, period, factor, horizon)).lower()
                cells = "".join((
                    _key_cell(key), _cell(period), _cell(factor), _cell(horizon),
                    _cell(metrics["mean_rank_ic_direction_adjusted"], numeric=True, display=_number(metrics["mean_rank_ic_direction_adjusted"], 4)),
                    _cell(metrics["expected_direction_quantile_spread"], numeric=True, display=_pct(metrics["expected_direction_quantile_spread"])),
                    _cell(metrics["factor_coverage"], numeric=True, display=_pct(metrics["factor_coverage"])),
                    _cell(metrics["mean_rank_turnover"], numeric=True, display=_pct(metrics["mean_rank_turnover"])),
                    _cell(metrics["rank_ic_expected_sign_fraction"], numeric=True, display=_pct(metrics["rank_ic_expected_sign_fraction"])),
                ))
                rows.append(f"<tr data-search='{html.escape(search)}' data-period='{html.escape(period)}' data-factor='{html.escape(factor)}' data-horizon='{html.escape(horizon)}'>{cells}</tr>")
    toolbar = (
        "<div class='toolbar'><input id='q' placeholder='搜索索引键、月份、因子或周期…'>"
        + _select("period", periods, "月份")
        + _select("factor", factors, "因子")
        + _select("horizon", horizons, "预测周期")
        + "</div>"
    )
    headers = (
        "<th>索引键</th><th>月份</th><th>因子</th><th>周期</th>"
        "<th data-type='number' title='原始 Rank IC 乘以策略方向；正数表示当前多空方向有效'>策略方向 Rank IC</th>"
        "<th data-type='number' title='按策略多空方向计算的首尾分位组合收益差'>策略多空分位差</th>"
        "<th data-type='number'>覆盖率</th><th data-type='number'>Rank 换手</th>"
        "<th data-type='number' title='各时点策略方向 Rank IC 大于零的比例'>策略方向为正比例</th>"
    )
    body = f"""<section class="panel"><p>这里只判断因子排序是否具有预测信息，不模拟账户、不扣交易成本。点击表头排序；索引键可精确定位某个结果。</p>
<div class="legend"><strong>策略方向 Rank IC</strong> = 原始 Rank IC × 策略方向。正数表示按当前规则做多/做空的方向有效；负数表示未来收益排序更支持反向交易。例如 LOWVOL24 做多低波动端，因此会把原始负相关统一转换为策略方向的正值。</div>{toolbar}
<div class="table-wrap"><table id="results"><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"""
    return _page("快速研究层 / Quick Research", "FACTOR SCREENING", body, script=_filter_script(("period", "factor", "horizon")))


def _matrix_report(summary: dict[str, Any]) -> str:
    study_id = str(summary["study_id"])
    rows: list[str] = []
    periods: set[str] = set()
    factors: set[str] = set()
    costs: set[str] = set()
    for period, month in sorted(summary["months"].items()):
        periods.add(period)
        for factor, values in sorted(month["factors"].items()):
            factors.add(factor)
            for cost, metrics in sorted(values["fast_matrix"].items()):
                costs.add(cost)
                run_id = str(metrics["run_id"])
                key = f"{study_id}|{period}|{factor}|{cost}|{run_id}"
                search = " ".join((key, study_id, period, factor, cost, run_id)).lower()
                report = f"fast_matrix_reports/{run_id}.html"
                cells = "".join((
                    _key_cell(key), _cell(period), _cell(factor), _cell(cost), _cell(run_id),
                    _cell(metrics["total_return"], numeric=True, display=_pct(metrics["total_return"])),
                    _cell(metrics["max_drawdown"], numeric=True, display=_pct(metrics["max_drawdown"])),
                    _cell(metrics["cumulative_turnover"], numeric=True, display=_number(metrics["cumulative_turnover"], 1)),
                    _cell(metrics["fee_amount"], numeric=True, display=_number(metrics["fee_amount"], 2)),
                    _cell(metrics["slippage_amount"], numeric=True, display=_number(metrics["slippage_amount"], 2)),
                    _cell(metrics["funding_amount"], numeric=True, display=_number(metrics["funding_amount"], 2)),
                ))
                cells += f"<td><a href='{html.escape(report)}'>查看报告</a></td>"
                rows.append(f"<tr data-search='{html.escape(search)}' data-period='{html.escape(period)}' data-factor='{html.escape(factor)}' data-cost='{html.escape(cost)}'>{cells}</tr>")
    toolbar = (
        "<div class='toolbar'><input id='q' placeholder='搜索索引键、Run ID、月份或因子…'>"
        + _select("period", periods, "月份")
        + _select("factor", factors, "因子")
        + _select("cost", costs, "成本口径")
        + "</div>"
    )
    headers = (
        "<th>索引键</th><th>月份</th><th>因子</th><th>成本</th><th>Run ID</th>"
        "<th data-type='number'>收益</th><th data-type='number'>最大回撤</th>"
        "<th data-type='number'>累计换手</th><th data-type='number'>手续费</th>"
        "<th data-type='number'>滑点</th><th data-type='number'>Funding</th><th>详情</th>"
    )
    policy = html.escape(json.dumps(summary.get("policy", {}), ensure_ascii=False, sort_keys=True))
    body = f"""<section class="panel"><p>这里模拟目标权重账户路径。单 run 详情补充因子、组合、成交、估值、成本、敞口和身份信息；仍属于研究结果，尚未经过 Event 引擎的正式回测确认。</p><p class="muted">统一执行口径：<code>{policy}</code></p>{toolbar}
<div class="table-wrap"><table id="results"><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"""
    return _page("Fast Matrix 组合研究", "PORTFOLIO RESEARCH", body, script=_filter_script(("period", "factor", "cost")))


def _rebuild_run_reports(
    summary: dict[str, Any], study_root: Path, matrix_runs_root: Path
) -> None:
    destination = study_root / "fast_matrix_reports"
    destination.mkdir(parents=True, exist_ok=True)
    store = MatrixResearchStore(matrix_runs_root)
    for period, month in sorted(summary["months"].items()):
        for factor, values in sorted(month["factors"].items()):
            definition = summary["factors"][factor]
            for cost, metrics in sorted(values["fast_matrix"].items()):
                run_id = str(metrics["run_id"])
                manifest = store.load(run_id)
                if manifest is None:
                    raise ResearchStudyReportError(f"missing Fast Matrix run: {run_id}")
                root = store.directory(run_id)
                returns = pl.read_parquet(root / manifest.tables["returns"].path)
                rebalances = pl.read_parquet(root / manifest.tables["rebalance_summary"].path)
                targets = pl.read_parquet(root / manifest.tables["target_schedule"].path)
                config = json.loads((root / manifest.files["resolved_config"].path).read_text())
                times = tuple(sorted(set(targets["fill_time"].to_list())))
                schedule = TargetSchedule(
                    frame=targets,
                    rebalance_times=times,
                    schedule_id=manifest.target_schedule_id,
                    parent_manifest_sha256=manifest.target_parent_sha256,
                )
                terminal = float(returns[-1, "equity"]) if returns.height else float(config["capital"]["initial_equity"])
                checkpoint = MatrixCheckpoint(
                    identity_sha256=manifest.result_hash,
                    symbols=(), quantities=(), average_entry_prices=(), last_close_prices=(),
                    cash=terminal, previous_equity=terminal, peak_equity=terminal,
                    sequence=returns.height, processed_bars=0,
                )
                result = MatrixResult(
                    run_id=run_id, result_hash=manifest.result_hash, returns=returns,
                    rebalance_summary=rebalances, checkpoint=checkpoint, warnings=(),
                    diagnostics={"backend_decision": manifest.backend_decision},
                )
                context = {
                    "index_key": f"{summary['study_id']}|{period}|{factor}|{cost}|{run_id}",
                    "study_id": summary["study_id"], "period": period,
                    "cost_variant": cost, "factor_code": factor,
                    "factor_name": definition["name"],
                    "factor_description": definition["description"],
                    "factor_parameters": definition["parameters"],
                    "factor_direction": definition["direction"],
                    "portfolio": summary.get("policy", {}).get("portfolio", "—"),
                }
                _, page = render_matrix_research_report(
                    result, schedule, resolved_config=config,
                    market_identity=manifest.market_identity, research_context=context,
                )
                write_html_variants(destination / f"{run_id}.html", page)


def render_factor_study_reports(
    study_root: Path, *, matrix_runs_root: Path
) -> dict[str, Path]:
    """Render independent quick/matrix indexes plus a navigation-only landing page."""
    root = study_root.resolve()
    summary = _load_summary(root)
    quick = root / "quick_research.html"
    matrix = root / "fast_matrix.html"
    landing = root / "report.html"
    write_html_variants(quick, _quick_report(summary))
    write_html_variants(matrix, _matrix_report(summary))
    _rebuild_run_reports(summary, root, matrix_runs_root.resolve())
    body = """<section class="panel"><p>快速筛选与组合模拟已经拆分。先在快速研究索引中筛选因子，只把保留候选送入 Fast Matrix；本页不再混放两阶段明细。</p>
<div class="cards"><a class="nav-card" href="quick_research.html"><strong>快速研究层</strong><span>Rank IC、分位差、覆盖率和 Rank 换手</span></a>
<a class="nav-card" href="fast_matrix.html"><strong>Fast Matrix</strong><span>组合净值、回撤、成本、换手与单 run 详情</span></a>
<a class="nav-card" href="summary.json"><strong>机器可读汇总</strong><span>供批处理、二次分析和精确复现使用</span></a></div></section>"""
    write_html_variants(
        landing,
        _page(str(summary["study_id"]), "RESEARCH WORKFLOW", body),
    )
    return {
        "landing": landing,
        "landing_en": variant_path(landing, "en"),
        "landing_zh_cn": variant_path(landing, "zh-CN"),
        "quick_research": quick,
        "quick_research_en": variant_path(quick, "en"),
        "quick_research_zh_cn": variant_path(quick, "zh-CN"),
        "fast_matrix": matrix,
        "fast_matrix_en": variant_path(matrix, "en"),
        "fast_matrix_zh_cn": variant_path(matrix, "zh-CN"),
    }


def render_quick_only_study_report(study_root: Path) -> Path:
    """Render a searchable quick-research report from a flat result catalog."""

    root = study_root.resolve()
    path = root / "summary.json"
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResearchStudyReportError(f"cannot read study summary: {path}") from exc
    if summary.get("status") != "succeeded" or not isinstance(summary.get("results"), list):
        raise ResearchStudyReportError("quick-only summary must be succeeded with results")

    study_id = str(summary["study_id"])
    rows: list[str] = []
    periods: set[str] = set()
    intervals: set[str] = set()
    factors: set[str] = set()
    horizons: set[str] = set()
    for result in summary["results"]:
        period = str(result["period"])
        interval = str(result["bar_interval"])
        factor = str(result["factor_code"])
        horizon = f"{result['horizon_bars']} bars"
        periods.add(period)
        intervals.add(interval)
        factors.add(factor)
        horizons.add(horizon)
        key = f"{study_id}|{period}|{interval}|{factor}|{result['horizon_bars']}"
        search = " ".join((key, period, interval, factor, horizon)).lower()
        cells = "".join((
            _key_cell(key), _cell(period), _cell(str(result["role"])),
            _cell(interval), _cell(factor), _cell(horizon),
            _cell(result["mean_rank_ic"], numeric=True, display=_number(result["mean_rank_ic"], 4)),
            _cell(result["rank_ic_ir"], numeric=True, display=_number(result["rank_ic_ir"], 3)),
            _cell(result["q5_minus_q1"], numeric=True, display=_pct(result["q5_minus_q1"])),
            _cell(result["factor_coverage"], numeric=True, display=_pct(result["factor_coverage"])),
            _cell(result["mean_rank_turnover"], numeric=True, display=_pct(result["mean_rank_turnover"])),
            _cell(result["rank_ic_positive_fraction"], numeric=True, display=_pct(result["rank_ic_positive_fraction"])),
            _cell(result["timestamps"], numeric=True, display=f"{int(result['timestamps']):,}"),
        ))
        rows.append(
            f"<tr data-search='{html.escape(search)}' data-period='{html.escape(period)}' "
            f"data-interval='{html.escape(interval)}' data-factor='{html.escape(factor)}' "
            f"data-horizon='{html.escape(horizon)}'>{cells}</tr>"
        )
    toolbar = (
        "<div class='toolbar'><input id='q' placeholder='搜索索引键、区间、K线、因子或预测根数…'>"
        + _select("period", periods, "区间")
        + _select("interval", intervals, "K线")
        + _select("factor", factors, "因子")
        + _select("horizon", horizons, "预测根数")
        + "</div>"
    )
    headers = (
        "<th>索引键</th><th>区间</th><th>用途</th><th>K线</th><th>因子</th><th>预测</th>"
        "<th data-type='number' title='因子原始排序与未来收益排序的截面 Spearman 相关系数'>Rank IC</th>"
        "<th data-type='number' title='Rank IC 均值除以其时序标准差'>Rank IC IR</th>"
        "<th data-type='number' title='因子最高五分位平均未来收益减最低五分位'>Q5-Q1</th>"
        "<th data-type='number'>覆盖率</th><th data-type='number'>Rank 换手</th>"
        "<th data-type='number'>Rank IC 为正比例</th><th data-type='number'>有效时点</th>"
    )
    contract = html.escape(json.dumps(summary.get("contract", {}), ensure_ascii=False, sort_keys=True))
    body = f"""<section class="panel"><p>本报告只做快速研究：不模拟账户、不扣手续费和滑点。所有窗口与预测长度均按当前 K 线的根数解释。</p>
<div class="legend"><strong>未见区间隔离：</strong>7 月标记为未见集；公式、参数、K 线级别和预测根数在读取 7 月结果前冻结。快速研究层只使用一套连续因子历史口径，不重复计算分段冷启动版本。Alpha40 默认使用 quote_volume，另列 base_volume 对照。</div>
<p class="muted">冻结合同：<code>{contract}</code></p>{toolbar}
<div class="table-wrap"><table id="results"><thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"""
    report = root / "quick_research.html"
    write_html_variants(
        report,
        _page(
            str(summary.get("title", study_id)),
            "GTJA191 QUICK RESEARCH",
            body,
            script=_filter_script(("period", "interval", "factor", "horizon")),
        ),
    )
    return report
