from __future__ import annotations

from pathlib import Path

import pytest

from bianbt.reports.event_study import (
    EventStudyReportError,
    render_event_parameter_study,
)


def _candidate(profile: str, report: str) -> dict[str, object]:
    return {
        "profile_id": profile,
        "report_href": report,
        "run_id": "a17-0123456789abcdef01234567",
        "status": "succeeded",
        "take_profit": 0.036 if profile != "BASE" else None,
        "stop_loss": 0.02 if profile != "BASE" else None,
        "trade_count": 12,
        "stop_loss_count": 2,
        "take_profit_count": 3,
        "metrics": {
            "performance": {"total_return": 0.12, "max_drawdown": -0.08},
            "risk": {"total_turnover": 9.5},
            "attribution": {"fee_contribution": 0.01, "slippage_contribution": 0.0025},
        },
    }


def test_parent_report_indexes_independent_child_sheets(tmp_path: Path) -> None:
    summary = {
        "study_version": "event-parameter-study/v1",
        "study_id": "event-study-a36",
        "title": "R5 Event Study",
        "factor_name": "sampled_mean_ratio",
        "direction": "POS",
        "contract": {"sample_lags": list(range(0, 166, 15))},
        "candidates": [
            _candidate("BASE", "children/BASE.html"),
            _candidate("F2", "children/F2.html"),
        ],
    }
    output = render_event_parameter_study(summary, output_path=tmp_path / "report.html")
    page = output.read_text(encoding="utf-8")
    assert "BASE" in page and "F2" in page
    assert "children/BASE.html" in page and "children/F2.html" in page
    assert "data-open-sheet='F2'" in page
    assert "EVENT PARAMETER STUDY" in page
    assert "https://" not in page and "http://" not in page


def test_parent_report_keeps_failed_profile_without_fake_metrics(tmp_path: Path) -> None:
    failed = _candidate("BASE", "children/BASE.html")
    failed.update(
        status="failed",
        trade_count=None,
        stop_loss_count=None,
        take_profit_count=None,
        metrics={},
    )
    summary = {
        "study_version": "event-parameter-study/v1",
        "study_id": "event-study-failed-a36",
        "candidates": [failed],
    }
    page = render_event_parameter_study(
        summary, output_path=tmp_path / "report.html"
    ).read_text(encoding="utf-8")
    assert "failed" in page
    assert "0.00%" not in page
    assert page.count("—") >= 7


@pytest.mark.parametrize("href", ["../run/report.html", "/tmp/report.html", "report.json"])
def test_parent_report_rejects_unsafe_child_paths(tmp_path: Path, href: str) -> None:
    summary = {
        "study_version": "event-parameter-study/v1",
        "candidates": [_candidate("F1", href)],
    }
    with pytest.raises(EventStudyReportError):
        render_event_parameter_study(summary, output_path=tmp_path / "report.html")
