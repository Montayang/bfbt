"""A39: bounded Agent intent, verified evidence, doctor, and showcase hub."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from bfbt.artifacts.store import RunArtifactStore
from bfbt.cli import app
from bfbt.data.hashing import sha256_bytes, sha256_file
from bfbt.data.manifests import (
    ArtifactHash,
    FactorVersionReference,
    RunDatasetReference,
    RunManifest,
    SchemaVersionReference,
    manifest_json,
)
from bfbt.data.schemas import get_schema_definition
from bfbt.showcase.doctor import doctor
from bfbt.showcase.models import ShowcaseSpec
from bfbt.showcase.service import ShowcaseError, build_showcase, inspect_showcase


UTC = timezone.utc


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fixture_run(root: Path, run_id: str, *, dirty: bool = True) -> None:
    run = root / run_id
    tables = run / "tables"
    tables.mkdir(parents=True)
    pl.DataFrame(
        {
            "fill_time": [
                datetime(2026, 5, 2, tzinfo=UTC),
                datetime(2026, 5, 3, tzinfo=UTC),
            ],
            "symbol": ["BTCUSDT", "BTCUSDT"],
            "side": ["BUY", "SELL"],
            "notional": [1_000.0, 1_050.0],
        }
    ).write_parquet(tables / "trades.parquet")
    _write_json(
        run / "metrics.json",
        {
            "performance": {
                "initial_equity": 2_000.0,
                "ending_equity": 2_100.0,
                "total_return": 0.05,
                "max_drawdown": -0.02,
                "sharpe_ratio": 1.2,
            },
            "risk": {"total_turnover": 3.5},
            "attribution": {
                "gross_price_contribution": 0.06,
                "fee_contribution": 0.008,
                "slippage_contribution": 0.002,
                "funding_contribution": 0.0,
            },
        },
    )
    _write_json(
        run / "resolved_config.json",
        {
            "factor": {
                "factors": [
                    {
                        "name": "sampled_mean_ratio",
                        "parameters": {"sample_count": 12, "sample_interval": "2h"},
                    }
                ]
            },
            "backtest": {
                "run": {
                    "name": "R5-T4-H2-ROLLING-202605-r01",
                    "start": "2026-05-01T00:00:00Z",
                    "end": "2026-06-01T00:00:00Z",
                },
                "engine": {"backend": "event"},
                "risk": {
                    "leverage": 5.0,
                    "symbol_exits": {"stop_loss": {}, "trailing_stop": {}},
                },
                "portfolio": {"sizing": {"mode": "rolling_margin"}},
                "execution": {
                    "fill_price": "next_bar_open",
                    "fee": {"model": "fixed_bps", "taker_bps": 4.0},
                    "slippage": {"model": "fixed_bps", "bps": 1.0},
                    "funding": {"enabled": True, "missing_policy": "assume_zero"},
                },
            },
        },
    )
    _write_json(
        run / "environment.json",
        {
            "git_commit": "abcdef1",
            "git_dirty": dirty,
            "source_fingerprint": "b" * 64,
            "python_version": "3.12.3",
            "dependency_fingerprint": "c" * 64,
        },
    )
    _write_json(run / "warnings.json", ["funding_assume_zero:fixture"])
    _write_json(
        run / "run_metadata.json",
        {
            "execution_mode": "chunked_v2",
            "factor_versions": [
                {"factor_name": "sampled_mean_ratio", "factor_version": "v1-fixture"}
            ],
        },
    )
    (run / "report.html").write_text("<!doctype html><title>fixture</title>\n", encoding="utf-8")
    artifacts = tuple(
        ArtifactHash(
            path=path.relative_to(run).as_posix(),
            byte_size=path.stat().st_size,
            sha256=sha256_file(path),
        )
        for path in sorted(item for item in run.rglob("*") if item.is_file())
    )
    bars = get_schema_definition("bars", "v1")
    manifest = RunManifest(
        run_id=run_id,
        created_at=datetime(2026, 5, 1, tzinfo=UTC),
        completed_at=datetime(2026, 6, 1, tzinfo=UTC),
        status="succeeded",
        git_commit="abcdef1",
        python_version="3.12.3",
        dependency_fingerprint="c" * 64,
        dataset_refs=(
            RunDatasetReference(
                dataset_id="fixture", dataset_version="v1-fixture", manifest_sha256="d" * 64
            ),
        ),
        schema_versions=(
            SchemaVersionReference(
                dataset_name="bars",
                schema_version="v1",
                schema_fingerprint=bars.fingerprint,
            ),
        ),
        resolved_config_hash="e" * 64,
        factor_versions=(
            FactorVersionReference(
                factor_name="sampled_mean_ratio", factor_version="v1-fixture"
            ),
        ),
        random_seed=42,
        artifact_hashes=artifacts,
        warnings_count=1,
    )
    (run / "manifest.json").write_text(manifest_json(manifest), encoding="utf-8")
    RunArtifactStore.verify(run, manifest)


def _spec(run_id: str, *, unresolved: tuple[str, ...] = ()) -> ShowcaseSpec:
    user_text = "比较两小时采样策略。"
    return ShowcaseSpec.model_validate(
        {
            "showcase_id": "fixture-showcase",
            "title": "Fixture Showcase",
            "subtitle": "Verified deterministic evidence",
            "strategy_identity": "R5-T4-H2-ROLLING",
            "intent": {
                "operation": "result_query",
                "user_text": user_text,
                "user_text_sha256": sha256_bytes(user_text.encode("utf-8")),
                "market": {},
                "periods": [
                    {
                        "label": "2026-05",
                        "start": "2026-05-01T00:00:00Z",
                        "end": "2026-06-01T00:00:00Z",
                    }
                ],
                "factor": {
                    "name": "sampled_mean_ratio",
                    "version": "v1",
                    "direction": "positive",
                    "parameters": {"sample_count": 12, "sample_interval": "2h"},
                },
                "semantics": {
                    "universe": "point-in-time",
                    "rank_rule": "rank descent",
                    "decision_clock": "1m",
                    "rebalance_clock": "1m",
                    "fill_timing": "next open",
                    "sizing": "rolling margin",
                    "costs": "explicit",
                    "risk_exits": "path dependent",
                    "terminal_handling": "explicit tail",
                },
                "unresolved_ambiguities": list(unresolved),
                "user_decisions": ["Event/V2"],
                "requested_outputs": ["comparison"],
                "required_actions": ["read_only_inspection", "derived_report_write"],
            },
            "runs": [
                {"run_id": run_id, "label": "May", "period_label": "2026-05"}
            ],
            "narrative": ["Evidence first."],
            "disclosures": ["Not investment advice."],
        }
    )


def test_a39_research_intent_hash_and_ambiguity_gate(tmp_path: Path) -> None:
    run_id = "a09-fixture"
    _fixture_run(tmp_path / "runs", run_id)
    payload = _spec(run_id).model_dump(mode="json")
    payload["intent"]["user_text_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="does not match"):
        ShowcaseSpec.model_validate(payload)
    with pytest.raises(ShowcaseError, match="unresolved economic ambiguities"):
        build_showcase(
            _spec(run_id, unresolved=("fee model",)),
            runs_root=tmp_path / "runs",
            output_root=tmp_path / "showcases",
        )


def test_a39_verified_evidence_and_deterministic_static_hub(tmp_path: Path) -> None:
    run_id = "a09-fixture"
    runs_root = tmp_path / "runs"
    output_root = tmp_path / "showcases"
    _fixture_run(runs_root, run_id)
    spec = _spec(run_id)
    evidence = inspect_showcase(spec, runs_root=runs_root, output_root=output_root)
    run = evidence["runs"][0]
    assert evidence["summary"] == {
        "run_count": 1,
        "verified_run_count": 1,
        "dirty_run_count": 1,
        "warning_count": 1,
        "provenance_status": "qualified",
    }
    assert run["performance"]["total_return"] == pytest.approx(0.05)
    assert run["trade_count"] == 2
    assert run["opening_margin_trajectory"][0]["margin"] == pytest.approx(200.0)
    page, first = build_showcase(spec, runs_root=runs_root, output_root=output_root)
    first_html = page.read_bytes()
    first_en = (page.parent / "index.en.html").read_bytes()
    first_zh = (page.parent / "index.zh-CN.html").read_bytes()
    first_json = (page.parent / "evidence.json").read_bytes()
    page, second = build_showcase(spec, runs_root=runs_root, output_root=output_root)
    assert first == second
    assert page.read_bytes() == first_html
    assert (page.parent / "index.en.html").read_bytes() == first_en
    assert (page.parent / "index.zh-CN.html").read_bytes() == first_zh
    assert (page.parent / "evidence.json").read_bytes() == first_json
    html = first_html.decode("utf-8")
    assert html == first_en.decode("utf-8")
    assert '<html lang="en">' in html
    assert '<html lang="zh-CN">' in first_zh.decode("utf-8")
    assert "QUALIFIED" in html
    assert "5.00%" in html
    assert "May 2026" in html
    assert "2026 年 5 月" in first_zh.decode("utf-8")
    assert "retained in their source language for auditability" in html
    assert "funding_assume_zero" in html
    assert "funding_assume_zero:fixture" not in html
    assert "funding_assume_zero:fixture" in first_json.decode("utf-8")
    assert "http://" not in html and "https://" not in html
    assert str(tmp_path) not in html and str(tmp_path) not in first_json.decode()


def test_a39_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    run_id = "a09-fixture"
    runs_root = tmp_path / "runs"
    _fixture_run(runs_root, run_id)
    (runs_root / run_id / "metrics.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ShowcaseError, match="artifact verification failed"):
        inspect_showcase(
            _spec(run_id),
            runs_root=runs_root,
            output_root=tmp_path / "showcases",
        )


def test_a39_never_writes_showcase_inside_immutable_runs(tmp_path: Path) -> None:
    run_id = "a09-fixture"
    runs_root = tmp_path / "runs"
    _fixture_run(runs_root, run_id)
    with pytest.raises(ShowcaseError, match="outside immutable runs_root"):
        build_showcase(
            _spec(run_id),
            runs_root=runs_root,
            output_root=runs_root,
        )


def test_a39_intent_must_match_exact_run_identity(tmp_path: Path) -> None:
    run_id = "a09-fixture"
    runs_root = tmp_path / "runs"
    _fixture_run(runs_root, run_id)
    payload = _spec(run_id).model_dump(mode="json")
    payload["intent"]["factor"]["parameters"]["sample_interval"] = "1h"
    with pytest.raises(ShowcaseError, match="factor parameters do not match"):
        inspect_showcase(
            ShowcaseSpec.model_validate(payload),
            runs_root=runs_root,
            output_root=tmp_path / "showcases",
        )


def test_a39_doctor_is_read_only_and_cli_is_discoverable(tmp_path: Path) -> None:
    run_id = "a09-fixture"
    runs_root = tmp_path / "runs"
    output_root = tmp_path / "not-created" / "showcases"
    _fixture_run(runs_root, run_id)
    result = doctor(
        project_root=Path(__file__).resolve().parents[2],
        output_root=output_root,
        spec=_spec(run_id),
        runs_root=runs_root,
    )
    assert result["ready"] is True
    assert result["summary"]["warned"] == 1
    assert not output_root.exists()
    runner = CliRunner()
    assert runner.invoke(app, ["doctor", "--help"]).exit_code == 0
    assert runner.invoke(app, ["showcase", "--help"]).exit_code == 0
    assert runner.invoke(app, ["showcase", "build", "--help"]).exit_code == 0
