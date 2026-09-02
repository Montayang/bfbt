"""Self-contained bilingual HTML renderer for verified showcase evidence."""

from __future__ import annotations

import html
import re
from calendar import month_name
from collections import Counter
from typing import Any


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _percent(value: object, digits: int = 2) -> str:
    return f"{float(value) * 100:.{digits}f}%"


def _number(value: object, digits: int = 2) -> str:
    return f"{float(value):,.{digits}f}"


def _short(value: object, length: int = 12) -> str:
    text = str(value)
    return text if len(text) <= length else text[:length] + "…"


def _warning_summary(warnings: list[str]) -> str:
    """Summarize warning codes without republishing untrusted source fields."""

    counts = Counter(item.partition(":")[0] or "unknown" for item in warnings)
    return "".join(
        f"<li><code>{_escape(code)}</code> × {count}</li>"
        for code, count in sorted(counts.items())
    )


def _period_labels(run: dict[str, Any]) -> tuple[str, str]:
    period = str(run.get("period_label", ""))
    match = re.fullmatch(r"(\d{4})-(\d{2})", period)
    if match is None:
        label = str(run["label"])
        return label, label
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        label = str(run["label"])
        return label, label
    return f"{year} 年 {month} 月", f"{month_name[month]} {year}"


def _period_display(run: dict[str, Any]) -> str:
    chinese, english = _period_labels(run)
    return chinese if chinese == english else f"{chinese} / {english}"


def _warning_heading(run: dict[str, Any], count: int) -> str:
    chinese, english = _period_labels(run)
    if chinese == english:
        return f"{chinese} · {count} warnings"
    return f"{chinese} · {count} 条 / {english} · {count} warnings"


def _margin_polyline(values: list[dict[str, Any]]) -> str:
    if not values:
        return ""
    width, height, padding = 820.0, 250.0, 28.0
    margins = [float(item["margin"]) for item in values]
    low = min(80.0, min(margins))
    high = max(1_000.0, max(margins))
    span = max(high - low, 1.0)
    points = []
    for index, value in enumerate(margins):
        x = padding + (width - 2 * padding) * index / max(len(margins) - 1, 1)
        y = height - padding - (height - 2 * padding) * (value - low) / span
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def render_showcase(evidence: dict[str, Any]) -> str:
    """Render stable HTML without external assets or machine-specific paths."""

    presentation = evidence["presentation"]
    intent = evidence["intent"]
    semantics = intent["semantics"]
    runs = evidence["runs"]
    colors = ("#55d6be", "#f5bd55", "#f06b7e", "#7ea8ff")
    result_cards = []
    comparison_rows = []
    provenance_rows = []
    warning_blocks = []
    paths = []
    for index, run in enumerate(runs):
        performance = run["performance"]
        provenance = run["provenance"]
        tone = "positive" if float(performance["total_return"]) >= 0 else "negative"
        qualified = bool(provenance["git_dirty"])
        badge = "限定来源 / QUALIFIED" if qualified else "干净来源 / CLEAN"
        result_cards.append(
            f"""<article class="result-card {tone}">
<div class="card-top"><span>{_escape(_period_display(run))}</span><span class="badge {'warn' if qualified else 'ok'}">{badge}</span></div>
<strong class="return">{_percent(performance['total_return'])}</strong>
<span class="return-label">总收益 / Total return</span>
<dl><div><dt>最大回撤</dt><dd>{_percent(performance['max_drawdown'])}</dd></div>
<div><dt>期末权益</dt><dd>{_number(performance['ending_equity'])}</dd></div>
<div><dt>累计换手</dt><dd>{_number(performance['total_turnover'])}</dd></div>
<div><dt>成交</dt><dd>{run['trade_count']}</dd></div></dl>
</article>"""
        )
        cost_drag = float(performance["fee_contribution"]) + float(
            performance["slippage_contribution"]
        )
        comparison_rows.append(
            "<tr>"
            f"<th>{_escape(_period_display(run))}</th>"
            f"<td class='{tone}'>{_percent(performance['total_return'])}</td>"
            f"<td>{_percent(performance['max_drawdown'])}</td>"
            f"<td>{_percent(cost_drag)}</td>"
            f"<td>{_number(performance['total_turnover'])}</td>"
            f"<td>{run['opening_count']}</td>"
            f"<td>{len(run['warnings'])}</td>"
            "</tr>"
        )
        links = run["evidence_links"]
        provenance_rows.append(
            f"""<article class="evidence-card"><div><span class="eyebrow">{_escape(_period_display(run))}</span>
<h3>{_escape(run['run_name'])}</h3></div>
<p>run <code>{_escape(run['run_id'])}</code> · manifest <code>{_escape(_short(run['manifest_sha256']))}</code></p>
<p>commit <code>{_escape(_short(provenance['git_commit']))}</code> · source <code>{_escape(_short(provenance['source_fingerprint']))}</code></p>
<nav aria-label="Evidence links"><a href="{_escape(links['report'])}">深度报告</a><a href="{_escape(links['config'])}">配置</a><a href="{_escape(links['metrics'])}">指标</a><a href="{_escape(links['manifest'])}">Manifest</a><a href="{_escape(links['warnings'])}">警告</a></nav></article>"""
        )
        warnings = run["warnings"]
        warning_blocks.append(
            f"<details><summary>{_escape(_warning_heading(run, len(warnings)))}</summary>"
            + (
                "<ul>" + _warning_summary(warnings) + "</ul>"
                if warnings
                else "<p>无运行警告 / No run warnings.</p>"
            )
            + "</details>"
        )
        color = colors[index % len(colors)]
        paths.append(
            f"<polyline points=\"{_margin_polyline(run['opening_margin_trajectory'])}\" fill=\"none\" stroke=\"{color}\" stroke-width=\"3\" vector-effect=\"non-scaling-stroke\"><title>{_escape(_period_display(run))}</title></polyline>"
        )

    decisions = "".join(f"<li>{_escape(item)}</li>" for item in intent["user_decisions"])
    narrative = "".join(f"<li>{_escape(item)}</li>" for item in presentation["narrative"])
    disclosures = "".join(f"<li>{_escape(item)}</li>" for item in presentation["disclosures"])
    action_rows = "".join(
        f"<span class='action'>{_escape(item)}</span>" for item in intent["required_actions"]
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_escape(presentation['title'])} · bfbt Showcase</title>
<style>
:root{{--ink:#eaf2ff;--muted:#9eabc0;--bg:#081018;--panel:#101b27;--panel2:#152333;--line:#26394d;--green:#55d6be;--amber:#f5bd55;--red:#f06b7e;--blue:#7ea8ff}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:radial-gradient(circle at 85% 2%,#18344a 0,transparent 30%),var(--bg);color:var(--ink);font:15px/1.65 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}a{{color:#9fc0ff;text-decoration:none}}a:hover{{text-decoration:underline}}code{{font:12px ui-monospace,SFMono-Regular,monospace;color:#c7d9f7;word-break:break-all}}.shell{{max-width:1180px;margin:auto;padding:0 24px 72px}}.hero{{min-height:74vh;display:flex;flex-direction:column;justify-content:center;padding:70px 0 48px}}.eyebrow{{color:var(--green);font-size:12px;font-weight:800;letter-spacing:.14em;text-transform:uppercase}}h1{{font-size:clamp(42px,7vw,82px);line-height:1.02;letter-spacing:-.045em;margin:16px 0;max-width:980px}}h2{{font-size:clamp(27px,4vw,42px);letter-spacing:-.025em;margin:0 0 12px}}h3{{margin:4px 0 8px}}.subtitle{{font-size:clamp(18px,2.2vw,25px);color:#c2cede;max-width:860px}}.boundary{{display:inline-flex;width:max-content;margin-top:24px;padding:9px 13px;border:1px solid #31506a;border-radius:999px;color:#b8cbe0;background:#0e1e2d}}.quick-nav{{display:flex;gap:10px;flex-wrap:wrap;margin-top:32px}}.quick-nav a,.evidence-card nav a{{padding:8px 12px;border:1px solid var(--line);border-radius:9px;background:var(--panel)}}section{{margin:56px 0}}.section-head{{margin-bottom:22px;max-width:820px}}.section-head p{{color:var(--muted);font-size:17px}}.workflow{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.step,.panel,.result-card,.evidence-card{{background:linear-gradient(150deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:16px;padding:20px}}.step b{{display:block;font-size:18px;margin:7px 0}}.step span{{color:var(--muted)}}.arrow{{color:var(--green);font-size:23px}}.intent-quote{{font-size:22px;border-left:3px solid var(--green);padding:14px 20px;background:#0d1823;border-radius:0 12px 12px 0}}.freeze-grid{{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:14px}}.freeze-grid div{{padding:13px;background:#0d1823;border:1px solid var(--line);border-radius:10px}}.freeze-grid span{{display:block;color:var(--muted);font-size:12px}}.action{{display:inline-block;padding:6px 9px;margin:4px;border-radius:8px;background:#14293a;color:#b9d2ed;font:12px ui-monospace,monospace}}.results{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.card-top{{display:flex;justify-content:space-between;gap:10px;align-items:start}}.badge{{font-size:10px;padding:4px 7px;border-radius:999px;font-weight:800}}.badge.warn{{color:#291b00;background:var(--amber)}}.badge.ok{{color:#05241d;background:var(--green)}}.return{{display:block;font-size:42px;margin-top:22px}}.positive .return,.positive td{{color:var(--green)}}.negative .return,.negative td{{color:var(--red)}}.return-label{{color:var(--muted)}}dl{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:20px 0 0}}dl div{{border-top:1px solid var(--line);padding-top:8px}}dt{{color:var(--muted);font-size:12px}}dd{{margin:2px 0;font-weight:700}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:14px}}table{{border-collapse:collapse;width:100%;background:var(--panel)}}th,td{{padding:12px 14px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}th:first-child{{text-align:left}}thead th{{color:var(--muted);font-size:12px}}.chart{{background:#0d1823;border:1px solid var(--line);border-radius:14px;padding:18px}}svg{{width:100%;height:auto}}.legend{{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted)}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}}.evidence-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}}.evidence-card nav{{display:flex;gap:6px;flex-wrap:wrap;margin-top:13px}}.evidence-card nav a{{font-size:12px;padding:6px 8px}}details{{border:1px solid var(--line);background:var(--panel);border-radius:10px;margin:8px 0;padding:10px 14px}}summary{{cursor:pointer;font-weight:700}}.disclosure{{border:1px solid #5d4821;background:#211b10;border-radius:14px;padding:18px}}footer{{color:var(--muted);border-top:1px solid var(--line);padding-top:24px;margin-top:60px}}@media(max-width:820px){{.workflow,.results,.evidence-grid,.freeze-grid{{grid-template-columns:1fr}}.hero{{min-height:auto}}h1{{font-size:44px}}}}@media print{{:root{{--ink:#111;--muted:#444;--bg:#fff;--panel:#fff;--panel2:#f7f7f7;--line:#ccc}}body{{background:#fff}}.hero{{min-height:auto}}a{{color:#111}}}}
</style></head><body><main class="shell">
<header class="hero"><span class="eyebrow">BFBT · VERIFIED RESEARCH SHOWCASE</span><h1>{_escape(presentation['title'])}</h1><p class="subtitle">{_escape(presentation['subtitle'])}</p><span class="boundary">离线研究系统 · 无账户连接 · 不执行真实订单</span><nav class="quick-nav"><a href="#workflow">工作流</a><a href="#intent">研究冻结</a><a href="#results">多期结果</a><a href="#audit">审计证据</a></nav></header>
<section id="workflow"><div class="section-head"><span class="eyebrow">01 · SYSTEM</span><h2>Agent 控制面，确定性计算内核</h2><p>自然语言负责表达意图；版本化合同和既有引擎负责检查、计算与发布。复杂的路径依赖风险只进入 Event/V2。</p></div><div class="workflow"><div class="step"><span>快速诊断</span><b>Quick Research</b><span>IC、分层、覆盖率与换手，不模拟账户。</span></div><div class="step"><span>组合研究</span><b>Fast Matrix</b><span>常规截面目标路径的列式经济研究，由用户人工选择。</span></div><div class="step"><span>正式确认</span><b>Event / V2</b><span>时序账户、风险仲裁、滚仓状态、checkpoint 与不可变审计。</span></div></div></section>
<section id="intent"><div class="section-head"><span class="eyebrow">02 · RESEARCH INTENT</span><h2>先冻结语义，再看结果</h2><p>原始研究请求和用户决定按原语言保留，以便审计。</p></div><blockquote class="intent-quote">{_escape(intent['user_text'])}</blockquote><div class="panel"><div class="freeze-grid"><div><span>因子 / Factor</span>{_escape(intent['factor']['name'])} · {_escape(intent['factor']['parameters'])}</div><div><span>市场 / Market</span>Binance USD-M perpetual · USDT</div><div><span>Rank 规则 / Rank</span>{_escape(semantics['rank_rule'])}</div><div><span>时钟与成交 / Clock & fill</span>{_escape(semantics['decision_clock'])} · {_escape(semantics['fill_timing'])}</div><div><span>仓位 / Sizing</span>{_escape(semantics['sizing'])}</div><div><span>成本与风险 / Cost & risk</span>{_escape(semantics['costs'])} · {_escape(semantics['risk_exits'])}</div></div><h3>用户决定 / Human decisions</h3><ul>{decisions}</ul><div>{action_rows}</div></div></section>
<section id="results"><div class="section-head"><span class="eyebrow">03 · MULTI-PERIOD EVIDENCE</span><h2>同一身份，三个独立月份</h2><p>不以单月年化收益作为标题。收益、回撤、成本、换手和来源限定同时展示。</p></div><div class="results">{''.join(result_cards)}</div><div class="table-wrap" style="margin-top:14px"><table><thead><tr><th>月份</th><th>总收益</th><th>最大回撤</th><th>手续费+滑点拖累</th><th>累计换手</th><th>开仓轮次</th><th>警告</th></tr></thead><tbody>{''.join(comparison_rows)}</tbody></table></div></section>
<section><div class="section-head"><span class="eyebrow">04 · ROLLING MARGIN</span><h2>每次开仓的保证金轨迹</h2><p>由已验证 trades 中 BUY 名义价值 ÷ 冻结杠杆派生；这是展示视图，不改写正式 run。</p></div><div class="chart"><svg viewBox="0 0 820 250" role="img" aria-label="三个月开仓保证金轨迹"><line x1="28" y1="28" x2="28" y2="222" stroke="#40546a"/><line x1="28" y1="222" x2="792" y2="222" stroke="#40546a"/>{''.join(paths)}</svg><div class="legend">{''.join(f'<span><i style="background:{colors[i % len(colors)]}"></i>{_escape(_period_display(run))}</span>' for i, run in enumerate(runs))}</div></div></section>
<section id="audit"><div class="section-head"><span class="eyebrow">05 · AUDIT</span><h2>每个结论都能回到证据</h2><p>所有 headline 数字来自逐文件哈希验证后的 artifact。黄色来源徽标表示 run 记录了 dirty source，不能被解释为干净工作树运行。</p></div><div class="evidence-grid">{''.join(provenance_rows)}</div><h3>运行警告 / Run warnings</h3>{''.join(warning_blocks)}<p><a href="evidence.json">机器可读展示证据 / Machine-readable evidence</a> · SHA-256 <code>{_escape(evidence['evidence_sha256'])}</code></p></section>
<section class="disclosure"><span class="eyebrow">DISCLOSURE</span><ul>{disclosures}</ul></section>
<section><div class="section-head"><span class="eyebrow">WHY THIS MATTERS</span><h2>展示的是研究可信度，不是收益承诺</h2></div><ul>{narrative}</ul></section>
<footer>由 bfbt 从不可变回测产物确定性生成。This is historical simulation evidence, not investment advice.</footer>
</main></body></html>"""
