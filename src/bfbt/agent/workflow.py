"""Recorded, resumable hand-off protocol for supervised Agent workflows."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from bfbt.agent.contracts import (
    AgentActionClass,
    AgentResearchIntent,
    AgentEvidenceSummary,
    AgentWorkflowJob,
    AgentWorkflowError,
    AuthorizationGrant,
    EvidenceReference,
    MatrixCandidateEvidence,
    PromotionDecision,
    WorkflowPlan,
    WorkflowStage,
    WorkflowStageName,
)
from bfbt.artifacts.matrix import MatrixResearchManifest, MatrixResearchStore
from bfbt.artifacts.store import RunArtifactStore
from bfbt.data.hashing import canonical_json_bytes, sha256_file
from bfbt.data.manifests import RunManifest, load_manifest_auto
from bfbt.data.preparation import DataPlan, DataPrepareJob, DataReadiness


_EVIDENCE_KIND = {
    WorkflowStageName.DATA: "data_readiness",
    WorkflowStageName.QUICK: "quick_research",
    WorkflowStageName.MATRIX: "matrix_candidates",
    WorkflowStageName.SELECTION: "promotion_decision",
    WorkflowStageName.EVENT: "event_run",
    WorkflowStageName.EXPLAIN: "evidence_summary",
}


def _atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bytes(model) -> bytes:
    return canonical_json_bytes(model) + b"\n"


class AgentWorkflowStore:
    """Store control-plane truth without executing research or shell commands."""

    def __init__(self, root: Path, *, now=lambda: datetime.now(timezone.utc)) -> None:
        self.root = root.resolve()
        if self.root in {Path("/"), Path.home().resolve()}:
            raise AgentWorkflowError("unsafe Agent job root")
        self.now = now

    def directory(self, job_id: str) -> Path:
        if not job_id.startswith("agent-") or "/" in job_id or ".." in job_id:
            raise AgentWorkflowError("unsafe Agent job ID")
        return self.root / job_id

    def load(self, job_id: str) -> AgentWorkflowJob:
        path = self.directory(job_id) / "job.json"
        if not path.is_file():
            raise AgentWorkflowError(f"Agent job not found: {job_id}")
        return AgentWorkflowJob.model_validate_json(path.read_bytes())

    def _plan(self, job_id: str) -> WorkflowPlan:
        return WorkflowPlan.model_validate_json(
            (self.directory(job_id) / "plan.json").read_bytes()
        )

    def _intent(self, job_id: str) -> AgentResearchIntent:
        return AgentResearchIntent.model_validate_json(
            (self.directory(job_id) / "intent.json").read_bytes()
        )

    def create(
        self,
        intent: AgentResearchIntent,
        plan: WorkflowPlan,
        *,
        allow_job_write: bool,
    ) -> AgentWorkflowJob:
        if not allow_job_write:
            raise AgentWorkflowError("creating an Agent job requires write approval")
        if plan.intent_hash != intent.intent_hash:
            raise AgentWorkflowError("plan does not belong to intent")
        if not plan.executable:
            raise AgentWorkflowError("workflow plan is blocked: " + ",".join(plan.blockers))
        job_id = f"agent-{plan.plan_hash[:24]}"
        directory = self.directory(job_id)
        path = directory / "job.json"
        if path.is_file():
            job = self.load(job_id)
            if job.plan_hash != plan.plan_hash or job.intent_hash != intent.intent_hash:
                raise AgentWorkflowError("Agent job identity collision")
            return job
        stages = []
        for ordinal, stage in enumerate(plan.route, start=1):
            required = tuple(dict.fromkeys(
                action.action_class for action in plan.actions
                if action.stage == stage
                and action.action_class != AgentActionClass.READ_ONLY
            ))
            stages.append(WorkflowStage(
                ordinal=ordinal,
                name=stage,
                required_actions=required,
                status="pending",
            ))
        current = self.now()
        job = AgentWorkflowJob(
            job_id=job_id,
            plan_hash=plan.plan_hash,
            intent_hash=intent.intent_hash,
            status="awaiting_authorization",
            created_at=current,
            updated_at=current,
            stages=tuple(stages),
        )
        _atomic(directory / "intent.json", _bytes(intent))
        _atomic(directory / "plan.json", _bytes(plan))
        _atomic(path, _bytes(job))
        return self.refresh(job_id)

    def _grants(self, job_id: str) -> tuple[AuthorizationGrant, ...]:
        directory = self.directory(job_id) / "authorizations"
        if not directory.is_dir():
            return ()
        return tuple(
            AuthorizationGrant.model_validate_json(path.read_bytes())
            for path in sorted(directory.glob("grant-*.json"))
        )

    def authorize(
        self,
        job_id: str,
        grant: AuthorizationGrant,
        *,
        allow_job_write: bool,
    ) -> AgentWorkflowJob:
        if not allow_job_write:
            raise AgentWorkflowError("recording authorization requires write approval")
        job = self.load(job_id)
        if grant.plan_hash != job.plan_hash:
            raise AgentWorkflowError("authorization is bound to another plan")
        if grant.expires_at <= self.now():
            raise AgentWorkflowError("authorization is already expired")
        plan = self._plan(job_id)
        required = {item.action_class for item in plan.actions}
        if grant.action_class not in required:
            raise AgentWorkflowError("authorization action is not present in the plan")
        _atomic(
            self.directory(job_id) / "authorizations" / f"{grant.grant_id}.json",
            _bytes(grant),
        )
        return self.refresh(job_id)

    def _authorization_state(
        self, job_id: str, stage: WorkflowStage
    ) -> tuple[set[AgentActionClass], set[str]]:
        job = self.load(job_id)
        plan = self._plan(job_id)
        current = self.now()
        grants = tuple(
            item for item in self._grants(job_id)
            if item.plan_hash == job.plan_hash
            and item.issued_at <= current < item.expires_at
        )
        actions = {item.action_class for item in grants}
        acknowledged = {
            code for item in grants for code in item.acknowledgement_codes
        }
        if not set(plan.confirmations).issubset(acknowledged):
            return actions, set(plan.confirmations) - acknowledged
        return actions, set()

    def refresh(self, job_id: str) -> AgentWorkflowJob:
        job = self.load(job_id)
        stages = list(job.stages)
        first = next((index for index, item in enumerate(stages) if item.status != "succeeded"), None)
        if first is None:
            status = "succeeded"
        else:
            stage = stages[first]
            actions, missing_confirmations = self._authorization_state(job_id, stage)
            missing_actions = set(stage.required_actions) - actions
            if stage.name == WorkflowStageName.SELECTION:
                status = "awaiting_selection"
                stage_status = "paused"
                reason = "USER_SELECTION_REQUIRED"
            elif missing_actions or missing_confirmations:
                status = "awaiting_authorization"
                stage_status = "paused"
                reason = (
                    "CONFIRMATION_REQUIRED" if missing_confirmations
                    else "AUTHORIZATION_REQUIRED"
                )
            else:
                status = "awaiting_evidence"
                stage_status = "ready"
                reason = "EVIDENCE_REQUIRED"
            stage = stage.model_copy(update={
                "status": stage_status,
                "reason_code": reason,
                "detail": self._request_detail(
                    stage, missing_actions, missing_confirmations
                ),
            })
            stages[first] = stage
        updated = job.model_copy(update={
            "status": status,
            "updated_at": self.now(),
            "stages": tuple(stages),
            "authorization_grant_ids": tuple(
                item.grant_id for item in self._grants(job_id)
            ),
        })
        _atomic(self.directory(job_id) / "job.json", _bytes(updated))
        return updated

    @staticmethod
    def _request_detail(
        stage: WorkflowStage,
        missing_actions: set[AgentActionClass],
        missing_confirmations: set[str],
    ) -> str:
        if missing_confirmations:
            return "acknowledge: " + ",".join(sorted(missing_confirmations))
        if missing_actions:
            return "authorize: " + ",".join(sorted(item.value for item in missing_actions))
        return f"provide verified {_EVIDENCE_KIND[stage.name]} evidence"

    def record_evidence(
        self,
        job_id: str,
        evidence_path: Path,
        *,
        allow_job_write: bool,
    ) -> AgentWorkflowJob:
        if not allow_job_write:
            raise AgentWorkflowError("recording evidence requires write approval")
        job = self.refresh(job_id)
        stage_index = next(
            (index for index, item in enumerate(job.stages) if item.status != "succeeded"),
            None,
        )
        if stage_index is None:
            return job
        stage = job.stages[stage_index]
        if stage.status not in {"ready", "paused"}:
            raise AgentWorkflowError("stage is not ready for evidence")
        actions, missing_confirmations = self._authorization_state(job_id, stage)
        if set(stage.required_actions) - actions or missing_confirmations:
            raise AgentWorkflowError("stage lacks required authorization or confirmation")
        reference = self._verify_evidence(job_id, stage.name, evidence_path.resolve())
        completed = stage.model_copy(update={
            "status": "succeeded",
            "attempts": stage.attempts + 1,
            "evidence": (reference,),
            "reason_code": None,
            "detail": None,
        })
        stages = list(job.stages)
        stages[stage_index] = completed
        updated = job.model_copy(update={
            "status": "running",
            "updated_at": self.now(),
            "stages": tuple(stages),
        })
        _atomic(self.directory(job_id) / "job.json", _bytes(updated))
        return self.refresh(job_id)

    def _verify_evidence(
        self, job_id: str, stage: WorkflowStageName, path: Path
    ) -> EvidenceReference:
        if not path.is_file():
            raise AgentWorkflowError(f"evidence file does not exist: {path}")
        plan = self._plan(job_id)
        expected = _EVIDENCE_KIND[stage]
        identity: str
        if stage == WorkflowStageName.DATA:
            value = DataReadiness.model_validate_json(path.read_bytes())
            if plan.data_plan is None or value.requirement_hash != plan.data_plan.requirement_hash:
                raise AgentWorkflowError("DataReadiness does not match workflow data plan")
            if value.status != "ready":
                raise AgentWorkflowError("data evidence is not ready")
            data_job_path = path.parent / "job.json"
            data_plan_path = path.parent / "plan.json"
            if not data_job_path.is_file() or not data_plan_path.is_file():
                raise AgentWorkflowError("DataReadiness lacks its recorded DE-v1 job and plan")
            data_job = DataPrepareJob.model_validate_json(data_job_path.read_bytes())
            recorded_plan = DataPlan.model_validate_json(data_plan_path.read_bytes())
            if (
                data_job.status != "succeeded"
                or data_job.readiness_path != path.name
                or data_job.plan_hash != recorded_plan.plan_hash
                or recorded_plan.plan_hash != plan.data_plan.plan_hash
                or value.preparation_job_id != data_job.job_id
            ):
                raise AgentWorkflowError("DataReadiness chain does not match the reviewed plan")
            identity = value.readiness_id
        elif stage == WorkflowStageName.QUICK:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("status") != "succeeded":
                raise AgentWorkflowError("Quick Research summary is not succeeded")
            identity = str(value.get("study_id", ""))
            if not identity:
                raise AgentWorkflowError("Quick Research summary lacks study_id")
        elif stage == WorkflowStageName.MATRIX:
            candidates = MatrixCandidateEvidence.model_validate_json(path.read_bytes())
            if candidates.plan_hash != plan.plan_hash:
                raise AgentWorkflowError("Matrix candidates belong to another plan")
            for candidate in candidates.candidates:
                manifest_path = Path(candidate.manifest_path).resolve()
                if not manifest_path.is_file():
                    raise AgentWorkflowError(
                        f"Matrix candidate manifest is missing: {candidate.run_id}"
                    )
                if sha256_file(manifest_path) != candidate.manifest_sha256:
                    raise AgentWorkflowError(
                        f"Matrix candidate manifest hash changed: {candidate.run_id}"
                    )
                manifest = MatrixResearchManifest.model_validate_json(
                    manifest_path.read_bytes()
                )
                loaded = MatrixResearchStore(manifest_path.parent.parent).load(
                    candidate.run_id
                )
                if loaded != manifest or manifest.run_id != candidate.run_id:
                    raise AgentWorkflowError("Fast Matrix evidence identity mismatch")
            identity = candidates.candidate_set_id
        elif stage == WorkflowStageName.SELECTION:
            decision = PromotionDecision.model_validate_json(path.read_bytes())
            if decision.plan_hash != plan.plan_hash:
                raise AgentWorkflowError("promotion decision belongs to another plan")
            matrix_ids: set[str] = set()
            for completed_stage in self.load(job_id).stages:
                for item in completed_stage.evidence:
                    if item.kind == "matrix_candidates":
                        candidate_set = MatrixCandidateEvidence.model_validate_json(
                            Path(item.path).read_bytes()
                        )
                        matrix_ids.update(
                            candidate.run_id for candidate in candidate_set.candidates
                        )
            if decision.selected_matrix_run_id not in matrix_ids:
                raise AgentWorkflowError("promotion selects an unverified Matrix run")
            identity = decision.decision_id
        elif stage == WorkflowStageName.EVENT:
            manifest = load_manifest_auto(path)
            if not isinstance(manifest, RunManifest) or manifest.status != "succeeded":
                raise AgentWorkflowError("Event evidence is not a succeeded run")
            RunArtifactStore.verify(path.parent, manifest)
            decisions = [
                item for completed_stage in self.load(job_id).stages
                for item in completed_stage.evidence
                if item.kind == "promotion_decision"
            ]
            if len(decisions) != 1:
                raise AgentWorkflowError("Event evidence requires one promotion decision")
            decision = PromotionDecision.model_validate_json(
                Path(decisions[0].path).read_bytes()
            )
            config_path = path.parent / "resolved_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            backtest = config.get("backtest", config) if isinstance(config, dict) else {}
            engine = backtest.get("engine", {}) if isinstance(backtest, dict) else {}
            if engine.get("source_matrix_run_id") != decision.selected_matrix_run_id:
                raise AgentWorkflowError(
                    "Event run is not bound to the selected Matrix candidate"
                )
            identity = manifest.run_id
        else:
            value = AgentEvidenceSummary.model_validate_json(path.read_bytes())
            if value.plan_hash != plan.plan_hash:
                raise AgentWorkflowError("evidence summary belongs to another plan")
            cited = value.evidence_sha256
            previous = {
                item.sha256 for item in self.load(job_id).stages
                for item in item.evidence
            }
            intent = self._intent(job_id)
            referenced = set()
            for item in intent.result_references:
                referenced_path = Path(item.path).resolve()
                if (
                    not referenced_path.is_file()
                    or sha256_file(referenced_path) != item.sha256
                ):
                    raise AgentWorkflowError(
                        f"result reference is missing or changed: {item.identity}"
                    )
                referenced.add(item.sha256)
            if not set(cited).issubset(previous | referenced):
                raise AgentWorkflowError("summary cites unknown evidence")
            identity = value.summary_id
        return EvidenceReference(
            kind=expected,  # type: ignore[arg-type]
            identity=identity,
            path=str(path),
            sha256=sha256_file(path),
        )
