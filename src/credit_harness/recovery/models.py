from enum import StrEnum
from typing import Annotated, Protocol
from pydantic import AwareDatetime, Field, model_validator
from credit_harness.domain.models import Model
from credit_harness.context.models import Hash
from credit_harness.context.structured_values import OpaqueSubjectRef
from credit_harness.authorization.commands import DomainCommand
from credit_harness.authorization.models import EffectStatus

ContractVersion = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")]


class LookupStatus(StrEnum):
    FOUND_APPLIED = "FOUND_APPLIED"
    FOUND_ACCEPTED = "FOUND_ACCEPTED"
    FOUND_FAILED_NO_EFFECT = "FOUND_FAILED_NO_EFFECT"
    NOT_FOUND = "NOT_FOUND"
    INDETERMINATE = "INDETERMINATE"


class RecoveryCapability(Model):
    resolver_id: OpaqueSubjectRef
    contract_version: ContractVersion
    supports_lookup: bool = True
    not_found_proves_no_effect: bool = False
    lookup_freshness_sla_seconds: Annotated[int, Field(ge=1, le=3600)] = 30
    supports_external_effect_ref: bool = True


class EffectIdentity(Model):
    effect_id: Hash
    target_hash: Hash
    payload_hash: Hash
    command: DomainCommand


class SideEffectLookupResult(Model):
    correlation_id: OpaqueSubjectRef
    command_identity_hash: Hash
    resolver_id: OpaqueSubjectRef
    lookup_status: LookupStatus
    external_effect_ref: OpaqueSubjectRef | None = None
    observed_at: AwareDatetime
    no_effect_confirmed: bool = False
    remote_status_reference: OpaqueSubjectRef | None = None

    @model_validator(mode="after")
    def no_effect_semantics(self):
        if self.lookup_status == LookupStatus.FOUND_FAILED_NO_EFFECT and not self.no_effect_confirmed:
            raise ValueError("no-effect proof required")
        if self.no_effect_confirmed and self.lookup_status not in (LookupStatus.FOUND_FAILED_NO_EFFECT, LookupStatus.NOT_FOUND):
            raise ValueError("contradictory no-effect assertion")
        return self


class RemediationEffectStatusResolver(Protocol):
    @property
    def capability(self) -> RecoveryCapability: ...
    def lookup(self, correlation_id: str, command_identity: EffectIdentity) -> SideEffectLookupResult: ...


class RecoveryFailure(StrEnum):
    LOOKUP_TIMEOUT = "LOOKUP_TIMEOUT"
    LOOKUP_TRANSPORT_ERROR = "LOOKUP_TRANSPORT_ERROR"
    LOOKUP_INDETERMINATE = "LOOKUP_INDETERMINATE"
    LOOKUP_NOT_FOUND = "LOOKUP_NOT_FOUND"
    LOOKUP_PROOF_CONFLICT = "LOOKUP_PROOF_CONFLICT"
    INVALID_LOOKUP_RECEIPT = "INVALID_LOOKUP_RECEIPT"
    RECOVERY_AUTHORIZATION_EXPIRED = "RECOVERY_AUTHORIZATION_EXPIRED"
    RECOVERY_APPROVAL_REVOKED = "RECOVERY_APPROVAL_REVOKED"
    RECOVERY_STALE = "RECOVERY_STALE"
    RECOVERY_LIMIT_REACHED = "RECOVERY_LIMIT_REACHED"
    LEASE_LOST = "LEASE_LOST"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"


class RecoveryOutcome(StrEnum):
    RECOVERED_APPLIED = "RECOVERED_APPLIED"
    RECOVERED_ACCEPTED = "RECOVERED_ACCEPTED"
    RECOVERED_FAILED_CONFIRMED = "RECOVERED_FAILED_CONFIRMED"
    STILL_UNKNOWN = "STILL_UNKNOWN"
    RESUMED_PREPARED = "RESUMED_PREPARED"
    BLOCKED_PREPARED = "BLOCKED_PREPARED"
    NOTHING_TO_DO = "NOTHING_TO_DO"
    ESCALATION_REQUIRED = "ESCALATION_REQUIRED"
    BUSY_OR_NOT_DUE = "BUSY_OR_NOT_DUE"


class DispatchRecoveryStatus(StrEnum):
    OBSERVATION_RECOVERED = "OBSERVATION_RECOVERED"
    OBSERVATION_ALREADY_PUBLISHED = "OBSERVATION_ALREADY_PUBLISHED"
    OBSERVATION_NOT_FOUND = "OBSERVATION_NOT_FOUND"
    PROVENANCE_INVALID = "PROVENANCE_INVALID"
    AMBIGUOUS_OBSERVATION = "AMBIGUOUS_OBSERVATION"


class RecoverySubject(StrEnum):
    READ_DISPATCH = "READ_DISPATCH"
    SIDE_EFFECT = "SIDE_EFFECT"


class RecoveryResult(Model):
    recovery_id: OpaqueSubjectRef
    subject_type: RecoverySubject
    subject_id: OpaqueSubjectRef
    before_status: EffectStatus | DispatchRecoveryStatus | None
    after_status: EffectStatus | DispatchRecoveryStatus
    action_taken: RecoveryOutcome | DispatchRecoveryStatus
    proof_ref: OpaqueSubjectRef | None = None
    recovered_evidence_refs: tuple[Annotated[str, Field(pattern=r"^E-[0-9a-f]{64}$")], ...] = ()
    requires_escalation: bool = False
    failure_code: RecoveryFailure | None = None


class RecoveryProof(Model):
    effect_id: Hash
    correlation_id: OpaqueSubjectRef
    resolver_id: OpaqueSubjectRef
    lookup_status: LookupStatus
    external_effect_ref: OpaqueSubjectRef | None
    observed_at: AwareDatetime
    contract_version: ContractVersion
    command_identity_hash: Hash
    result_hash: Hash


class EffectRecoveryAttempt(Model):
    recovery_id: OpaqueSubjectRef
    worker_id: OpaqueSubjectRef
    effect_id: Hash
    sequence: int
    started_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    source_status: EffectStatus
    lookup_result: SideEffectLookupResult | None = None
    result_status: EffectStatus | None = None
    resolver_id: OpaqueSubjectRef
    proof_hash: Hash | None = None
    failure_code: RecoveryFailure | None = None


class RecoveryPolicy(Model):
    grace_seconds: Annotated[int, Field(ge=0, le=3600)] = 30
    lease_seconds: Annotated[int, Field(ge=1, le=3600)] = 60
    max_attempts: Annotated[int, Field(ge=1, le=20)] = 3
    backoff_seconds: tuple[Annotated[int, Field(ge=1, le=86400)], ...] = (30, 120, 600)

    @model_validator(mode="after")
    def nonempty_backoff(self):
        if not self.backoff_seconds:
            raise ValueError("backoff required")
        return self


class RecoveryClaim(Model):
    recovery_id: OpaqueSubjectRef
    effect_id: Hash
    worker_id: OpaqueSubjectRef
    source_status: EffectStatus
    sequence: int


class RecoveryAuditEvent(StrEnum):
    RECOVERY_SCAN = "RECOVERY_SCAN"
    RECOVERY_LOOKUP = "RECOVERY_LOOKUP"
    RECOVERY_TRANSITION = "RECOVERY_TRANSITION"
    RECOVERY_ESCALATED = "RECOVERY_ESCALATED"


class RecoveryAuditRecord(Model):
    recovery_id: OpaqueSubjectRef
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    intent_id: Hash
    approval_id: OpaqueSubjectRef | None
    capability_id: OpaqueSubjectRef
    effect_id: Hash
    correlation_id: OpaqueSubjectRef
    event: RecoveryAuditEvent
    recorded_at: AwareDatetime
    proof: RecoveryProof | None = None
    failure_code: RecoveryFailure | None = None


class RecoveryError(RuntimeError):
    def __init__(self, code: RecoveryFailure):
        self.code = code
        super().__init__(code.value)
