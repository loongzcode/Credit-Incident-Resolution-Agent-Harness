from enum import StrEnum
from typing import Annotated
from pydantic import AwareDatetime, Field, StrictInt
from credit_harness.domain.models import Model
from credit_harness.context.structured_values import OpaqueSubjectRef
from credit_harness.context.models import Hash
from credit_harness.cases.models import CaseStatus
from credit_harness.evaluation.models import VerificationRequirement, EvidenceRef
from credit_harness.recovery.models import RecoveryResult


class WorkType(StrEnum):
    INVESTIGATION_RESUME = "INVESTIGATION_RESUME"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    RECOVERY_RECHECK = "RECOVERY_RECHECK"
    OPERATOR_FOLLOWUP = "OPERATOR_FOLLOWUP"


class WorkStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    CANCELED = "CANCELED"
    BLOCKED = "BLOCKED"


class WorkBindingMode(StrEnum):
    CASE_SNAPSHOT = "CASE_SNAPSHOT"
    SIDE_EFFECT = "SIDE_EFFECT"


class WorkReason(StrEnum):
    WAIT_REQUESTED = "WAIT_REQUESTED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    DEPLOYMENT_STATE_UNKNOWN = "DEPLOYMENT_STATE_UNKNOWN"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    EFFECT_APPLIED = "EFFECT_APPLIED"
    EFFECT_UNRESOLVED = "EFFECT_UNRESOLVED"
    EFFECT_FAILED = "EFFECT_FAILED"
    EVALUATION_INCONCLUSIVE = "EVALUATION_INCONCLUSIVE"
    EVALUATION_FAILED = "EVALUATION_FAILED"
    RESUME_LIMIT = "RESUME_LIMIT"
    VERIFICATION_LIMIT = "VERIFICATION_LIMIT"
    NO_PROGRESS = "NO_PROGRESS"
    INTERRUPTED_RUN = "INTERRUPTED_RUN"
    NO_TOOL = "NO_TOOL"
    TOOL_BUDGET_EXHAUSTED = "TOOL_BUDGET_EXHAUSTED"


class SignalType(StrEnum):
    OPERATOR_ACKNOWLEDGED = "OPERATOR_ACKNOWLEDGED"
    CAPABILITY_AVAILABLE = "CAPABILITY_AVAILABLE"
    DEPLOYMENT_STATE_UPDATED = "DEPLOYMENT_STATE_UPDATED"
    SOURCE_RECOVERED = "SOURCE_RECOVERED"


class Trigger(StrEnum):
    TIMER = "TIMER"
    RESOLUTION_SIGNAL = "RESOLUTION_SIGNAL"
    SIDE_EFFECT = "SIDE_EFFECT"
    EVALUATION = "EVALUATION"
    RECOVERY = "RECOVERY"


class WorkEvent(StrEnum):
    RECOVERY_REBASED = "RECOVERY_REBASED"
    REARMED = "REARMED"
    CREATED = "CREATED"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RESUMED = "RESUMED"
    STARTED = "STARTED"
    COMPLETED = "COMPLETED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    CANCELED = "CANCELED"


class OrchestrationError(RuntimeError):
    """Sanitized orchestration boundary failure."""


class OrchestrationBudget(Model):
    max_resume_cycles: Annotated[StrictInt, Field(ge=0, le=20)] = 5
    max_verification_cycles: Annotated[StrictInt, Field(ge=0, le=20)] = 5
    max_no_progress_cycles: Annotated[StrictInt, Field(ge=1, le=20)] = 3


class CaseOrchestrationState(Model):
    case_id: OpaqueSubjectRef
    tenant_id: OpaqueSubjectRef
    budget: OrchestrationBudget = OrchestrationBudget()
    resume_cycle_count: int = 0
    verification_cycle_count: int = 0
    no_progress_count: int = 0  # Investigation / Verification only; legacy values are preserved.
    pending_work_count: int = 0
    last_work_item_id: Hash | None = None
    last_progress_fingerprint: Hash | None = None
    last_progress_version: str | None = None


class PauseReservation(Model):
    decision_id: Hash
    snapshot_id: Hash
    run_id: OpaqueSubjectRef
    reason: WorkReason
    wait_seconds: Annotated[StrictInt, Field(ge=30, le=3600)] | None = None
    required_signal: SignalType | None = None


class ResolutionSignal(Model):
    signal_id: OpaqueSubjectRef
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    work_item_id: Hash
    signal_type: SignalType
    subject: OpaqueSubjectRef
    created_at: AwareDatetime
    source_actor_ref: OpaqueSubjectRef


class OrchestrationProgressFingerprint(Model):
    version: str = "2"
    evidence_fingerprint: Hash
    effect_fingerprint: Hash
    requirements: tuple[VerificationRequirement, ...]
    fingerprint: Hash


class ResumeRecoveryResult(Model):
    case_id: OpaqueSubjectRef
    work_item_id: Hash
    before_case_revision: AwareDatetime
    after_case_revision: AwareDatetime
    read_recoveries: tuple[RecoveryResult, ...] = ()
    recovered_evidence_refs: tuple[EvidenceRef, ...] = ()
    effect_recoveries: tuple[RecoveryResult, ...] = ()
    semantic_progress: bool
    prepared_blocked: bool


class CaseWorkItem(Model):
    work_item_id: Hash
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    work_type: WorkType
    status: WorkStatus
    reason_code: WorkReason
    source_ref: OpaqueSubjectRef
    trigger: Trigger
    expected_case_status: CaseStatus
    expected_case_revision: AwareDatetime
    created_at: AwareDatetime
    not_before: AwareDatetime
    claimed_by: OpaqueSubjectRef | None = None
    lease_until: AwareDatetime | None = None
    lease_token: OpaqueSubjectRef | None = None
    attempt_count: int = 0
    completed_at: AwareDatetime | None = None
    required_signal: SignalType | None = None
    signal_id: OpaqueSubjectRef | None = None
    previous_run_id: OpaqueSubjectRef | None = None
    run_id: OpaqueSubjectRef | None = None
    resumed_at: AwareDatetime | None = None
    resume_lease_token: OpaqueSubjectRef | None = None
    started_at: AwareDatetime | None = None
    snapshot_ref: Hash | None = None
    wait_seconds: int | None = None
    requirement: VerificationRequirement | None = None
    before_progress_fingerprint: Hash | None = None
    after_progress_fingerprint: Hash | None = None
    before_evidence_fingerprint: Hash | None = None
    after_evidence_fingerprint: Hash | None = None
    verification_requirements: tuple[VerificationRequirement, ...] = ()
    progress_before: OrchestrationProgressFingerprint | None = None
    recovery_reads: tuple[RecoveryResult, ...] = ()
    recovery_result: ResumeRecoveryResult | None = None
    recovery_claim_token: OpaqueSubjectRef | None = None

    @property
    def binding_mode(self) -> WorkBindingMode:
        # Derived from the durable type, including legacy rows. Callers cannot
        # provide a mode that weakens another Work's freshness contract.
        return WorkBindingMode.SIDE_EFFECT if self.work_type == WorkType.RECOVERY_RECHECK else WorkBindingMode.CASE_SNAPSHOT


class WorkClaim(Model):
    work_item_id: Hash
    worker_id: OpaqueSubjectRef
    lease_token: OpaqueSubjectRef


class RunLineage(Model):
    parent_run_id: OpaqueSubjectRef | None = None
    resumed_from_work_item_id: Hash
    resume_reason: WorkReason
    resume_trigger: Trigger


class OrchestrationAuditRecord(Model):
    work_item_id: Hash
    case_id: OpaqueSubjectRef
    event: WorkEvent
    before_status: WorkStatus | None
    after_status: WorkStatus
    worker_id: OpaqueSubjectRef | None = None
    trigger_ref: OpaqueSubjectRef
    recorded_at: AwareDatetime
