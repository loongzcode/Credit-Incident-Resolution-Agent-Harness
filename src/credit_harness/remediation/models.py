from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from credit_harness.domain.models import Model
from credit_harness.cases.models import CaseStatus
from credit_harness.context.models import (
    Hash, TaskContext, SafetyContext, PaymentIdentityContext, FactCapsule, HypothesisCapsule, GapCapsule,
)
from credit_harness.context.structured_values import OpaqueSubjectRef, ContextReference
from credit_harness.planner.models import PlannerModelMetadata

REMEDIATION_POLICY_VERSION = "1"
REMEDIATION_CATALOG_VERSION = "1"
REMEDIATION_SCHEMA_VERSION = "1"


class RemediationActionType(StrEnum):
    NO_REMEDIATION = "NO_REMEDIATION"
    REPLAY_CALLBACK_CONSUMPTION = "REPLAY_CALLBACK_CONSUMPTION"
    REDELIVER_ASSET_NOTIFICATION = "REDELIVER_ASSET_NOTIFICATION"
    CREATE_RECONCILIATION_TASK = "CREATE_RECONCILIATION_TASK"
    REQUEST_OPERATOR_REVIEW = "REQUEST_OPERATOR_REVIEW"


class ActionRiskLevel(StrEnum):
    L0_READ_ONLY = "L0_READ_ONLY"
    L1_ADMINISTRATIVE = "L1_ADMINISTRATIVE"
    L2_SINGLE_ORDER_SIDE_EFFECT = "L2_SINGLE_ORDER_SIDE_EFFECT"
    L3_MONEY_MOVEMENT = "L3_MONEY_MOVEMENT"
    L4_BULK_OR_SYSTEMIC = "L4_BULK_OR_SYSTEMIC"


class EffectScope(StrEnum):
    NONE = "NONE"
    MESSAGE = "MESSAGE"
    DELIVERY = "DELIVERY"
    RECONCILIATION = "RECONCILIATION"
    ORDER_STATE = "ORDER_STATE"
    MONEY = "MONEY"
    BULK = "BULK"


class EvidenceCondition(StrEnum):
    SIGNED_FAILED_CALLBACK = "SIGNED_FAILED_CALLBACK"
    PAYMENT_IDENTITY_MATCH = "PAYMENT_IDENTITY_MATCH"
    UNCONVERGED_ASSET_DELIVERY = "UNCONVERGED_ASSET_DELIVERY"
    OBSERVED_DIVERGENCE = "OBSERVED_DIVERGENCE"
    OPEN_PROBLEM = "OPEN_PROBLEM"


class GapRequirement(StrEnum):
    NO_OPEN_SAFETY_GAP = "NO_OPEN_SAFETY_GAP"
    CURRENT_COMPATIBLE_DEPLOYMENT_IF_SCHEMA_FAILURE = "CURRENT_COMPATIBLE_DEPLOYMENT_IF_SCHEMA_FAILURE"


class RejectReason(StrEnum):
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"
    MONEY_MOVEMENT_PROHIBITED = "MONEY_MOVEMENT_PROHIBITED"
    FOREIGN_ORDER = "FOREIGN_ORDER"
    FOREIGN_CASE = "FOREIGN_CASE"
    UNKNOWN_PROBLEM = "UNKNOWN_PROBLEM"
    EVIDENCE_NOT_FOUND = "EVIDENCE_NOT_FOUND"
    EVIDENCE_NOT_CURRENT = "EVIDENCE_NOT_CURRENT"
    EVIDENCE_DOES_NOT_SUPPORT_ACTION = "EVIDENCE_DOES_NOT_SUPPORT_ACTION"
    IDENTITY_UNKNOWN = "IDENTITY_UNKNOWN"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    HYPOTHESIS_NOT_CONFIRMED = "HYPOTHESIS_NOT_CONFIRMED"
    UNRESOLVED_SAFETY_GAP = "UNRESOLVED_SAFETY_GAP"
    DEPLOYMENT_STATE_UNKNOWN = "DEPLOYMENT_STATE_UNKNOWN"
    DEPLOYMENT_INCOMPATIBLE = "DEPLOYMENT_INCOMPATIBLE"
    ACTION_ALREADY_SATISFIED = "ACTION_ALREADY_SATISFIED"
    ACTION_NOT_NEEDED = "ACTION_NOT_NEEDED"
    CASE_NOT_ACTIONABLE = "CASE_NOT_ACTIONABLE"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    STALE_POLICY = "STALE_POLICY"
    TARGET_BINDING_MISMATCH = "TARGET_BINDING_MISMATCH"


class RemediationActionContract(Model):
    action_type: RemediationActionType
    risk_level: ActionRiskLevel
    description: str
    required_evidence_conditions: tuple[EvidenceCondition, ...]
    required_identity_state: Literal["MATCH"] | None
    required_case_state: tuple[CaseStatus, ...]
    forbidden_if: tuple[RejectReason, ...]
    required_open_or_resolved_gaps: tuple[GapRequirement, ...]
    max_effect_scope: EffectScope
    requires_future_approval: bool
    requires_future_capability: bool


class RemediationCandidate(Model):
    candidate_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    action_type: RemediationActionType
    target_order_id: OpaqueSubjectRef
    target_problem_ids: Annotated[tuple[ContextReference, ...], Field(max_length=24)]
    evidence_refs: Annotated[tuple[ContextReference, ...], Field(max_length=128)]
    reason_summary: Annotated[str, Field(max_length=600)]
    # No external target reference input. Runtime binds precise addresses from
    # cited durable evidence; aliases are never accepted as write addresses.


class RemediationDraft(Model):
    snapshot_id: Hash
    candidates: Annotated[tuple[RemediationCandidate, ...], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({c.candidate_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("candidate IDs must be unique")
        return self


class RemediationTarget(Model):
    case_id: OpaqueSubjectRef
    tenant_id: str
    internal_order_id: OpaqueSubjectRef
    callback_event_ref: OpaqueSubjectRef | None = None
    message_ref: OpaqueSubjectRef | None = None
    delivery_ref: OpaqueSubjectRef | None = None


class ValidatedRemediationCandidate(Model):
    candidate: RemediationCandidate
    target: RemediationTarget
    risk_level: ActionRiskLevel
    supporting_evidence_refs: tuple[ContextReference, ...]
    snapshot_id: Hash
    evidence_hash: Hash
    policy_version: str = REMEDIATION_POLICY_VERSION
    catalog_version: str = REMEDIATION_CATALOG_VERSION


class RejectedRemediationCandidate(Model):
    candidate: RemediationCandidate
    reason_codes: tuple[RejectReason, ...]


class PreflightStatus(StrEnum):
    READY_FOR_FUTURE_AUTHORIZATION = "READY_FOR_FUTURE_AUTHORIZATION"
    BLOCKED = "BLOCKED"
    STALE = "STALE"
    NOT_NEEDED = "NOT_NEEDED"


class RemediationIntent(Model):
    intent_id: Hash
    case_id: OpaqueSubjectRef
    tenant_id: str
    internal_order_id: OpaqueSubjectRef
    action_type: RemediationActionType
    risk_level: ActionRiskLevel
    target: RemediationTarget
    supporting_evidence_refs: tuple[ContextReference, ...]
    snapshot_id: Hash
    evidence_hash: Hash
    catalog_version: str
    policy_version: str
    preflight_fingerprint: Hash
    requires_approval: bool
    requires_capability: bool
    status: Literal["PROPOSED"] = "PROPOSED"


class RemediationPreflightResult(Model):
    candidate_id: str
    status: PreflightStatus
    reason_codes: tuple[RejectReason, ...]
    proposal_snapshot_id: Hash
    fresh_snapshot_id: Hash
    preflight_fingerprint: Hash
    intent: RemediationIntent | None = None


class RemediationPreview(Model):
    candidate: RemediationCandidate
    risk_level: ActionRiskLevel
    status: PreflightStatus
    blocking_reasons: tuple[RejectReason, ...]
    target: RemediationTarget | None = None
    execution: Literal["NOT_AUTHORIZED_NOT_EXECUTED"] = "NOT_AUTHORIZED_NOT_EXECUTED"


class EligibilityStatus(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INVESTIGATION_INCOMPLETE = "INVESTIGATION_INCOMPLETE"
    SAFETY_GAP_OPEN = "SAFETY_GAP_OPEN"
    IDENTITY_UNSAFE = "IDENTITY_UNSAFE"
    NO_REMEDIATION_NEEDED = "NO_REMEDIATION_NEEDED"


class RemediationEligibility(Model):
    status: EligibilityStatus
    can_plan: bool
    allowed_actions: tuple[RemediationActionType, ...]


class RemediationTrustedControl(Model):
    internal_order_id: OpaqueSubjectRef
    task: TaskContext
    safety_constraints: SafetyContext
    action_catalog: tuple[RemediationActionContract, ...]


class RemediationDerivedState(Model):
    financial_identity: PaymentIdentityContext
    hypotheses: tuple[HypothesisCapsule, ...]
    open_gaps: tuple[GapCapsule, ...]


class RemediationExternalData(Model):
    current_facts: tuple[FactCapsule, ...]


class RemediationInputBundle(Model):
    schema_version: str = REMEDIATION_SCHEMA_VERSION
    snapshot_id: Hash
    system_contract: str
    trusted_control: RemediationTrustedControl
    deterministic_derived: RemediationDerivedState
    untrusted_external_data: RemediationExternalData
    output_schema: dict[str, JsonValue]

    @property
    def model_visible_payload(self):
        return self.model_dump(mode="json", exclude={"system_contract"})


class RemediationDecision(Model):
    decision_id: Hash
    snapshot_id: Hash
    draft_hash: Hash | None
    eligibility: RemediationEligibility
    valid_candidates: tuple[ValidatedRemediationCandidate, ...]
    rejected_candidates: tuple[RejectedRemediationCandidate, ...]
    selected_candidate: ValidatedRemediationCandidate | None
    preflight_results: tuple[RemediationPreflightResult, ...]
    preflight_result: RemediationPreflightResult | None
    previews: tuple[RemediationPreview, ...]
    final_intent: RemediationIntent | None
    model_metadata: PlannerModelMetadata | None
    policy_version: str = REMEDIATION_POLICY_VERSION
    catalog_version: str = REMEDIATION_CATALOG_VERSION


class RemediationUnavailable(RuntimeError):
    pass


class RemediationProtocolError(RemediationUnavailable):
    pass
