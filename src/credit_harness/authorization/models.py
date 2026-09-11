from enum import StrEnum
from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, model_validator
from credit_harness.domain.models import Model
from credit_harness.context.models import Hash
from credit_harness.context.structured_values import OpaqueSubjectRef
from credit_harness.remediation.models import RemediationIntent, RemediationActionType

AUTHORIZATION_POLICY_VERSION = "1"


class AuthorizationCode(StrEnum):
    AUTHORIZED = "AUTHORIZED"
    UNSIGNED_CAPABILITY = "UNSIGNED_CAPABILITY"
    INVALID_CAPABILITY = "INVALID_CAPABILITY"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    CAPABILITY_PAYLOAD_MISMATCH = "CAPABILITY_PAYLOAD_MISMATCH"
    CAPABILITY_TARGET_MISMATCH = "CAPABILITY_TARGET_MISMATCH"
    CAPABILITY_SCOPE_MISMATCH = "CAPABILITY_SCOPE_MISMATCH"
    CAPABILITY_ALREADY_USED = "CAPABILITY_ALREADY_USED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    APPROVAL_BINDING_MISMATCH = "APPROVAL_BINDING_MISMATCH"
    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_TRANSITION = "INVALID_TRANSITION"


class AuthorizationError(RuntimeError):
    def __init__(self, code: AuthorizationCode):
        self.code = code
        super().__init__(code.value)


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class ApprovalReason(StrEnum):
    REQUESTED = "REQUESTED"
    OPERATOR_APPROVED = "OPERATOR_APPROVED"
    OPERATOR_REJECTED = "OPERATOR_REJECTED"
    OPERATOR_REVOKED = "OPERATOR_REVOKED"


class ApprovalRequest(Model):
    approval_id: OpaqueSubjectRef
    intent: RemediationIntent  # complete, immutable approval binding
    status: ApprovalStatus
    requested_at: AwareDatetime
    decided_at: AwareDatetime | None = None
    expires_at: AwareDatetime
    actor_ref: OpaqueSubjectRef | None = None
    reason_code: ApprovalReason = ApprovalReason.REQUESTED


class ApprovalDecision(Model):
    approval_id: OpaqueSubjectRef
    decision: Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.REVOKED]
    actor_ref: OpaqueSubjectRef


class ExecutionCapability(Model):
    capability_id: OpaqueSubjectRef
    intent_id: Hash
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    internal_order_id: OpaqueSubjectRef
    action_type: RemediationActionType
    target_hash: Hash
    payload_hash: Hash
    snapshot_id: Hash
    evidence_hash: Hash
    preflight_fingerprint: Hash
    approval_id: OpaqueSubjectRef | None
    policy_version: str
    catalog_version: str
    authorization_policy_version: str
    expected_case_revision: AwareDatetime
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    max_effects: Literal[1] = 1


class SignedExecutionCapability(Model):
    payload: ExecutionCapability = Field(repr=False)
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", repr=False)]


class EffectStatus(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHED = "DISPATCHED"
    ACCEPTED = "ACCEPTED"
    APPLIED = "APPLIED"
    VERIFIED = "VERIFIED"  # reserved; no Step 8 transition reaches it
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"
    NOOP = "NOOP"


class ReceiptOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    APPLIED = "APPLIED"
    FAILED_CONFIRMED = "FAILED_CONFIRMED"


class FailureCode(StrEnum):
    TRANSPORT_UNCERTAIN = "TRANSPORT_UNCERTAIN"
    INVALID_RECEIPT = "INVALID_RECEIPT"
    REMOTE_REJECTED = "REMOTE_REJECTED"


class SideEffectReceipt(Model):
    correlation_id: OpaqueSubjectRef
    outcome: ReceiptOutcome
    external_effect_ref: OpaqueSubjectRef | None
    observed_at: AwareDatetime
    no_effect_confirmed: bool = False

    @model_validator(mode="after")
    def confirmed_failure_requires_no_effect(self):
        if self.outcome == ReceiptOutcome.FAILED_CONFIRMED and not self.no_effect_confirmed:
            raise ValueError("confirmed failure requires explicit no-effect confirmation")
        if self.no_effect_confirmed and self.outcome != ReceiptOutcome.FAILED_CONFIRMED:
            raise ValueError("no-effect confirmation conflicts with receipt outcome")
        return self


class SideEffectLedger(Model):
    effect_id: Hash
    idempotency_key: Hash
    intent_id: Hash
    capability_id: OpaqueSubjectRef
    approval_id: OpaqueSubjectRef | None
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    internal_order_id: OpaqueSubjectRef
    action_type: RemediationActionType
    target_hash: Hash
    payload_hash: Hash
    snapshot_id: Hash
    evidence_hash: Hash
    prepared_case_revision: AwareDatetime
    status: EffectStatus
    attempt_count: Annotated[int, Field(ge=0, le=1)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    dispatch_correlation_id: OpaqueSubjectRef
    external_effect_ref: OpaqueSubjectRef | None = None
    failure_code: FailureCode | None = None


class SideEffectExecutionResult(Model):
    ledger: SideEffectLedger
    executed_now: bool
    idempotent_replay: bool
    authorization_status: AuthorizationCode = AuthorizationCode.AUTHORIZED


class AuditEvent(StrEnum):
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    CAPABILITY_ISSUED = "CAPABILITY_ISSUED"
    EFFECT_PREPARED = "EFFECT_PREPARED"
    EFFECT_TRANSITION = "EFFECT_TRANSITION"


class AuthorizationAuditRecord(Model):
    audit_id: OpaqueSubjectRef
    event: AuditEvent
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    intent_id: Hash
    approval_id: OpaqueSubjectRef | None = None
    capability_id: OpaqueSubjectRef | None = None
    effect_id: Hash | None = None
    correlation_id: OpaqueSubjectRef | None = None
    effect_status: EffectStatus | None = None
    actor_ref: OpaqueSubjectRef | None = None
    recorded_at: AwareDatetime
