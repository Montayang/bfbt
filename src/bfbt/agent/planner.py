"""Side-effect-free planning for supervised natural-language research."""

from __future__ import annotations

import math

from bfbt.agent.contracts import (
    AgentActionClass,
    AgentIssue,
    AgentResearchIntent,
    ResearchObjective,
    SemanticFreeze,
    WorkflowAction,
    WorkflowPlan,
    WorkflowStageName,
)
from bfbt.config.durations import duration_seconds
from bfbt.data.hashing import content_sha256
from bfbt.data.preparation import DataWorkspace, plan_data_preparation
from bfbt.factors.expression import compile_factor_expression


def _route(intent: AgentResearchIntent) -> tuple[WorkflowStageName, ...]:
    if intent.objective == ResearchObjective.RESULT_QUERY:
        return (WorkflowStageName.EXPLAIN,)
    if intent.objective == ResearchObjective.FACTOR_DIAGNOSTIC:
        return (
            WorkflowStageName.DATA,
            WorkflowStageName.QUICK,
            WorkflowStageName.EXPLAIN,
        )
    if intent.objective == ResearchObjective.PORTFOLIO_RESEARCH:
        return (
            WorkflowStageName.DATA,
            WorkflowStageName.QUICK,
            WorkflowStageName.MATRIX,
            WorkflowStageName.EXPLAIN,
        )
    return (
        WorkflowStageName.DATA,
        WorkflowStageName.QUICK,
        WorkflowStageName.MATRIX,
        WorkflowStageName.SELECTION,
        WorkflowStageName.EVENT,
        WorkflowStageName.EXPLAIN,
    )


def _backend(intent: AgentResearchIntent) -> str:
    if intent.objective in {
        ResearchObjective.FACTOR_DIAGNOSTIC, ResearchObjective.RESULT_QUERY
    }:
        return "none"
    if intent.objective == ResearchObjective.PORTFOLIO_RESEARCH:
        return "fast_matrix"
    return "fast_matrix_then_event"


def _freeze(intent: AgentResearchIntent) -> SemanticFreeze:
    semantics = intent.semantics
    if intent.factor.kind == "expression":
        assert intent.factor.expression is not None
        factor_identity = compile_factor_expression(
            intent.factor.expression
        ).expression_id
    else:
        factor_identity = f"{intent.factor.name}/{intent.factor.version}"
    return SemanticFreeze(
        factor_identity=factor_identity,
        factor_direction=intent.factor.direction,
        timing=(
            f"base={semantics.base_interval}; factor={semantics.factor_interval}; "
            f"decision={semantics.decision_interval}; rebalance={semantics.rebalance_interval}; "
            f"available={semantics.signal_availability}"
        ),
        ranking=semantics.rank_rule,
        portfolio=(
            f"side={semantics.portfolio_side}; rule={semantics.selection_rule}; "
            f"size={semantics.selection_size}; sizing={semantics.sizing}; "
            f"gross={semantics.gross_exposure}; leverage={semantics.leverage}"
        ),
        execution=f"fill={semantics.fill_timing}",
        costs=(
            f"fee={intent.costs.fee_bps_per_side}bps/side; "
            f"slippage={intent.costs.slippage_bps_per_side}bps/side; "
            f"funding={str(intent.costs.include_funding).lower()}"
        ),
        risk=semantics.risk_model,
        terminal=semantics.terminal_handling,
    )


def _costs(intent: AgentResearchIntent) -> tuple[int, float | None, list[AgentIssue]]:
    if intent.objective not in {
        ResearchObjective.PORTFOLIO_RESEARCH,
        ResearchObjective.FORMAL_BACKTEST,
    }:
        return 0, None, []
    seconds = (intent.data.core_end - intent.data.core_start).total_seconds()
    rebalances = max(
        1, math.ceil(seconds / duration_seconds(intent.semantics.rebalance_interval))
    )
    turnover = intent.costs.expected_turnover_per_rebalance
    if turnover is None:
        return rebalances, None, [AgentIssue(
            code="TURNOVER_ESTIMATE_REQUIRED",
            severity="blocker",
            detail="portfolio research requires expected_turnover_per_rebalance",
        )]
    drag = rebalances * turnover * (
        intent.costs.fee_bps_per_side + intent.costs.slippage_bps_per_side
    )
    issues: list[AgentIssue] = []
    gross = intent.costs.expected_gross_return_bps
    excessive = drag >= 100
    if gross is not None and gross > 0:
        excessive = drag / gross > intent.costs.max_acceptable_cost_share
    if excessive:
        issues.append(AgentIssue(
            code="COST_DRAG_CONFIRMATION_REQUIRED",
            severity="confirmation",
            detail=(
                f"estimated trading drag is {drag:.2f} bps across {rebalances} "
                "rebalances; explicit acknowledgement is required"
            ),
        ))
    return rebalances, drag, issues


def plan_agent_workflow(
    intent: AgentResearchIntent,
    *,
    data_workspace: DataWorkspace,
) -> WorkflowPlan:
    """Build a canonical local plan without writing, networking, or executing research."""

    route = _route(intent)
    backend = _backend(intent)
    issues: list[AgentIssue] = []
    for ambiguity in (*intent.unresolved_ambiguities, *intent.data.unresolved):
        issues.append(AgentIssue(
            code="UNRESOLVED_AMBIGUITY",
            severity="blocker",
            detail=ambiguity,
        ))
    data_plan = None
    if WorkflowStageName.DATA in route:
        data_plan = plan_data_preparation(intent.data, workspace=data_workspace)
        issues.extend(
            AgentIssue(
                code=f"DATA_{item.code}",
                severity=("blocker" if item.severity.value == "blocker" else "warning"),
                detail=item.detail,
            )
            for item in data_plan.issues
        )
    rebalances, drag, cost_issues = _costs(intent)
    issues.extend(cost_issues)
    issues.append(AgentIssue(
        code="SEMANTIC_CONFIRMATION_REQUIRED",
        severity="confirmation",
        detail="factor, clocks, ranking, portfolio, costs, risk, and terminal handling must be acknowledged",
    ))
    if intent.semantics.risk_model == "path_dependent" and backend == "fast_matrix":
        issues.append(AgentIssue(
            code="EVENT_BACKEND_REQUIRED",
            severity="blocker",
            detail="path-dependent risk cannot be represented by a Fast Matrix-only objective",
        ))

    actions: list[WorkflowAction] = []

    def add(stage: WorkflowStageName, action: AgentActionClass, detail: str) -> None:
        actions.append(WorkflowAction(
            ordinal=len(actions) + 1,
            stage=stage,
            action_class=action,
            detail=detail,
            input_hashes=(intent.intent_hash,),
        ))

    for stage in route:
        if stage == WorkflowStageName.DATA:
            assert data_plan is not None
            add(stage, AgentActionClass.DATA_WRITE, "prepare or reuse one exact DatasetSnapshot")
            if data_plan.requires_network:
                add(stage, AgentActionClass.NETWORK, "access public Binance market-data endpoints")
                add(stage, AgentActionClass.DATA_DOWNLOAD, "download the planned immutable Raw objects")
        elif stage in {WorkflowStageName.QUICK, WorkflowStageName.MATRIX}:
            add(stage, AgentActionClass.RESEARCH, f"execute {stage.value}")
        elif stage == WorkflowStageName.EVENT:
            add(stage, AgentActionClass.FORMAL_EVENT, "execute the confirmed formal Event run")
        else:
            add(stage, AgentActionClass.READ_ONLY, f"perform {stage.value}")

    freeze = _freeze(intent)
    payload = {
        "version": "agent-workflow-plan/v1",
        "intent_hash": intent.intent_hash,
        "route": [item.value for item in route],
        "backend": backend,
        "freeze": freeze.model_dump(mode="json"),
        "data_plan_hash": data_plan.plan_hash if data_plan else None,
        "actions": [item.model_dump(mode="json") for item in actions],
        "issues": [item.model_dump(mode="json") for item in issues],
        "estimated_rebalances": rebalances,
        "estimated_cost_drag_bps": drag,
    }
    return WorkflowPlan(
        plan_id=f"agent-plan-{content_sha256(payload)[:24]}",
        intent_hash=intent.intent_hash,
        route=route,
        backend=backend,  # type: ignore[arg-type]
        freeze=freeze,
        data_plan=data_plan,
        actions=tuple(actions),
        issues=tuple(sorted(issues, key=lambda item: (item.severity, item.code, item.detail))),
        estimated_rebalances=rebalances,
        estimated_cost_drag_bps=drag,
    )


def render_workflow_plan(plan: WorkflowPlan, language: str = "en") -> str:
    if language not in {"en", "zh-CN"}:
        raise ValueError("language must be en or zh-CN")
    zh = language == "zh-CN"
    labels = {
        "title": "Agent 研究工作流计划" if zh else "Agent research workflow plan",
        "route": "路线" if zh else "route",
        "backend": "后端" if zh else "backend",
        "cost": "预计交易成本拖累" if zh else "estimated trading drag",
        "freeze": "语义冻结" if zh else "semantic freeze",
        "actions": "授权动作" if zh else "authorization actions",
        "issues": "问题与确认" if zh else "issues and confirmations",
        "none": "无" if zh else "none",
    }
    lines = [
        f"{labels['title']} {plan.plan_id}",
        f"{labels['route']}: " + " -> ".join(item.value for item in plan.route),
        f"{labels['backend']}: {plan.backend}",
        f"{labels['cost']}: " + (
            "—" if plan.estimated_cost_drag_bps is None
            else f"{plan.estimated_cost_drag_bps:.2f} bps"
        ),
        f"{labels['freeze']}:",
    ]
    for key, value in plan.freeze.model_dump(mode="json").items():
        lines.append(f"  {key}: {value}")
    lines.append(f"{labels['actions']}:")
    lines.extend(
        f"  {item.ordinal}. [{item.action_class.value}] {item.stage.value}: {item.detail}"
        for item in plan.actions
    )
    lines.append(f"{labels['issues']}:")
    lines.extend(
        [f"  [{item.severity}] {item.code}: {item.detail}" for item in plan.issues]
        or [f"  {labels['none']}"]
    )
    return "\n".join(lines)
