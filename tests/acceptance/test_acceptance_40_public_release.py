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
    assert "docs/assets/research-workflow.svg" in readme
    assert chinese.startswith("# BFBT\n")
    assert "不存在隶属、背书、赞助或任何利益关系" in chinese
    assert "```mermaid" not in chinese
    assert "docs/assets/research-workflow.zh-CN.svg" in chinese
    for name in ("research-workflow.svg", "research-workflow.zh-CN.svg"):
        root = ET.parse(ROOT / "docs" / "assets" / name).getroot()
        assert root.attrib["viewBox"] == "0 0 1200 960"
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
