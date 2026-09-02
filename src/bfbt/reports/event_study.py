"""Offline parent reports for one Event strategy and many risk profiles."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Mapping

from bfbt.reports.locales import write_html_variants


class EventStudyReportError(ValueError):
    """An Event parameter study cannot be rendered safely."""


def _pct(value: object) -> str:
    return f"{float(value):.2%}"


def _num(value: object, digits: int = 2) -> str:
    return f"{float(value):,.{digits}f}"


def _metric_pct(value: object) -> str:
    return "—" if value is None else _pct(value)


def _metric_num(value: object, digits: int = 2) -> str:
    return "—" if value is None else _num(value, digits)


def _validated(summary: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if summary.get("study_version") != "event-parameter-study/v1":
        raise EventStudyReportError("unsupported Event study version")
    candidates = summary.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise EventStudyReportError("Event study requires candidates")
    profile_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise EventStudyReportError("candidate must be an object")
        profile = candidate.get("profile_id")
        report = candidate.get("report_href")
        if not isinstance(profile, str) or not profile:
            raise EventStudyReportError("candidate profile_id is required")
        if not isinstance(report, str) or not report.endswith(".html"):
            raise EventStudyReportError("candidate report_href must be an HTML path")
        target = Path(report)
        if target.is_absolute() or ".." in target.parts:
            raise EventStudyReportError("candidate report_href must stay below the study")
        profile_ids.append(profile)
    if len(profile_ids) != len(set(profile_ids)):
        raise EventStudyReportError("candidate profile_id values must be unique")
    return candidates


def render_event_parameter_study(
    summary: Mapping[str, Any], *, output_path: Path
) -> Path:
    """Write one sheets-style index over independently auditable child reports."""

    candidates = _validated(summary)
    title = html.escape(str(summary.get("title", summary.get("study_id", "Event study"))))
    direction = html.escape(str(summary.get("direction", "")))
    factor = html.escape(str(summary.get("factor_name", "")))
    contract = html.escape(
        json.dumps(summary.get("contract", {}), ensure_ascii=False, sort_keys=True)
    )
    tabs: list[str] = []
    panels: list[str] = []
    rows: list[str] = []
    for index, candidate in enumerate(candidates):
        profile = str(candidate["profile_id"])
        escaped_profile = html.escape(profile)
        active = " active" if index == 0 else ""
        selected = "true" if index == 0 else "false"
        tabs.append(
            f"<button class='sheet-tab{active}' type='button' data-sheet='{escaped_profile}' "
            f"aria-selected='{selected}'>{escaped_profile}</button>"
        )
        report = html.escape(str(candidate["report_href"]), quote=True)
        run_id = html.escape(str(candidate.get("run_id", "")))
        source = f" src='{report}'" if index == 0 else ""
        panels.append(
            f"<section class='sheet-panel{active}' data-panel='{escaped_profile}'>"
            f"<div class='sheet-meta'><strong>{escaped_profile}</strong>"
            f"<span>Run <code>{run_id}</code></span>"
            f"<a href='{report}' target='_blank' rel='noopener'>单独打开子报告 ↗</a></div>"
            f"<iframe title='{escaped_profile} Event report' data-src='{report}'"
            f"{source}></iframe></section>"
        )
        metrics = candidate.get("metrics", {})
        performance = metrics.get("performance", {}) if isinstance(metrics, Mapping) else {}
        risk = metrics.get("risk", {}) if isinstance(metrics, Mapping) else {}
        attribution = metrics.get("attribution", {}) if isinstance(metrics, Mapping) else {}
        status = str(candidate.get("status", "unknown"))
        stop = candidate.get("stop_loss")
        take = candidate.get("take_profit")
        trade_count = candidate.get("trade_count")
        stop_count = candidate.get("stop_loss_count")
        take_count = candidate.get("take_profit_count")
        rows.append(
            "<tr>"
            f"<td><button class='row-link' data-open-sheet='{escaped_profile}'>{escaped_profile}</button></td>"
            f"<td>{html.escape(status)}</td>"
            f"<td>{'—' if take is None else _pct(take)}</td>"
            f"<td>{'—' if stop is None else _pct(stop)}</td>"
            f"<td>{_metric_pct(performance.get('total_return'))}</td>"
            f"<td>{_metric_pct(performance.get('max_drawdown'))}</td>"
            f"<td>{_metric_num(risk.get('total_turnover'), 1)}</td>"
            f"<td>{'—' if trade_count is None else f'{int(trade_count):,}'}</td>"
            f"<td>{'—' if stop_count is None else f'{int(stop_count):,}'}</td>"
            f"<td>{'—' if take_count is None else f'{int(take_count):,}'}</td>"
            f"<td>{_metric_pct(attribution.get('fee_contribution'))}</td>"
            f"<td>{_metric_pct(attribution.get('slippage_contribution'))}</td>"
            "</tr>"
        )
    page = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title>
<style>
:root{{--ink:#10231f;--muted:#62736f;--line:#d7e3de;--accent:#07836f;--paper:#fff;--bg:#f1f6f3}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,sans-serif}}
main{{max-width:1680px;margin:18px auto;padding:0 18px}}header,.panel{{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:14px}}
.eyebrow{{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em}}h1{{margin:3px 0 7px;font-size:25px}}p{{margin:5px 0}}.muted{{color:var(--muted)}}code{{font-size:12px}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}th,td{{padding:9px 10px;border-bottom:1px solid var(--line);text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#edf5f1;position:sticky;top:0}}.row-link{{border:0;background:none;color:var(--accent);font-weight:750;cursor:pointer}}
.sheet-tabs{{display:flex;gap:5px;overflow:auto;border-bottom:1px solid var(--line);padding:0 3px}}.sheet-tab{{border:1px solid var(--line);border-bottom:0;border-radius:9px 9px 0 0;background:#eef4f1;padding:9px 17px;cursor:pointer;color:var(--muted)}}.sheet-tab.active{{background:white;color:var(--ink);font-weight:800;transform:translateY(1px)}}
.sheet-panel{{display:none}}.sheet-panel.active{{display:block}}.sheet-meta{{display:flex;gap:16px;align-items:center;flex-wrap:wrap;padding:11px 4px}}.sheet-meta a{{margin-left:auto;color:var(--accent);font-weight:700;text-decoration:none}}iframe{{width:100%;height:82vh;min-height:760px;border:1px solid var(--line);border-radius:12px;background:white}}
@media(max-width:800px){{iframe{{min-height:620px}}.sheet-meta a{{margin-left:0}}}}
</style></head><body><main><header><div class='eyebrow'>EVENT PARAMETER STUDY</div><h1>{title}</h1>
<p>因子：<strong>{factor}</strong> · 方向：<strong>{direction}</strong></p><p class='muted'>一个父研究身份索引多个独立经济配置；每个子报告仍由不可变 Event run 确定性生成。</p></header>
<section class='panel'><h2>参数总览</h2><div class='table-wrap'><table><thead><tr><th>Sheet</th><th>状态</th><th>止盈</th><th>止损</th><th>收益</th><th>最大回撤</th><th>累计换手</th><th>成交</th><th>止损</th><th>止盈</th><th>手续费拖累</th><th>滑点拖累</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><details><summary>冻结合同</summary><code>{contract}</code></details></section>
<section class='panel'><div class='sheet-tabs' role='tablist'>{''.join(tabs)}</div>{''.join(panels)}</section>
</main><script>
function openSheet(id){{document.querySelectorAll('.sheet-tab').forEach(x=>{{const on=x.dataset.sheet===id;x.classList.toggle('active',on);x.setAttribute('aria-selected',String(on))}});document.querySelectorAll('.sheet-panel').forEach(x=>{{const on=x.dataset.panel===id;x.classList.toggle('active',on);if(on){{const f=x.querySelector('iframe');if(!f.getAttribute('src'))f.setAttribute('src',f.dataset.src)}}}})}}
document.querySelectorAll('.sheet-tab').forEach(x=>x.addEventListener('click',()=>openSheet(x.dataset.sheet)));document.querySelectorAll('.row-link').forEach(x=>x.addEventListener('click',()=>{{openSheet(x.dataset.openSheet);document.querySelector('.sheet-tabs').scrollIntoView({{behavior:'smooth'}})}}));
</script></body></html>"""
    destination = output_path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_html_variants(destination, page)
    return destination
