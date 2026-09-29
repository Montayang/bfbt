"""Versioned contracts for the general supervised Agent workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from bfbt.compat import StrEnum
from bfbt.config.common import StrictModel, as_utc
from bfbt.config.durations import duration_seconds, is_integer_multiple
from bfbt.data.hashing import content_sha256, sha256_bytes
from bfbt.data.preparation import DataPlan, ResearchDataRequirement
from bfbt.factors.expression import compile_factor_expression
from bfbt.factors.registry import FACTOR_REGISTRY


INTENT_VERSION = "agent-research-intent/v1"
PLAN_VERSION = "agent-workflow-plan/v1"
AUTHORIZATION_VERSION = "agent-authorization/v1"
JOB_VERSION = "agent-workflow-job/v1"
DECISION_VERSION = "agent-promotion-decision/v1"
SUMMARY_VERSION = "agent-evidence-summary/v1"


class AgentWorkflowError(ValueError):
    """The Agent workflow cannot proceed safely."""


class ResearchObjective(StrEnum):
    FACTOR_DIAGNOSTIC = "factor_diagnostic"
    PORTFOLIO_RESEARCH = "portfolio_research"
    FORMAL_BACKTEST = "formal_backtest"
    RESULT_QUERY = "result_query"


class AgentActionClass(StrEnum):
    READ_ONLY = "read_only_inspection"
    TEST = "test_execution"
    NETWORK = "network_access"
    DATA_DOWNLOAD = "data_download"
    DATA_WRITE = "data_write"
    RESEARCH = "research_execution"
    FORMAL_EVENT = "formal_event_execution"
    SOURCE_CONTROL = "source_control_change"


class WorkflowStageName(StrEnum):
    DATA = "data_prepare"
    QUICK = "quick_research"
    MATRIX = "fast_matrix"
    SELECTION = "manual_selection"
    EVENT = "event_formal"
    EXPLAIN = "evidence_summary"


class AgentFactorSpec(StrictModel):
    kind: Literal["registered", "expression"]
    name: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    version: str = Field(min_length=1)
    direction: Literal["positive", "negative"]
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    expression: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_factor(self) -> "AgentFactorSpec":
        if self.kind == "registered":
            if self.expression is not None:
                raise ValueError("registered factors cannot include an expression")
            registered = FACTOR_REGISTRY.get(self.name)
            if registered is None:
                raise ValueError(f"registered factor does not exist: {self.name}")
            if registered.version != self.version:
                raise ValueError(
                    f"factor version mismatch: expected {registered.version}"
                )
        else:
            if self.expression is None:
                raise ValueError("expression factors require expression")
            compiled = compile_factor_expression(self.expression)
            if self.version != compiled.expression_id:
                raise ValueError(
                    f"expression version must equal {compiled.expression_id}"
                )
            if self.parameters:
                raise ValueError("expression factors do not accept hidden parameters")
        return self

    @property
    def warmup_bars(self) -> int:
        if self.kind == "expression":
            assert self.expression is not None
            return compile_factor_expression(self.expression).warmup_bars
        values = [
            int(value) for key, value in self.parameters.items()
            if isinstance(value, int) and not isinstance(value, bool)
            and any(word in key for word in ("window", "lookback", "period", "span"))
        ]
        return max(values, default=0)


class ExecutionSemantics(StrictModel):
    base_interval: str
    factor_interval: str
    decision_interval: str
    rebalance_interval: str
    signal_availability: Literal["bar_close"] = "bar_close"
    fill_timing: Literal["next_bar_open", "same_bar_trigger"]
    rank_rule: Literal["score_desc_symbol_asc"] = "score_desc_symbol_asc"
    portfolio_side: Literal["long_only", "short_only", "long_short"]
    selection_rule: Literal["top_n", "quantiles"]
    selection_size: int = Field(ge=1)
    sizing: Literal["equal_weight", "score_weight", "inverse_volatility"]
    gross_exposure: float = Field(gt=0, le=10)
    leverage: float = Field(gt=0, le=125)
    risk_model: Literal["none", "fixed_exits", "path_dependent"]
    terminal_handling: Literal["force_close", "mark_open"]

    @field_validator(
        "base_interval", "factor_interval", "decision_interval", "rebalance_interval"
    )
    @classmethod
    def valid_interval(cls, value: str) -> str:
        duration_seconds(value)
        return value

    @model_validator(mode="after")
    def validate_clocks(self) -> "ExecutionSemantics":
        for value in (
            self.factor_interval, self.decision_interval, self.rebalance_interval
        ):
            if not is_integer_multiple(value, self.base_interval):
                raise ValueError(f"{value} must be a multiple of base_interval")
        if self.fill_timing == "same_bar_trigger" and self.risk_model != "path_dependent":
            raise ValueError("same_bar_trigger is reserved for path-dependent Event risk")
        return self


class CostAssumptions(StrictModel):
    fee_bps_per_side: float = Field(ge=0, le=100)
    slippage_bps_per_side: float = Field(ge=0, le=1_000)
    include_funding: bool
    expected_turnover_per_rebalance: float | None = Field(default=None, ge=0, le=20)
    expected_gross_return_bps: float | None = Field(default=None, ge=-100_000, le=100_000)
    max_acceptable_cost_share: float = Field(default=0.5, gt=0, le=1)


class IntentDecision(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    value: str = Field(min_length=1, max_length=500)
    decided_by: str = Field(min_length=1, max_length=120)
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked


class ResultReference(StrictModel):
    kind: Literal["quick_research", "matrix_run", "event_run", "report", "metrics"]
    identity: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentResearchIntent(StrictModel):
    intent_version: Literal["agent-research-intent/v1"] = INTENT_VERSION
    user_text: str = Field(min_length=1, max_length=20_000)
    user_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    objective: ResearchObjective
    title: str = Field(min_length=1, max_length=200)
    factor: AgentFactorSpec
    data: ResearchDataRequirement
    semantics: ExecutionSemantics
    costs: CostAssumptions
    assumptions: tuple[str, ...] = ()
    unresolved_ambiguities: tuple[str, ...] = ()
    decisions: tuple[IntentDecision, ...] = ()
    requested_outputs: tuple[str, ...] = Field(min_length=1)
    result_references: tuple[ResultReference, ...] = ()

    @model_validator(mode="after")
    def validate_intent(self) -> "AgentResearchIntent":
        if sha256_bytes(self.user_text.encode("utf-8")) != self.user_text_sha256:
            raise ValueError("user_text_sha256 does not match user_text")
        if self.data.base_interval != self.semantics.base_interval:
            raise ValueError("data and execution base_interval must match")
        if self.data.factor_warmup_bars < self.factor.warmup_bars:
            raise ValueError("data factor warmup is shorter than the factor requirement")
        if len({item.key for item in self.decisions}) != len(self.decisions):
            raise ValueError("decision keys must be unique")
        if self.objective == ResearchObjective.RESULT_QUERY and not self.result_references:
            raise ValueError("result_query requires result_references")
        return self

    @property
    def executable(self) -> bool:
        return not self.unresolved_ambiguities and not self.data.unresolved

    @property
    def intent_hash(self) -> str:
        return content_sha256(self)


class AgentIssue(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    severity: Literal["info", "warning", "confirmation", "blocker"]
    detail: str = Field(min_length=1)


class WorkflowAction(StrictModel):
    ordinal: int = Field(ge=1)
    stage: WorkflowStageName
    action_class: AgentActionClass
    detail: str = Field(min_length=1)
    input_hashes: tuple[str, ...] = ()


class SemanticFreeze(StrictModel):
    factor_identity: str
    factor_direction: Literal["positive", "negative"]
    timing: str
    ranking: str
    portfolio: str
    execution: str
    costs: str
    risk: str
    terminal: str


class WorkflowPlan(StrictModel):
    plan_version: Literal["agent-workflow-plan/v1"] = PLAN_VERSION
    plan_id: str
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    route: tuple[WorkflowStageName, ...]
    backend: Literal["none", "fast_matrix", "event", "fast_matrix_then_event"]
    freeze: SemanticFreeze
    data_plan: DataPlan | None = None
    actions: tuple[WorkflowAction, ...]
    issues: tuple[AgentIssue, ...]
    estimated_rebalances: int = Field(ge=0)
    estimated_cost_drag_bps: float | None = Field(default=None, ge=0)

    @property
    def blockers(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.issues if item.severity == "blocker")

    @property
    def confirmations(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.issues if item.severity == "confirmation")

    @property
    def executable(self) -> bool:
        return not self.blockers

    @property
    def plan_hash(self) -> str:
        return content_sha256(self)


class AuthorizationGrant(StrictModel):
    authorization_version: Literal["agent-authorization/v1"] = AUTHORIZATION_VERSION
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_class: AgentActionClass
    approved_by: str = Field(min_length=1, max_length=120)
    issued_at: datetime
    expires_at: datetime
    acknowledgement_codes: tuple[str, ...] = ()
    note: str = Field(default="", max_length=1_000)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def auth_utc(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @model_validator(mode="after")
    def valid_period(self) -> "AuthorizationGrant":
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization expiry must follow issue time")
        return self

    @property
    def grant_id(self) -> str:
        return f"grant-{content_sha256(self)[:24]}"


class EvidenceReference(StrictModel):
    kind: Literal[
        "data_readiness", "quick_research", "matrix_candidates", "promotion_decision",
        "event_run", "evidence_summary",
    ]
    identity: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MatrixCandidateReference(StrictModel):
    run_id: str = Field(pattern=r"^fm-[0-9a-f]{24}$")
    manifest_path: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MatrixCandidateEvidence(StrictModel):
    evidence_version: Literal["agent-matrix-candidates/v1"] = (
        "agent-matrix-candidates/v1"
    )
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidates: tuple[MatrixCandidateReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_candidates(self) -> "MatrixCandidateEvidence":
        ids = [item.run_id for item in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("matrix candidate run IDs must be unique")
        return self

    @property
    def candidate_set_id(self) -> str:
        return f"matrix-set-{content_sha256(self)[:24]}"


class PromotionDecision(StrictModel):
    decision_version: Literal["agent-promotion-decision/v1"] = DECISION_VERSION
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_matrix_run_id: str = Field(pattern=r"^fm-[0-9a-f]{24}$")
    rejected_matrix_run_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=4_000)
    decided_by: str = Field(min_length=1, max_length=120)
    decided_at: datetime
    event_overrides_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("decided_at")
    @classmethod
    def decision_utc(cls, value: datetime) -> datetime:
        checked = as_utc(value)
        assert checked is not None
        return checked

    @property
    def decision_id(self) -> str:
        return f"promotion-{content_sha256(self)[:24]}"


class WorkflowStage(StrictModel):
    ordinal: int = Field(ge=1)
    name: WorkflowStageName
    required_actions: tuple[AgentActionClass, ...]
    status: Literal["pending", "ready", "running", "paused", "succeeded", "failed", "skipped"]
    attempts: int = Field(default=0, ge=0)
    evidence: tuple[EvidenceReference, ...] = ()
    reason_code: str | None = None
    detail: str | None = None


class AgentWorkflowJob(StrictModel):
    job_version: Literal["agent-workflow-job/v1"] = JOB_VERSION
    job_id: str = Field(pattern=r"^agent-[0-9a-f]{24}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal[
        "awaiting_authorization", "awaiting_evidence", "awaiting_selection",
        "running", "succeeded", "failed",
    ]
    created_at: datetime
    updated_at: datetime
    stages: tuple[WorkflowStage, ...]
    authorization_grant_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_detail: str | None = None


class EvidenceClaim(StrictModel):
    claim: str = Field(min_length=1, max_length=2_000)
    severity: Literal["fact", "qualification", "warning"]
    evidence_sha256: tuple[str, ...] = Field(min_length=1)

    @field_validator("evidence_sha256")
    @classmethod
    def valid_hashes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in values):
            raise ValueError("claim evidence must contain SHA-256 values")
        return values


class AgentEvidenceSummary(StrictModel):
    summary_version: Literal["agent-evidence-summary/v1"] = SUMMARY_VERSION
    summary_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome: Literal["completed", "qualified", "failed"]
    claims: tuple[EvidenceClaim, ...] = Field(min_length=1)
    evidence_sha256: tuple[str, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def claims_are_cited(self) -> "AgentEvidenceSummary":
        declared = set(self.evidence_sha256)
        used = {digest for claim in self.claims for digest in claim.evidence_sha256}
        if not used.issubset(declared):
            raise ValueError("claims cite evidence absent from evidence_sha256")
        return self

