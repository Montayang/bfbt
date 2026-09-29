"""A43: safe contracts and recorded hand-offs for the general Agent workflow."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

from bfbt.agent.contracts import (
    AgentActionClass,
    AgentFactorSpec,
    AgentResearchIntent,
    AgentWorkflowError,
    AuthorizationGrant,
    CostAssumptions,
    ExecutionSemantics,
    ResearchObjective,
)
from bfbt.agent.planner import plan_agent_workflow, render_workflow_plan
from bfbt.agent.workflow import AgentWorkflowStore
from bfbt.cli import app
from bfbt.data.hashing import sha256_bytes
from bfbt.data.preparation import (
    CoverageEvidence,
    DataPrepareJob,
    DataReadiness,
    DataPurpose,
    DataWorkspace,
    DatasetRequirement,
    ResearchDataRequirement,
    SnapshotSelector,
)
from bfbt.factors.expression import (
    FactorExpressionError,
    compile_factor_expression,
    compute_expression_factor,
)

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)


def _intent(
    *,
    objective: ResearchObjective = ResearchObjective.FACTOR_DIAGNOSTIC,
    ambiguities: tuple[str, ...] = (),
    turnover: float | None = None,
) -> AgentResearchIntent:
    text = "Research five-bar cross-sectional momentum on BTC and ETH."
    data = ResearchDataRequirement(
        purpose=(
            DataPurpose.QUICK_RESEARCH
            if objective == ResearchObjective.FACTOR_DIAGNOSTIC
            else DataPurpose.FAST_MATRIX
        ),
        symbols=("BTCUSDT", "ETHUSDT"),
        core_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        core_end=datetime(2024, 1, 2, tzinfo=timezone.utc),
        base_interval="1m",
        datasets=(DatasetRequirement(dataset_name="bars", interval="1m"),),
        factor_warmup_bars=5,
        label_future_bars=1,
    )
    return AgentResearchIntent(
        user_text=text,
        user_text_sha256=sha256_bytes(text.encode()),
        objective=objective,
        title="Five-bar momentum study",
        factor=AgentFactorSpec(
            kind="registered", name="momentum", version="v1",
            direction="positive", parameters={"lookback_bars": 5},
        ),
        data=data,
        semantics=ExecutionSemantics(
            base_interval="1m", factor_interval="5m", decision_interval="5m",
            rebalance_interval="1h", fill_timing="next_bar_open",
            portfolio_side="long_only", selection_rule="top_n",
            selection_size=2, sizing="equal_weight", gross_exposure=1,
            leverage=1, risk_model="none", terminal_handling="force_close",
        ),
        costs=CostAssumptions(
            fee_bps_per_side=4, slippage_bps_per_side=2,
            include_funding=True, expected_turnover_per_rebalance=turnover,
            expected_gross_return_bps=50,
        ),
        unresolved_ambiguities=ambiguities,
        requested_outputs=("bilingual_report", "machine_summary"),
    )


def test_expression_language_is_causal_bounded_and_group_isolated() -> None:
    compiled = compile_factor_expression("close / lag(close, 2) - 1")
    assert compiled.warmup_bars == 2
    assert compiled.required_columns == ("close",)
    frame = pl.DataFrame({
        "symbol": ["A", "A", "A", "B", "B", "B"],
        "close": [1.0, 2.0, 4.0, 10.0, 20.0, 40.0],
    })
    values = frame.with_columns(compiled.expression.alias("factor"))["factor"].to_list()
    assert values == [None, None, 3.0, None, None, 3.0]
    with pytest.raises(FactorExpressionError):
        compile_factor_expression("__import__('os').system('id')")
    with pytest.raises(FactorExpressionError):
        compile_factor_expression("lag(close, -1)")


def test_expression_identity_must_match_validated_source() -> None:
    compiled = compile_factor_expression("rolling_mean(close, 5) / close - 1")
    spec = AgentFactorSpec(
        kind="expression", name="mean_distance", version=compiled.expression_id,
        direction="negative", expression=compiled.source,
    )
    assert spec.warmup_bars == 4
    with pytest.raises(ValueError, match="expression version"):
        AgentFactorSpec(
            kind="expression", name="mean_distance", version="fx-incorrect",
            direction="negative", expression=compiled.source,
        )


def test_expression_factor_resets_history_at_gaps_and_is_point_in_time() -> None:
    rows = []
    for symbol, minutes in (("A", (0, 1, 2)), ("B", (0, 2, 3))):
        for minute in minutes:
            rows.append({
                "open_time": NOW + timedelta(minutes=minute),
                "close_time": NOW + timedelta(minutes=minute + 1),
                "symbol": symbol, "interval": "1m", "close": float(minute + 1),
                "is_complete": True, "dataset_version": "bars-a43",
            })
    bars = pl.DataFrame(rows).lazy()
    universe = pl.DataFrame([
        {
            "timestamp": NOW + timedelta(minutes=minute + 1),
            "symbol": symbol, "is_eligible": True,
            "universe_version": "universe-a43",
        }
        for symbol, minutes in (("A", (0, 1, 2)), ("B", (0, 2, 3)))
        for minute in minutes
    ]).lazy()
    result = compute_expression_factor(
        bars, universe, source="close / lag(close, 2) - 1",
        factor_name="safe_momentum", direction="positive",
        base_interval="1m", compute_interval="1m",
        bars_dataset_version="bars-a43", universe_version="universe-a43",
    ).frame.collect()
    a = result.filter(pl.col("symbol") == "A")["is_valid"].to_list()
    b = result.filter(pl.col("symbol") == "B")["is_valid"].to_list()
    assert a == [False, False, True]
    assert b == [False, False, False]


def test_plan_is_read_only_bilingual_and_blocks_ambiguity(tmp_path: Path) -> None:
    workspace = DataWorkspace(tmp_path / "absent")
    intent = _intent(ambiguities=("Whether to rank ascending or descending",))
    first = plan_agent_workflow(intent, data_workspace=workspace)
    second = plan_agent_workflow(intent, data_workspace=workspace)
    assert first == second
    assert first.executable is False
    assert "UNRESOLVED_AMBIGUITY" in first.blockers
    assert "Agent 研究工作流计划" in render_workflow_plan(first, "zh-CN")
    assert not workspace.root.exists()


def test_portfolio_plan_has_cost_gate_and_manual_event_boundary(tmp_path: Path) -> None:
    intent = _intent(
        objective=ResearchObjective.FORMAL_BACKTEST,
        turnover=1.0,
    )
    plan = plan_agent_workflow(intent, data_workspace=DataWorkspace(tmp_path / "data"))
    assert plan.backend == "fast_matrix_then_event"
    assert [item.value for item in plan.route][-3:] == [
        "manual_selection", "event_formal", "evidence_summary"
    ]
    assert "COST_DRAG_CONFIRMATION_REQUIRED" in plan.confirmations
    assert plan.estimated_cost_drag_bps is not None
    assert plan.estimated_cost_drag_bps > 100


def test_job_pauses_until_exact_actions_and_confirmations_are_granted(
    tmp_path: Path,
) -> None:
    intent = _intent()
    plan = plan_agent_workflow(intent, data_workspace=DataWorkspace(tmp_path / "data"))
    store = AgentWorkflowStore(tmp_path / "jobs", now=lambda: NOW)
    with pytest.raises(AgentWorkflowError, match="write approval"):
        store.create(intent, plan, allow_job_write=False)
    job = store.create(intent, plan, allow_job_write=True)
    assert job.status == "awaiting_authorization"
    assert job.stages[0].reason_code == "CONFIRMATION_REQUIRED"

    acknowledgements = plan.confirmations
    for action in (
        AgentActionClass.DATA_WRITE,
        AgentActionClass.NETWORK,
        AgentActionClass.DATA_DOWNLOAD,
    ):
        grant = AuthorizationGrant(
            plan_hash=plan.plan_hash,
            action_class=action,
            approved_by="owner",
            issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            acknowledgement_codes=acknowledgements,
        )
        job = store.authorize(job.job_id, grant, allow_job_write=True)
    assert job.status == "awaiting_evidence"
    assert job.stages[0].status == "ready"
    assert "data_readiness" in (job.stages[0].detail or "")
    assert store.create(intent, plan, allow_job_write=True) == job


def test_factor_workflow_records_verified_handoffs_and_finishes(
    tmp_path: Path,
) -> None:
    intent = _intent()
    plan = plan_agent_workflow(intent, data_workspace=DataWorkspace(tmp_path / "data"))
    assert plan.data_plan is not None
    store = AgentWorkflowStore(tmp_path / "agent-jobs", now=lambda: NOW)
    job = store.create(intent, plan, allow_job_write=True)
    for action in (
        AgentActionClass.DATA_WRITE,
        AgentActionClass.NETWORK,
        AgentActionClass.DATA_DOWNLOAD,
    ):
        job = store.authorize(
            job.job_id,
            AuthorizationGrant(
                plan_hash=plan.plan_hash, action_class=action, approved_by="owner",
                issued_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
                acknowledgement_codes=plan.confirmations,
            ),
            allow_job_write=True,
        )

    data_job_id = f"de1-{plan.data_plan.plan_hash[:24]}"
    data_job_dir = tmp_path / "data-jobs" / data_job_id
    data_job_dir.mkdir(parents=True)
    (data_job_dir / "plan.json").write_text(
        json.dumps(plan.data_plan.model_dump(mode="json")), encoding="utf-8"
    )
    readiness = DataReadiness(
        readiness_id="ready-a43",
        preparation_job_id=data_job_id,
        requirement_hash=plan.data_plan.requirement_hash,
        plan_id=plan.data_plan.plan_id,
        status="ready",
        snapshot=SnapshotSelector(dataset_id="a43", dataset_version="v1"),
        snapshot_sha256="1" * 64,
        lineage_sha256="2" * 64,
        coverage=(CoverageEvidence(
            dataset_name="bars", dataset_version="bars-v1",
            required_from=intent.data.required_start,
            required_to=intent.data.required_end,
            available_from=intent.data.required_start,
            available_to=intent.data.required_end,
            partition_manifest_ids=("part-a43",),
            quality_report_ids=("quality-a43",),
            status="ready", detail="offline fixture",
        ),),
        derived_intervals=(), purposes=(DataPurpose.QUICK_RESEARCH,),
        estimated_scan_rows=2,
    )
    readiness_path = data_job_dir / "readiness.json"
    readiness_path.write_text(
        json.dumps(readiness.model_dump(mode="json")), encoding="utf-8"
    )
    data_job = DataPrepareJob(
        job_id=data_job_id,
        plan_id=plan.data_plan.plan_id,
        plan_hash=plan.data_plan.plan_hash,
        requirement_hash=plan.data_plan.requirement_hash,
        status="succeeded", created_at=NOW, updated_at=NOW,
        steps=(), snapshot=readiness.snapshot,
        readiness_path="readiness.json", lineage_path="lineage.json",
    )
    (data_job_dir / "job.json").write_text(
        json.dumps(data_job.model_dump(mode="json")), encoding="utf-8"
    )
    job = store.record_evidence(job.job_id, readiness_path, allow_job_write=True)
    assert job.status == "awaiting_authorization"
    assert job.stages[0].status == "succeeded"

    job = store.authorize(
        job.job_id,
        AuthorizationGrant(
            plan_hash=plan.plan_hash, action_class=AgentActionClass.RESEARCH,
            approved_by="owner", issued_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            acknowledgement_codes=plan.confirmations,
        ),
        allow_job_write=True,
    )
    quick = tmp_path / "quick-summary.json"
    quick.write_text(
        json.dumps({"status": "succeeded", "study_id": "quick-a43"}),
        encoding="utf-8",
    )
    job = store.record_evidence(job.job_id, quick, allow_job_write=True)
    assert job.stages[1].status == "succeeded"
    cited = [
        item.sha256 for stage in job.stages for item in stage.evidence
    ]
    summary = tmp_path / "agent-summary.json"
    summary.write_text(json.dumps({
        "summary_version": "agent-evidence-summary/v1",
        "summary_id": "summary-a43",
        "plan_hash": plan.plan_hash,
        "outcome": "completed",
        "evidence_sha256": cited,
        "claims": [{
            "claim": "The study completed.", "severity": "fact",
            "evidence_sha256": [cited[-1]],
        }],
        "limitations": [],
    }), encoding="utf-8")
    job = store.record_evidence(job.job_id, summary, allow_job_write=True)
    assert job.status == "succeeded"
    assert [item.attempts for item in job.stages] == [1, 1, 1]


def test_grants_are_plan_bound_and_expiring(tmp_path: Path) -> None:
    intent = _intent()
    plan = plan_agent_workflow(intent, data_workspace=DataWorkspace(tmp_path / "data"))
    store = AgentWorkflowStore(tmp_path / "jobs", now=lambda: NOW)
    job = store.create(intent, plan, allow_job_write=True)
    wrong = AuthorizationGrant(
        plan_hash="0" * 64, action_class=AgentActionClass.DATA_WRITE,
        approved_by="owner", issued_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
    )
    with pytest.raises(AgentWorkflowError, match="another plan"):
        store.authorize(job.job_id, wrong, allow_job_write=True)
    expired = wrong.model_copy(update={
        "plan_hash": plan.plan_hash,
        "expires_at": NOW - timedelta(seconds=1),
    })
    with pytest.raises(AgentWorkflowError, match="expired"):
        store.authorize(job.job_id, expired, allow_job_write=True)


def test_cli_exposes_schema_validation_and_side_effect_free_plan(
    tmp_path: Path,
) -> None:
    intent_path = tmp_path / "intent.json"
    intent_path.write_text(json.dumps(_intent().model_dump(mode="json")), encoding="utf-8")
    workspace = tmp_path / "absent"
    runner = CliRunner()
    schema = runner.invoke(app, ["agent", "schema", "intent"])
    validation = runner.invoke(app, ["agent", "validate", str(intent_path)])
    plan = runner.invoke(app, [
        "agent", "plan", str(intent_path), "--data-workspace", str(workspace),
        "--format", "json",
    ])
    assert schema.exit_code == 0, schema.output
    assert "agent-research-intent/v1" in schema.output
    assert validation.exit_code == 0, validation.output
    assert "intent_hash=" in validation.output
    assert plan.exit_code == 0, plan.output
    assert json.loads(plan.output)["plan_version"] == "agent-workflow-plan/v1"
    assert not workspace.exists()


def test_public_agent_intent_example_is_valid() -> None:
    intent = AgentResearchIntent.model_validate_json(
        (Path(__file__).resolve().parents[2] / "configs/agent_intent.example.json")
        .read_bytes()
    )
    assert intent.objective == ResearchObjective.PORTFOLIO_RESEARCH
    assert intent.executable is True
