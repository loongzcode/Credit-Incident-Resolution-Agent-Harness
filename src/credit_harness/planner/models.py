from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, JsonValue, StrictInt, model_validator

from credit_harness.domain.models import Model
from credit_harness.domain.enums import ToolName
from credit_harness.evidence.models import ClaimType
from credit_harness.hypotheses.models import UncollectedClaimType
from credit_harness.context.models import (
    Hash, TaskContext, SafetyContext, RuntimeBudgetContext, ToolCapability,
    PaymentIdentityContext, FactCapsule, HistoryDigest, HypothesisCapsule,
    ResolvedHypothesisSummary, GapCapsule,
)
from credit_harness.context.structured_values import ContextReference, OpaqueSubjectRef
from credit_harness.identity.models import FinancialSubject
from credit_harness.memory.models import OrganizationalGuidanceSection, HistoricalGuidanceSection, SkillRef, GuidanceBuildStatus, GuidanceDegradation
from .metadata import PlannerModelMetadata
from credit_harness.registry.models import CaseCapabilitySnapshot

PLANNER_SCHEMA_VERSION = "1"
PLANNER_POLICY_VERSION = "3"
MODEL_INPUT_SCHEMA_VERSION = "4"
ACTION_RANKING_VERSION = "1"
ShortText = Annotated[str, Field(max_length=600)]
CandidateId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
GapIds = Annotated[tuple[ContextReference, ...], Field(min_length=1, max_length=24)]


class ActionType(StrEnum):
    CALL_TOOL = "CALL_TOOL"
    WAIT = "WAIT"
    ESCALATE = "ESCALATE"


class ReasonCode(StrEnum):
    NO_AVAILABLE_TOOL = "NO_AVAILABLE_TOOL"
    REPEATED_SOURCE_FAILURE = "REPEATED_SOURCE_FAILURE"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNSUPPORTED_INVESTIGATION = "UNSUPPORTED_INVESTIGATION"
    POLICY_BLOCKED = "POLICY_BLOCKED"


class ProposalQuery(Model):
    """Untrusted proposed parameters, deliberately NOT an executable ToolQuery."""
    internal_order_id: OpaqueSubjectRef
    protocol_version: Annotated[str, Field(pattern=r"^\d+\.\d+$")] | None = None
    effective_at: AwareDatetime | None = None


class CallToolCandidate(Model):
    candidate_id: CandidateId
    action_type: Literal[ActionType.CALL_TOOL] = ActionType.CALL_TOOL
    target_gap_ids: GapIds
    tool_name: ToolName
    query: ProposalQuery
    expected_claim_types: Annotated[tuple[ClaimType, ...], Field(min_length=1, max_length=32)]
    reason_summary: ShortText


class WaitCandidate(Model):
    candidate_id: CandidateId
    action_type: Literal[ActionType.WAIT] = ActionType.WAIT
    target_gap_ids: GapIds
    reason_code: ReasonCode
    suggested_wait_seconds: Annotated[StrictInt, Field(ge=30, le=3600)]
    reason_summary: ShortText


class EscalateCandidate(Model):
    candidate_id: CandidateId
    action_type: Literal[ActionType.ESCALATE] = ActionType.ESCALATE
    target_gap_ids: GapIds
    reason_code: ReasonCode
    requested_capability: UncollectedClaimType | None = None
    reason_summary: ShortText


# Nested anyOf works with provider Structured Outputs; no root union/function calls.
ActionCandidate = CallToolCandidate | WaitCandidate | EscalateCandidate


class PlannerDraft(Model):
    snapshot_id: Hash
    candidates: Annotated[tuple[ActionCandidate, ...], Field(min_length=1, max_length=3)]
    observation_summary: ShortText
    uncertainty_summary: ShortText

    @model_validator(mode="after")
    def unique_candidates(self):
        ids = [c.candidate_id for c in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")
        return self


class TrustedControl(Model):
    internal_order_id: OpaqueSubjectRef
    task: TaskContext
    financial_subject: FinancialSubject | None
    safety_constraints: SafetyContext
    budget: RuntimeBudgetContext
    available_tools: tuple[ToolCapability, ...]
    capability_snapshot: CaseCapabilitySnapshot | None = None


class DeterministicDerived(Model):
    financial_identity: PaymentIdentityContext
    active_hypotheses: tuple[HypothesisCapsule, ...]
    resolved_hypotheses_summary: tuple[ResolvedHypothesisSummary, ...]
    open_evidence_gaps: tuple[GapCapsule, ...]


class UntrustedExternalData(Model):
    current_facts: tuple[FactCapsule, ...]
    history_digest: HistoryDigest


class ModelInputBundle(Model):
    schema_version: str = MODEL_INPUT_SCHEMA_VERSION
    snapshot_id: Hash
    system_contract: str
    trusted_control: TrustedControl
    deterministic_derived: DeterministicDerived
    untrusted_external_data: UntrustedExternalData
    planner_output_schema: dict[str, JsonValue]
    organizational_guidance: OrganizationalGuidanceSection | None = None
    historical_guidance: HistoricalGuidanceSection | None = None
    guidance_fingerprint: Hash | None = None

    @property
    def model_visible_payload(self):
        # Fresh JSON-compatible copy; no runtime objects or private reverse mapping.
        return self.model_dump(mode="json", exclude={"system_contract"})


class RejectionCode(StrEnum):
    TOOL_NOT_AVAILABLE = "TOOL_NOT_AVAILABLE"
    FOREIGN_ORDER = "FOREIGN_ORDER"
    UNKNOWN_TARGET_GAP = "UNKNOWN_TARGET_GAP"
    INVALID_PROTOCOL_QUERY = "INVALID_PROTOCOL_QUERY"
    EXPECTED_CLAIM_NOT_PRODUCED = "EXPECTED_CLAIM_NOT_PRODUCED"
    TOOL_DOES_NOT_ADDRESS_TARGET_GAP = "TOOL_DOES_NOT_ADDRESS_TARGET_GAP"
    INVESTIGATION_NOT_ALLOWED = "INVESTIGATION_NOT_ALLOWED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    NON_READ_ONLY_TOOL = "NON_READ_ONLY_TOOL"
    REPEATED_NO_NEW_INFORMATION = "REPEATED_NO_NEW_INFORMATION"
    INCOMPLETE_LOOKUP_HISTORY = "INCOMPLETE_LOOKUP_HISTORY"
    ACTIONABLE_SAFETY_GAP_NOT_ADDRESSED = "ACTIONABLE_SAFETY_GAP_NOT_ADDRESSED"
    ACTIONABLE_SAFETY_GAP_NOT_TARGETED = "ACTIONABLE_SAFETY_GAP_NOT_TARGETED"
    ESCALATION_CAPABILITY_MISMATCH = "ESCALATION_CAPABILITY_MISMATCH"
    ESCALATION_REASON_INCONSISTENT = "ESCALATION_REASON_INCONSISTENT"


class RejectedCandidate(Model):
    candidate: ActionCandidate
    reason_codes: tuple[RejectionCode, ...]


class ValidatedActionProposal(Model):
    """Bound to a snapshot and policy; not an authorization or execution token."""
    snapshot_id: Hash
    policy_version: str = PLANNER_POLICY_VERSION
    candidate: ActionCandidate


class RankingDetail(Model):
    candidate_id: CandidateId
    # Lexicographic discrete weights, not a probability or information-gain estimate.
    priority: int
    novel_requirement_coverage: int
    coverage: int
    hypothesis_discrimination: int
    economy: int
    repeated_query_penalty: int


class PlannerDecision(Model):
    decision_id: Hash
    snapshot_id: Hash
    planner_draft_hash: Hash
    valid_candidates: tuple[ValidatedActionProposal, ...]
    rejected_candidates: tuple[RejectedCandidate, ...]
    selected_action: ValidatedActionProposal | None
    selection_reason: str
    ranking_details: tuple[RankingDetail, ...]
    planner_model_metadata: PlannerModelMetadata
    planner_schema_version: str = PLANNER_SCHEMA_VERSION
    policy_version: str = PLANNER_POLICY_VERSION
    ranking_version: str = ACTION_RANKING_VERSION
    model_input_schema_version: str = MODEL_INPUT_SCHEMA_VERSION
    created_at: AwareDatetime
    guidance_fingerprint: Hash | None = None
    skill_refs: tuple[SkillRef, ...] = ()
    experience_refs: tuple[Hash, ...] = ()
    guidance_build_status: GuidanceBuildStatus = GuidanceBuildStatus.EMPTY
    guidance_degradation: GuidanceDegradation = GuidanceDegradation.NONE


class PlannerUnavailable(RuntimeError):
    """Safe provider failure; callers must not dispatch a fallback business tool."""


class PlannerProtocolError(PlannerUnavailable):
    """Invalid structured draft. Error text never echoes raw provider content."""
