from enum import StrEnum
from typing import Annotated
from pydantic import AwareDatetime, Field
from credit_harness.domain.models import Model
from credit_harness.context.models import Hash
from credit_harness.context.structured_values import OpaqueSubjectRef
from credit_harness.evidence.models import ClaimType

EvidenceRef = Annotated[str, Field(pattern=r"^E-[0-9a-f]{64}$")]


class EvaluationVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class DimensionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvaluationDimension(StrEnum):
    MONEY = "MONEY"
    IDENTITY = "IDENTITY"
    STATE_CONVERGENCE = "STATE_CONVERGENCE"
    SIDE_EFFECT = "SIDE_EFFECT"
    EVIDENCE_SUFFICIENCY = "EVIDENCE_SUFFICIENCY"
    POLICY = "POLICY"
    RECOVERY = "RECOVERY"
    OUTCOME = "OUTCOME"


class OutcomePath(StrEnum):
    SETTLED_PATH = "SETTLED_PATH"
    NO_DISBURSEMENT_PATH = "NO_DISBURSEMENT_PATH"


class EvaluationReason(StrEnum):
    PAYMENT_FINALITY_UNKNOWN = "PAYMENT_FINALITY_UNKNOWN"
    PAYMENT_IDENTITY_UNKNOWN = "PAYMENT_IDENTITY_UNKNOWN"
    PAYMENT_IDENTITY_MISMATCH = "PAYMENT_IDENTITY_MISMATCH"
    MONEY_STATE_CONTRADICTION = "MONEY_STATE_CONTRADICTION"
    REQUEST_ASSOCIATION_UNKNOWN = "REQUEST_ASSOCIATION_UNKNOWN"
    FUND_NOT_CONVERGED = "FUND_NOT_CONVERGED"
    GUARANTEE_NOT_CONVERGED = "GUARANTEE_NOT_CONVERGED"
    ASSET_NOT_CONVERGED = "ASSET_NOT_CONVERGED"
    ACCOUNTING_NOT_CONVERGED = "ACCOUNTING_NOT_CONVERGED"
    CALLBACK_NOT_CONSUMED = "CALLBACK_NOT_CONSUMED"
    DELIVERY_NOT_DELIVERED = "DELIVERY_NOT_DELIVERED"
    POST_EFFECT_EVIDENCE_MISSING = "POST_EFFECT_EVIDENCE_MISSING"
    OPEN_SAFETY_CRITICAL_GAP = "OPEN_SAFETY_CRITICAL_GAP"
    UNRESOLVED_SIDE_EFFECT = "UNRESOLVED_SIDE_EFFECT"
    RECOVERY_UNRESOLVED = "RECOVERY_UNRESOLVED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    PROVENANCE_INVALID = "PROVENANCE_INVALID"
    PENDING_READ_DISPATCH = "PENDING_READ_DISPATCH"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    FORBIDDEN_EFFECT_OBSERVED = "FORBIDDEN_EFFECT_OBSERVED"
    BUSINESS_OUTCOME_NOT_CONVERGED = "BUSINESS_OUTCOME_NOT_CONVERGED"
    CONVERGENCE_WINDOW_OPEN = "CONVERGENCE_WINDOW_OPEN"
    CLOSURE_INVARIANT_VIOLATION = "CLOSURE_INVARIANT_VIOLATION"


class VerificationRequirement(StrEnum):
    PAYMENT_FINALITY = "PAYMENT_FINALITY"
    PAYMENT_IDENTITY = "PAYMENT_IDENTITY"
    REQUEST_ASSOCIATION = "REQUEST_ASSOCIATION"
    FUND_FINAL_STATE = "FUND_FINAL_STATE"
    GUARANTEE_FINAL_STATE = "GUARANTEE_FINAL_STATE"
    ASSET_FINAL_STATE = "ASSET_FINAL_STATE"
    ACCOUNTING_ENTRY = "ACCOUNTING_ENTRY"
    CALLBACK_CONSUMPTION = "CALLBACK_CONSUMPTION"
    ASSET_DELIVERY = "ASSET_DELIVERY"
    POST_EFFECT_MESSAGE_STATUS = "POST_EFFECT_MESSAGE_STATUS"
    POST_EFFECT_DELIVERY_STATUS = "POST_EFFECT_DELIVERY_STATUS"
    SAFETY_GAP = "SAFETY_GAP"
    PROVENANCE = "PROVENANCE"
    READ_PUBLICATION = "READ_PUBLICATION"
    EFFECT_FINALITY = "EFFECT_FINALITY"
    RECOVERY_FINALITY = "RECOVERY_FINALITY"
    SAFE_POLICY = "SAFE_POLICY"


class UnresolvedVerificationRequirement(Model):
    requirement: VerificationRequirement
    dimension: EvaluationDimension
    reason_code: EvaluationReason
    required_claims: tuple[ClaimType, ...] = ()
    effect_ref: Hash | None = None


class EvaluationDimensionResult(Model):
    dimension: EvaluationDimension
    status: DimensionStatus
    reason_codes: tuple[EvaluationReason, ...] = ()
    supporting_evidence_refs: tuple[EvidenceRef, ...] = ()
    side_effect_refs: tuple[Hash, ...] = ()
    required_claims_missing: tuple[ClaimType, ...] = ()


class VerificationSnapshot(Model):
    verification_snapshot_id: Hash
    case_id: OpaqueSubjectRef
    tenant_id: OpaqueSubjectRef
    case_revision: AwareDatetime
    case_fingerprint: Hash
    evidence_fingerprint: Hash
    payment_identity_fingerprint: Hash
    current_state_fingerprint: Hash
    side_effect_ledger_fingerprint: Hash
    recovery_fingerprint: Hash
    call_history_fingerprint: Hash
    authorization_policy_fingerprint: Hash
    evaluation_policy_version: str
    verification_contract_version: str
    evaluation_policy_fingerprint: Hash
    verification_contract_fingerprint: Hash


class EvaluationReport(Model):
    report_id: Hash
    evaluation_run_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    verification_snapshot_id: Hash
    snapshot: VerificationSnapshot
    contract_version: str
    policy_version: str
    outcome_path: OutcomePath | None
    overall_verdict: EvaluationVerdict
    dimensions: tuple[EvaluationDimensionResult, ...]
    reason_codes: tuple[EvaluationReason, ...]
    supporting_evidence_refs: tuple[EvidenceRef, ...]
    side_effect_refs: tuple[Hash, ...]
    unresolved_requirements: tuple[UnresolvedVerificationRequirement, ...]
    created_at: AwareDatetime


class CaseClosureRecord(Model):
    closure_id: Hash
    case_id: OpaqueSubjectRef
    evaluation_run_id: OpaqueSubjectRef
    report_id: Hash
    verification_snapshot_id: Hash
    evidence_fingerprint: Hash
    ledger_fingerprint: Hash
    evaluation_policy_version: str
    contract_version: str
    closed_at: AwareDatetime


class ClosureStatus(StrEnum):
    CLOSED_VERIFIED = "CLOSED_VERIFIED"
    ALREADY_CLOSED_VERIFIED = "ALREADY_CLOSED_VERIFIED"


class VerifiedClosureResult(Model):
    status: ClosureStatus
    record: CaseClosureRecord


class ClosureCode(StrEnum):
    PASS_REQUIRED = "PASS_REQUIRED"
    STALE_EVALUATION = "STALE_EVALUATION"
    REPORT_NOT_PERSISTED = "REPORT_NOT_PERSISTED"
    REPORT_INVALID = "REPORT_INVALID"
    CLOSURE_INVARIANT_VIOLATION = "CLOSURE_INVARIANT_VIOLATION"


class ClosureError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code.value)
