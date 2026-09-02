"""Deterministic language variants for human-facing HTML artifacts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Mapping


ReportLocale = Literal["en", "zh-CN"]
REPORT_LOCALES: tuple[ReportLocale, ...] = ("en", "zh-CN")


# These are presentation strings only. Economic values, identifiers, JSON keys, and source
# evidence are deliberately left untouched.
_ENGLISH: Mapping[str, str] = {
    "由不可变运行产物确定性生成。收益不构成投资建议。": (
        "Generated deterministically from immutable run artifacts. "
        "Historical results are not investment advice."
    ),
    "最后估值时刻仍在账面的持仓；没有额外执行强制平仓。": (
        "Positions remaining on the ledger at the final valuation timestamp; "
        "no synthetic forced close was executed."
    ),
    "每笔成交均保留；成交点显示完整账本中真实相邻时点的受影响持仓状态。": (
        "Every fill is retained; trade points show affected positions at the true adjacent "
        "ledger timestamps."
    ),
    "这是研究结果，尚未经过 Event 引擎的正式回测确认。": (
        "This is a research result and has not yet been confirmed by a formal Event Engine backtest."
    ),
    "这里只判断因子排序是否具有预测信息，不模拟账户、不扣交易成本。点击表头排序；索引键可精确定位某个结果。": (
        "This layer evaluates whether factor ranks contain predictive information. It does "
        "not simulate an account or charge trading costs. Sort by clicking a column header; "
        "the index key identifies an exact result."
    ),
    "策略方向 Rank IC": "Strategy-direction Rank IC",
    "原始 Rank IC × 策略方向。正数表示按当前规则做多/做空的方向有效；负数表示未来收益排序更支持反向交易。例如 LOWVOL24 做多低波动端，因此会把原始负相关统一转换为策略方向的正值。": (
        "Raw Rank IC × strategy direction. A positive value supports the configured long/short "
        "direction; a negative value supports the opposite direction. LOWVOL24, for example, "
        "is long the low-volatility tail, so its raw negative relation is normalized to a "
        "positive strategy-direction value."
    ),
    "这里模拟目标权重账户路径。单 run 详情补充因子、组合、成交、估值、成本、敞口和身份信息；仍属于研究结果，尚未经过 Event 引擎的正式回测确认。": (
        "This layer simulates a target-weight account path. Each run adds factor, portfolio, "
        "fill, valuation, cost, exposure, and identity details. It remains a research result, "
        "not a completed Event Engine backtest."
    ),
    "快速筛选与组合模拟已经拆分。先在快速研究索引中筛选因子，只把保留候选送入 Fast Matrix；本页不再混放两阶段明细。": (
        "Factor screening and portfolio simulation are separated. Screen factors in Quick "
        "Research, then send only retained candidates to Fast Matrix."
    ),
    "本报告只做快速研究：不模拟账户、不扣手续费和滑点。所有窗口与预测长度均按当前 K 线的根数解释。": (
        "This report is Quick Research only: it does not simulate an account or charge fees "
        "and slippage. Windows and forecast horizons are measured in bars at the stated interval."
    ),
    "未见区间隔离：": "Holdout isolation:",
    "7 月标记为未见集；公式、参数、K 线级别和预测根数在读取 7 月结果前冻结。快速研究层只使用一套连续因子历史口径，不重复计算分段冷启动版本。Alpha40 默认使用 quote_volume，另列 base_volume 对照。": (
        "July is the holdout. Formula, parameters, bar interval, and horizon were frozen before "
        "reading July results. Quick Research uses one continuous factor-history convention "
        "rather than repeated cold starts. Alpha40 defaults to quote_volume with a separate "
        "base_volume comparison."
    ),
    "一个父研究身份索引多个独立经济配置；每个子报告仍由不可变 Event run 确定性生成。": (
        "One parent study identity indexes multiple independent economic configurations; every "
        "child report is still generated deterministically from an immutable Event run."
    ),
    "自然语言负责表达意图；版本化合同和既有引擎负责检查、计算与发布。复杂的路径依赖风险只进入 Event 引擎。": (
        "Natural language expresses intent; versioned contracts and deterministic engines "
        "validate, compute, and publish. Complex path-dependent risk is handled only by the Event Engine."
    ),
    "IC、分层、覆盖率与换手，不模拟账户。": (
        "IC, quantiles, coverage, and turnover without account simulation."
    ),
    "常规截面目标路径的列式经济研究，由用户人工选择。": (
        "Columnar economics for conventional cross-sectional target paths, followed by human selection."
    ),
    "时序账户、风险仲裁、滚仓状态、checkpoint 与不可变审计。": (
        "Chronological account state, risk arbitration, rolling state, checkpoints, and immutable audit."
    ),
    "原始研究请求和用户决定按原语言保留，以便审计。": (
        "The original research request and human decisions are retained in their source language for auditability."
    ),
    "不以单月年化收益作为标题。收益、回撤、成本、换手和来源限定同时展示。": (
        "No single-month annualized return is used as a headline. Return, drawdown, costs, "
        "turnover, and provenance qualifications are shown together."
    ),
    "由已验证 trades 中 BUY 名义价值 ÷ 冻结杠杆派生；这是展示视图，不改写正式 run。": (
        "Derived from BUY notional divided by frozen leverage in verified trades. This is a "
        "presentation view and does not rewrite a formal run."
    ),
    "所有 headline 数字来自逐文件哈希验证后的 artifact。黄色来源徽标表示 run 记录了 dirty source，不能被解释为干净工作树运行。": (
        "Every headline number comes from artifacts verified file by file. An amber provenance "
        "badge means the run recorded a dirty source tree and must not be presented as clean."
    ),
    "这些结果是历史模拟，不构成投资建议，也不代表未来收益。": (
        "These are historical simulations, not investment advice or a promise of future returns."
    ),
    "当前 r01 产物的环境审计记录为 git_dirty=true；页面必须显示该限定，不能称为干净源码运行。": (
        "The r01 environment records git_dirty=true. This qualification must remain visible; "
        "the runs cannot be described as clean-source executions."
    ),
    "资金费率缺失按冻结配置 assume_zero 处理，具体事件保留在每个 run 的 warnings.json。": (
        "Missing funding is handled by the frozen assume_zero policy; exact events remain in "
        "each run's warnings.json."
    ),
    "滚仓保证金轨迹由 BUY 成交名义价值除以冻结的 5 倍杠杆派生，不改写正式产物。": (
        "The rolling-margin path is derived from BUY notional divided by frozen 5x leverage and "
        "does not modify formal artifacts."
    ),
    "同一个策略身份在三个独立月份呈现不同结果，展示多期验证而不是挑选单一盈利曲线。": (
        "The same strategy identity produces different results in three independent months, "
        "showing multi-period evidence rather than one selected winning curve."
    ),
    "Event 引擎负责移动止盈、滚仓状态和逐分钟风险仲裁；Fast Matrix 不近似这些路径依赖语义。": (
        "The Event Engine handles trailing exits, rolling state, and minute-level risk arbitration; "
        "Fast Matrix does not approximate these path-dependent semantics."
    ),
    "换手、手续费、滑点、资金费率和警告与收益同时出现，避免只展示毛收益。": (
        "Turnover, fees, slippage, funding, and warnings are shown beside returns instead of "
        "presenting gross performance alone."
    ),
    "每个摘要都能回到 run ID、配置哈希、数据版本、源码指纹和逐笔成交。": (
        "Every summary links back to a run ID, configuration hash, data version, source "
        "fingerprint, and individual fills."
    ),
    "全市场截面因子研究，如何变成可追溯的正式回测": (
        "From full-market factor research to an auditable formal backtest"
    ),
    "从自然语言研究请求，到冻结语义、三个月正式回测结果与逐笔审计证据": (
        "From a natural-language request to frozen semantics, three formal backtest months, and "
        "trade-level evidence"
    ),
    "Agent 控制面，确定性计算内核": "Agent control plane, deterministic computation",
    "先冻结语义，再看结果": "Freeze semantics before viewing results",
    "同一身份，三个独立月份": "One identity, three independent months",
    "每次开仓的保证金轨迹": "Opening-margin path for every entry",
    "每个结论都能回到证据": "Every conclusion links back to evidence",
    "展示的是研究可信度，不是收益承诺": "Research credibility, not a return promise",
    "离线研究系统 · 无账户连接 · 不执行真实订单": (
        "Offline research · no account connection · no live orders"
    ),
    "由 bfbt 从不可变回测产物确定性生成。": (
        "Generated deterministically by BFBT from immutable backtest artifacts."
    ),
    "快速诊断": "Fast diagnostics",
    "组合研究": "Portfolio research",
    "正式回测": "Formal backtest",
    "工作流": "Workflow",
    "研究冻结": "Research freeze",
    "多期结果": "Multi-period results",
    "审计证据": "Audit evidence",
    "三个月开仓保证金轨迹": "Three-month opening-margin paths",
    "用户决定": "Human decisions",
    "最大回撤": "Maximum drawdown",
    "期末权益": "Ending equity",
    "累计换手": "Total turnover",
    "成交": "Trades",
    "月份": "Month",
    "总收益": "Total return",
    "手续费+滑点拖累": "Fee + slippage drag",
    "开仓轮次": "Entry count",
    "警告": "Warnings",
    "深度报告": "Detailed report",
    "配置": "Configuration",
    "指标": "Metrics",
    "限定来源": "QUALIFIED",
    "干净来源": "CLEAN",
    "运行警告": "Run warnings",
    "无运行警告": "No run warnings.",
    "机器可读展示证据": "Machine-readable showcase evidence",
    "报告版本：": "Report version: ",
    "研究索引键": "Research index key",
    "研究项目": "Research study",
    "候选因子": "Candidate factor",
    "因子名称": "Factor name",
    "因子说明": "Factor description",
    "因子参数": "Factor parameters",
    "方向": "Direction",
    "因子版本": "Factor version",
    "区间": "Period",
    "因子频率": "Factor interval",
    "调仓频率": "Rebalance interval",
    "信号延迟": "Signal delay",
    "组合": "Portfolio",
    "目标总/净敞口": "Target gross/net exposure",
    "成交模型": "Fill model",
    "估值价格": "Valuation price",
    "手续费模型": "Fee model",
    "滑点模型": "Slippage model",
    "资金费": "Funding",
    "父快照 SHA": "Parent snapshot SHA",
    "市场身份": "Market identity",
    "标的数量": "Symbols",
    "调仓次数": "Rebalances",
    "平均多/空数量": "Average long/short count",
    "调仓明细行": "Adjustment rows",
    "估值点": "Valuation points",
    "核心结果": "Performance",
    "净值路径": "Equity path",
    "执行口径": "Execution",
    "身份与审计": "Identity & audit",
    "净值点不足，无法绘图。": "Not enough equity observations to draw a chart.",
    "目标权重矩阵组合回测": "Target-weight matrix portfolio backtest",
    "矩阵组合研究": "Matrix portfolio research",
    "搜索索引键、月份、因子或周期…": "Search index key, month, factor, or horizon…",
    "搜索索引键、Run ID、月份或因子…": "Search index key, run ID, month, or factor…",
    "搜索索引键、区间、K线、因子或预测根数…": (
        "Search index key, period, bars, factor, or horizon…"
    ),
    "索引键": "Index key",
    "周期": "Horizon",
    "预测周期": "Forecast horizon",
    "策略多空分位差": "Strategy long-short quantile spread",
    "覆盖率": "Coverage",
    "策略方向为正比例": "Positive strategy-direction share",
    "查看报告": "View report",
    "成本口径": "Cost policy",
    "成本": "Cost",
    "收益": "Return",
    "详情": "Details",
    "统一执行口径：": "Shared execution policy: ",
    "快速研究层": "Quick Research",
    "分位差、覆盖率和 Rank 换手": "quantile spread, coverage, and Rank turnover",
    "组合净值、回撤、成本、换手与单 run 详情": (
        "portfolio equity, drawdown, costs, turnover, and per-run detail"
    ),
    "机器可读汇总": "Machine-readable summary",
    "供批处理、二次分析和精确复现使用": (
        "for batch processing, secondary analysis, and exact reproduction"
    ),
    "用途": "Role",
    "K线": "Bars",
    "预测": "Forecast",
    "预测根数": "Forecast bars",
    "有效时点": "Valid timestamps",
    "冻结合同：": "Frozen contract: ",
    "单独打开子报告 ↗": "Open child report ↗",
    "因子：": "Factor: ",
    "方向：": "Direction: ",
    "参数总览": "Parameter overview",
    "状态": "Status",
    "止盈": "Take profit",
    "止损": "Stop loss",
    "手续费拖累": "Fee drag",
    "滑点拖累": "Slippage drag",
    "冻结合同": "Frozen contract",
    "：全部": ": All",
    "这些结果是历史模拟，不构成投资建议，也不代表未来收益。": (
        "These results are historical simulations, not investment advice or future-return claims."
    ),
}


_TEXT_NODE = re.compile(r"(?<=>)([^<>]+)(?=<)")
_LOCALIZABLE_ATTRIBUTE = re.compile(
    r"(?P<prefix>\b(?:aria-label|placeholder|title)=(?P<quote>['\"]))"
    r"(?P<value>.*?)(?P=quote)"
)
_SUMMARY_CARD = re.compile(r"<span>([^<>]+)<small>([^<>]+)</small></span>")
_HAN = re.compile(r"[一-龥]")


def variant_path(path: Path, locale: ReportLocale) -> Path:
    """Return an explicit locale sibling without changing the compatibility path."""

    return path.with_name(f"{path.stem}.{locale}{path.suffix}")


def _replace_translations(value: str) -> str:
    for source in sorted(_ENGLISH, key=len, reverse=True):
        value = value.replace(source, _ENGLISH[source])
    return value


def _select_bilingual(value: str, locale: ReportLocale) -> str:
    if " / " not in value:
        return value
    left, right = value.split(" / ", 1)
    if not _HAN.search(left) or not re.search(r"[A-Za-z]", right):
        return value
    return left if locale == "zh-CN" else right


def localize_html(document: str, locale: ReportLocale) -> str:
    """Select one primary UI language while preserving source evidence verbatim."""

    selected = _SUMMARY_CARD.sub(
        lambda match: f"<span>{match.group(1) if locale == 'zh-CN' else match.group(2)}</span>",
        document,
    )
    if locale == "en":
        selected = _replace_translations(selected)

    def text_node(match: re.Match[str]) -> str:
        value = match.group(1)
        if locale == "en":
            value = _replace_translations(value)
        return _select_bilingual(value, locale)

    selected = _TEXT_NODE.sub(text_node, selected)

    def attribute(match: re.Match[str]) -> str:
        value = match.group("value")
        if locale == "en":
            value = _replace_translations(value)
        value = _select_bilingual(value, locale)
        return f"{match.group('prefix')}{value}{match.group('quote')}"

    selected = _LOCALIZABLE_ATTRIBUTE.sub(attribute, selected)
    selected = re.sub(r"<html\s+lang=(['\"]).*?\1", f'<html lang="{locale}"', selected, count=1)
    selected = selected.replace("bfbt", "BFBT")
    return selected


def html_variants(document: str) -> dict[ReportLocale, str]:
    """Return deterministic English and Simplified Chinese documents."""

    return {locale: localize_html(document, locale) for locale in REPORT_LOCALES}


def add_language_switch(
    document: str,
    *,
    locale: ReportLocale,
) -> str:
    """Add a path-independent language selector without external assets."""

    en_current = ' aria-current="page"' if locale == "en" else ""
    zh_current = ' aria-current="page"' if locale == "zh-CN" else ""
    navigation = (
        '<nav class="bfbt-language-switch" aria-label="Language">'
        f'<a href="" data-bfbt-language="en"{en_current}>English</a>'
        '<span aria-hidden="true"> · </span>'
        f'<a href="" data-bfbt-language="zh-CN"{zh_current}>中文</a>'
        "</nav>"
    )
    style = (
        "<style>.bfbt-language-switch{position:relative;z-index:50;max-width:1680px;"
        "margin:10px auto 0;padding:0 20px;text-align:right;font:13px/1.4 system-ui,"
        "sans-serif}.bfbt-language-switch a{color:inherit}.bfbt-language-switch "
        "a[aria-current=page]{font-weight:800;text-decoration:none}</style>"
    )
    script = (
        "<script>(function(){var p=window.location.pathname;"
        "var b=p.replace(/(?:\\.en|\\.zh-CN)?\\.html$/,\"\");"
        "document.querySelectorAll('[data-bfbt-language]').forEach(function(a){"
        "a.href=b+'.'+a.dataset.bfbtLanguage+'.html';});}());</script>"
    )
    body = re.search(r"<body(?:\s[^>]*)?>", document, flags=re.IGNORECASE)
    if body is None:
        return document
    return document[: body.end()] + style + navigation + script + document[body.end() :]


def write_html_variants(path: Path, document: str) -> dict[str, Path]:
    """Write compatibility English plus explicit English and Chinese files."""

    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    variants = html_variants(document)
    english = variant_path(path, "en")
    chinese = variant_path(path, "zh-CN")
    english_document = add_language_switch(
        variants["en"],
        locale="en",
    )
    chinese_document = add_language_switch(
        variants["zh-CN"],
        locale="zh-CN",
    )
    path.write_text(english_document, encoding="utf-8")
    english.write_text(english_document, encoding="utf-8")
    chinese.write_text(chinese_document, encoding="utf-8")
    return {"default": path, "en": english, "zh-CN": chinese}
