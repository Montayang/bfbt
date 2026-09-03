from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from bfbt.compat import StrEnum
from bfbt.reports.locales import localize_html, variant_path, write_html_variants
from bfbt.reports.research_study import render_quick_only_study_report


ROOT = Path(__file__).resolve().parents[2]


class _CompatibilityValue(StrEnum):
    VALUE = "VALUE"


def test_public_identity_is_bfbt_with_english_front_door() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '\nname = "bfbt"\n' in project
    assert '\nbfbt = "bfbt.cli:app"\n' in project
    assert 'Repository = "https://github.com/Montayang/bfbt"' in project
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    assert readme.startswith("# BFBT\n")
    assert "not affiliated with" in readme
    assert "financially connected to Binance" in readme
    assert "```mermaid" not in readme
    assert "V2" not in readme
    assert "docs/assets/research-workflow.svg" in readme
    assert "docs/assets/three-layer-reports.svg" in readme
    assert "showcase/README.md" in readme
    assert "https://montayang.github.io/bfbt/reports/quick-research.en.html" in readme
    assert "https://montayang.github.io/bfbt/reports/fast-matrix.en.html" in readme
    assert "https://montayang.github.io/bfbt/reports/event-engine.en.html" in readme
    assert chinese.startswith("# BFBT\n")
    assert "不存在隶属、背书、赞助或任何利益关系" in chinese
    assert "```mermaid" not in chinese
    assert "V2" not in chinese
    assert "docs/assets/research-workflow.zh-CN.svg" in chinese
    assert "docs/assets/three-layer-reports.zh-CN.svg" in chinese
    assert "showcase/README.zh-CN.md" in chinese
    assert "https://montayang.github.io/bfbt/reports/quick-research.zh-CN.html" in chinese
    assert "https://montayang.github.io/bfbt/reports/fast-matrix.zh-CN.html" in chinese
    assert "https://montayang.github.io/bfbt/reports/event-engine.zh-CN.html" in chinese
    showcase_english = (ROOT / "showcase" / "README.md").read_text(encoding="utf-8")
    showcase_chinese = (ROOT / "showcase" / "README.zh-CN.md").read_text(encoding="utf-8")
    assert showcase_english.startswith("# Explore BFBT\n")
    assert "README.zh-CN.md" in showcase_english
    assert showcase_chinese.startswith("# 探索 BFBT\n")
    assert "README.md" in showcase_chinese
    for report_name in ("quick-research", "fast-matrix", "event-engine"):
        assert f"https://montayang.github.io/bfbt/reports/{report_name}.en.html" in showcase_english
        assert f"https://montayang.github.io/bfbt/reports/{report_name}.zh-CN.html" in showcase_chinese
        assert (ROOT / "site" / "reports" / f"{report_name}.en.html").is_file()
        assert (ROOT / "site" / "reports" / f"{report_name}.zh-CN.html").is_file()
    pages_workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in pages_workflow
    assert "path: site" in pages_workflow
    assert (ROOT / "site" / "index.html").is_file()
    assert (ROOT / "site" / "index.zh-CN.html").is_file()
    public_documents = (
        ("docs/README.md", "docs/README.zh-CN.md", "# BFBT documentation", "# BFBT 文档导航"),
        (
            "docs/guides/beginner_tutorial.md",
            "docs/guides/beginner_tutorial.zh-CN.md",
            "# BFBT beginner tutorial",
            "# BFBT 傻瓜式入门教程",
        ),
        (
            "docs/guides/user_manual.md",
            "docs/guides/user_manual.zh-CN.md",
            "# BFBT user manual",
            "# BFBT 用户使用手册",
        ),
        (
            "docs/guides/custom_factor_tutorial.md",
            "docs/guides/custom_factor_tutorial.zh-CN.md",
            "# Add a cross-sectional factor",
            "# 以现有 Amihud 实现为模板新增截面因子",
        ),
    )
    for english_path, chinese_path, english_title, chinese_title in public_documents:
        english_document = (ROOT / english_path).read_text(encoding="utf-8")
        chinese_document = (ROOT / chinese_path).read_text(encoding="utf-8")
        assert english_document.startswith(english_title)
        assert chinese_document.startswith(chinese_title)
        assert Path(chinese_path).name in english_document
        assert Path(english_path).name in chinese_document
    for name in ("research-workflow.svg", "research-workflow.zh-CN.svg"):
        root = ET.parse(ROOT / "docs" / "assets" / name).getroot()
        assert root.attrib["viewBox"] == "0 0 1100 920"
        names = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
        assert not names.intersection({"a", "button", "foreignObject", "script"})
        visible_text = "".join(root.itertext())
        assert "V2" not in visible_text
    for name in ("three-layer-reports.svg", "three-layer-reports.zh-CN.svg"):
        root = ET.parse(ROOT / "docs" / "assets" / name).getroot()
        assert root.attrib["viewBox"] == "0 0 1200 940"
        names = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
        assert not names.intersection({"a", "button", "foreignObject", "script"})


def test_declared_python_310_compatibility_surface() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in project
    assert str(_CompatibilityValue.VALUE) == _CompatibilityValue.VALUE.value == "VALUE"


def test_html_variants_are_separate_deterministic_documents(tmp_path: Path) -> None:
    source = (
        '<!doctype html><html lang="zh-CN"><body>'
        '<h1>回测报告 / Backtest Report</h1>'
        '<p>这些结果是历史模拟，不构成投资建议，也不代表未来收益。</p>'
        "</body></html>"
    )
    paths = write_html_variants(tmp_path / "report.html", source)
    english = paths["default"].read_text(encoding="utf-8")
    chinese = paths["zh-CN"].read_text(encoding="utf-8")
    assert english == paths["en"].read_text(encoding="utf-8")
    assert '<html lang="en">' in english
    assert "Backtest Report" in english
    assert "These results are historical simulations" in english
    assert 'data-bfbt-language="zh-CN"' in english
    assert '<html lang="zh-CN">' in chinese
    assert "回测报告" in chinese
    assert "这些结果是历史模拟" in chinese
    assert 'data-bfbt-language="en"' in chinese
    assert localize_html(source, "en") == localize_html(source, "en")


def test_html_localization_preserves_executable_and_literal_blocks() -> None:
    source = (
        '<!doctype html><html lang="zh-CN"><body>'
        '<p>成交前 / Before</p>'
        '<script>var changed = trades.length > 0 || grid < 4;'
        'var label = "成交前 / Before";</script>'
        '<script type="application/json">{"label":"成交前 / Before"}</script>'
        '<style>.row > span { color: green; }</style>'
        '<pre><code>value > 0 / source evidence</code></pre>'
        '</body></html>'
    )

    english = localize_html(source, "en")
    chinese = localize_html(source, "zh-CN")

    assert "<p>Before</p>" in english
    assert "<p>成交前</p>" in chinese
    assert 'trades.length > 0 || grid < 4' in english
    assert 'trades.length > 0 || grid < 4' in chinese
    assert 'var label = "Before"' in english
    assert 'var label = "成交前"' in chinese
    assert '{"label":"成交前 / Before"}' in english
    assert '{"label":"成交前 / Before"}' in chinese
    assert '.row > span { color: green; }' in english
    assert 'value > 0 / source evidence' in english


def test_html_localization_selects_bilingual_text_before_translation() -> None:
    source = (
        '<!doctype html><html lang="zh-CN"><body>'
        '<h2>核心结果 / Performance</h2>'
        '<p>跨行中文说明 /\nMultiline English explanation</p>'
        '<span title="因子原始排序与未来Return排序的截面 Spearman 相关系数">因子</span>'
        '<script>var label = "核心结果 / Performance";</script>'
        '</body></html>'
    )

    english = localize_html(source, "en")
    assert "Performance / Performance" not in english
    assert "<h2>Performance</h2>" in english
    assert "<p>Multiline English explanation</p>" in english
    assert ">Factor</span>" in english
    assert "Cross-sectional Spearman correlation" in english
    assert 'var label = "Performance"' in english


def test_quick_research_report_publishes_both_languages(tmp_path: Path) -> None:
    root = tmp_path / "study"
    root.mkdir()
    summary = {
        "status": "succeeded",
        "study_id": "quick-a40",
        "title": "Quick Research A40",
        "contract": {"holdout": "2026-07"},
        "results": [
            {
                "period": "2026-07",
                "role": "holdout",
                "bar_interval": "1h",
                "factor_code": "MOM24",
                "horizon_bars": 1,
                "mean_rank_ic": 0.02,
                "rank_ic_ir": 0.3,
                "q5_minus_q1": 0.001,
                "factor_coverage": 0.99,
                "mean_rank_turnover": 0.08,
                "rank_ic_positive_fraction": 0.55,
                "timestamps": 10,
            }
        ],
    }
    (root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    default = render_quick_only_study_report(root)
    english = variant_path(default, "en")
    chinese = variant_path(default, "zh-CN")
    assert default.is_file() and english.is_file() and chinese.is_file()
    assert default.read_bytes() == english.read_bytes()
    assert "This report is Quick Research only" in default.read_text(encoding="utf-8")
    assert "本报告只做快速研究" in chinese.read_text(encoding="utf-8")
