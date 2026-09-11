from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator

from credit_harness.domain.models import Model
from credit_harness.domain.enums import Completeness, Freshness, SourceKind, ToolName, ObservationStatus
from credit_harness.evidence.models import ClaimType, SubjectKind
from credit_harness.hypotheses.models import HypothesisId, HypothesisKind, HypothesisStatus, GapStatus, PriorityClass, UncollectedClaimType
from credit_harness.identity.models import FinancialSubject, IdentityDimension, IdentityMatch
from .structured_values import OpaqueBusinessRef, OpaqueSubjectRef, ContextReference, StructuredVersion, StructuredFieldPath
from .value_contracts import validate_claim_value, validate_subject_field

CONTEXT_SCHEMA_VERSION = "4"
ELIGIBILITY_POLICY_VERSION = "4"
COMPACTION_POLICY_VERSION = "3"
CONTEXT_POLICY_VERSION = "4"
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]
FactValue = StrictBool | StrictInt | StrictStr
_BUSINESS_REFERENCE = TypeAdapter(OpaqueBusinessRef)


class InformationClass(StrEnum):
    BUSINESS = "BUSINESS"
    TOKENIZED_IDENTITY = "TOKENIZED_IDENTITY"
    MASKED_PII = "MASKED_PII"
    RAW_PII = "RAW_PII"
    INTERNAL_CONTROL = "INTERNAL_CONTROL"
    ORACLE = "ORACLE"


class TaskContext(Model):
    goal: str
    success_criteria: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    escalation_conditions: tuple[str, ...]
    forbidden_outcomes: tuple[str, ...]


class PaymentIdentityContext(Model):
    result: IdentityMatch
    mismatch_dimensions: tuple[IdentityDimension, ...]
    unknown_dimensions: tuple[IdentityDimension, ...]
    candidate_transaction_count: Count
    transaction_ref_preview: tuple[OpaqueBusinessRef, ...]
    transaction_refs_digest: Hash
    evidence_ref_count: Count
    evidence_refs_digest: Hash
    # Only critical proof refs, not all candidate auxiliary fields.
    evidence_refs: tuple[ContextReference, ...]
    verification_version: StructuredVersion

    @property
    def transaction_refs(self):
        """Legacy inspection alias; serialized context explicitly calls this a preview."""
        return self.transaction_ref_preview


class FactSubject(Model):
    kind: SubjectKind
    identifier: OpaqueSubjectRef
    field: StructuredFieldPath | None
    internal_order_id: OpaqueSubjectRef

    @model_validator(mode="after")
    def subject_contract(self):
        # All six subject kinds are covered by identifier's OpaqueSubjectRef.
        # Transaction/request additionally retain the financial reference grammar.
        if self.kind in (SubjectKind.TRANSACTION, SubjectKind.FUND_REQUEST):
            _BUSINESS_REFERENCE.validate_python(self.identifier)
        return self


class FactSource(Model):
    tool: ToolName
    source_kind: SourceKind
    source_version: StructuredVersion | None
    source_as_of: AwareDatetime | None


class FactCapsule(Model):
    claim_type: ClaimType
    value: FactValue
    subject: FactSubject
    business_time: AwareDatetime
    observed_at: AwareDatetime
    freshness: Freshness
    completeness: Completeness
    source: FactSource
    protocol_version: StructuredVersion | None
    evidence_refs: tuple[ContextReference, ...]

    @model_validator(mode="after")
    def claim_contract(self):
        validate_claim_value(self.claim_type, self.value)
        validate_subject_field(self.claim_type, self.subject.field)
        return self


class HypothesisCapsule(Model):
    hypothesis_id: HypothesisId
    kind: HypothesisKind
    statement: str
    status: HypothesisStatus
    decisive_evidence_refs: tuple[ContextReference, ...]
    supporting_ref_preview: tuple[ContextReference, ...] = ()
    contradicting_ref_preview: tuple[ContextReference, ...] = ()
    supporting_ref_count: Count
    contradicting_ref_count: Count
    supporting_refs_digest: Hash
    contradicting_refs_digest: Hash
    open_gap_ids: tuple[ContextReference, ...]
    reason: str


class ResolvedHypothesisSummary(Model):
    hypothesis_id: HypothesisId
    statement: str
    status: HypothesisStatus
    decisive_evidence_refs: tuple[ContextReference, ...]


class GapCapsule(Model):
    gap_id: ContextReference
    question: str
    required_claim_types: tuple[ClaimType | UncollectedClaimType, ...]
    priority: PriorityClass
    status: GapStatus
    related_hypotheses: tuple[HypothesisId, ...]
    evidence_refs: tuple[ContextReference, ...]


class ReferenceRange(Model):
    count: Count
    first_ref: ContextReference
    latest_ref: ContextReference
    range_digest: Hash


class LookupScope(Model):
    internal_order_id: OpaqueSubjectRef
    protocol_version: StructuredVersion | None
    effective_at: AwareDatetime | None


class RepeatedLookupGroup(Model):
    tool: ToolName
    scope: LookupScope
    status: ObservationStatus
    first_observed_at: AwareDatetime
    last_observed_at: AwareDatetime
    latest_freshness: Freshness
    latest_completeness: Completeness
    references: ReferenceRange
    # Trailing run for this tool AND exact query scope, considering successes too.
    latest_consecutive_count: Count = 0


class HistoricalStateGroup(Model):
    claim_type: ClaimType
    subject: FactSubject
    protocol_version: StructuredVersion | None
    first_observed_value: FactValue
    last_observed_value: FactValue
    first_business_time: AwareDatetime
    last_business_time: AwareDatetime
    last_freshness: Freshness
    references: ReferenceRange

    @model_validator(mode="after")
    def historical_claim_contract(self):
        validate_claim_value(self.claim_type, self.first_observed_value)
        validate_claim_value(self.claim_type, self.last_observed_value)
        validate_subject_field(self.claim_type, self.subject.field)
        return self


class HistoryDigest(Model):
    tool_calls_used: Count
    evidence_count: Count
    latest_observation_time: AwareDatetime | None
    repeated_lookup_groups: tuple[RepeatedLookupGroup, ...]
    state_transitions: tuple[HistoricalStateGroup, ...]
    lookup_history_complete: StrictBool = False


class SafetyInvariant(StrEnum):
    NO_NEW_FINANCIAL_INTENT = "NO_NEW_FINANCIAL_INTENT"
    UNKNOWN_IS_NOT_FAILED = "UNKNOWN_IS_NOT_FAILED"
    TOOL_SUCCESS_IS_NOT_BUSINESS_OUTCOME = "TOOL_SUCCESS_IS_NOT_BUSINESS_OUTCOME"
    REQUIRE_COMPLETE_PAYMENT_IDENTITY = "REQUIRE_COMPLETE_PAYMENT_IDENTITY"
    READ_ONLY_INVESTIGATION = "READ_ONLY_INVESTIGATION"
    NO_SELF_DECLARED_CASE_SUCCESS = "NO_SELF_DECLARED_CASE_SUCCESS"
    EVIDENCE_REFERENCES_REQUIRED = "EVIDENCE_REFERENCES_REQUIRED"


class SafetyContext(Model):
    invariants: tuple[SafetyInvariant, ...]
    forbidden_actions: tuple[str, ...]


class RuntimeBudgetContext(Model):
    max_tool_calls: Count
    used_tool_calls: Count
    remaining_tool_calls: Count
    investigation_allowed: StrictBool


class ToolRisk(StrEnum):
    READ_ONLY = "READ_ONLY"


class ToolDataClass(StrEnum):
    BUSINESS = "BUSINESS"
    BUSINESS_RULE = "BUSINESS_RULE"
    TOKENIZED_FINANCIAL_IDENTITY = "TOKENIZED_FINANCIAL_IDENTITY"


class EstimateClass(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"


class ToolCapability(Model):
    tool_name: ToolName
    description: str
    produces_claim_types: tuple[ClaimType, ...]
    contributes_requirements: tuple[UncollectedClaimType, ...] = ()
    risk_class: ToolRisk = ToolRisk.READ_ONLY
    data_classification: ToolDataClass
    estimated_cost_class: EstimateClass = EstimateClass.LOW
    estimated_latency_class: EstimateClass = EstimateClass.LOW


class OmissionReason(StrEnum):
    ELIGIBILITY_DENIED = "ELIGIBILITY_DENIED"
    REPEATED_LOOKUP = "REPEATED_LOOKUP"
    HISTORICAL_SUPERSEDED = "HISTORICAL_SUPERSEDED"
    IRRELEVANT_TO_ACTIVE_HYPOTHESES = "IRRELEVANT_TO_ACTIVE_HYPOTHESES"
    SIZE_BUDGET = "SIZE_BUDGET"
    RELATION_COMPACTED = "RELATION_COMPACTED"
    IDENTITY_COMPACTED = "IDENTITY_COMPACTED"


class OmissionGroup(Model):
    reason: OmissionReason
    count: Count
    evidence_ids_digest: Hash


class OmittedEvidenceSummary(Model):
    total_evidence: Count
    selected: Count
    omitted: tuple[OmissionGroup, ...]


class ContextBudget(Model):
    max_serialized_chars: Count = 60000
    max_fact_capsules: Count = 64
    max_hypothesis_capsules: Count = 16
    max_gap_capsules: Count = 24
    max_history_items: Count = 20
    max_relation_ref_preview: Annotated[StrictInt, Field(ge=0, le=32)] = 4
    max_identity_transaction_preview: Annotated[StrictInt, Field(ge=0, le=32)] = 3


class ContextBudgetUsage(Model):
    serialized_chars: Count
    approximate_tokens: Count
    fact_capsules: Count
    hypothesis_capsules: Count
    gap_capsules: Count
    history_items: Count
    limits: ContextBudget
    estimator: str = "unicode_chars_div_3_ceiling_v1"


class ContextTrustClass(StrEnum):
    TRUSTED_CONTROL = "TRUSTED_CONTROL"
    UNTRUSTED_EXTERNAL_DATA = "UNTRUSTED_EXTERNAL_DATA"
    DETERMINISTIC_DERIVED = "DETERMINISTIC_DERIVED"


class ContextSectionTrust(Model):
    # Literal fields prevent callers from promoting facts to instructions.
    task: Literal[ContextTrustClass.TRUSTED_CONTROL] = ContextTrustClass.TRUSTED_CONTROL
    financial_subject: Literal[ContextTrustClass.TRUSTED_CONTROL] = ContextTrustClass.TRUSTED_CONTROL
    safety_constraints: Literal[ContextTrustClass.TRUSTED_CONTROL] = ContextTrustClass.TRUSTED_CONTROL
    available_tools: Literal[ContextTrustClass.TRUSTED_CONTROL] = ContextTrustClass.TRUSTED_CONTROL
    budget: Literal[ContextTrustClass.TRUSTED_CONTROL] = ContextTrustClass.TRUSTED_CONTROL
    current_facts: Literal[ContextTrustClass.UNTRUSTED_EXTERNAL_DATA] = ContextTrustClass.UNTRUSTED_EXTERNAL_DATA
    history_digest: Literal[ContextTrustClass.UNTRUSTED_EXTERNAL_DATA] = ContextTrustClass.UNTRUSTED_EXTERNAL_DATA
    financial_identity: Literal[ContextTrustClass.DETERMINISTIC_DERIVED] = ContextTrustClass.DETERMINISTIC_DERIVED
    active_hypotheses: Literal[ContextTrustClass.DETERMINISTIC_DERIVED] = ContextTrustClass.DETERMINISTIC_DERIVED
    resolved_hypotheses_summary: Literal[ContextTrustClass.DETERMINISTIC_DERIVED] = ContextTrustClass.DETERMINISTIC_DERIVED
    open_evidence_gaps: Literal[ContextTrustClass.DETERMINISTIC_DERIVED] = ContextTrustClass.DETERMINISTIC_DERIVED


class CaseContextIdentifiers(Model):
    case_id: OpaqueSubjectRef
    internal_order_id: OpaqueSubjectRef


class ReasoningContextSnapshot(Model):
    snapshot_id: Hash
    section_trust: ContextSectionTrust = ContextSectionTrust()
    case_id: OpaqueSubjectRef
    internal_order_id: OpaqueSubjectRef
    context_schema_version: StructuredVersion
    eligibility_policy_version: StructuredVersion
    compaction_policy_version: StructuredVersion
    context_policy_version: StructuredVersion
    hypothesis_rule_version: StructuredVersion
    assembled_at: AwareDatetime
    case_fingerprint: Hash
    evidence_fingerprint: Hash
    hypothesis_input_fingerprint: Hash
    hypothesis_graph_fingerprint: Hash
    policy_fingerprint: Hash
    task: TaskContext
    financial_subject: FinancialSubject | None
    financial_identity: PaymentIdentityContext
    current_facts: tuple[FactCapsule, ...]
    active_hypotheses: tuple[HypothesisCapsule, ...]
    resolved_hypotheses_summary: tuple[ResolvedHypothesisSummary, ...]
    open_evidence_gaps: tuple[GapCapsule, ...]
    safety_constraints: SafetyContext
    budget: RuntimeBudgetContext
    available_tools: tuple[ToolCapability, ...]
    history_digest: HistoryDigest
    selected_evidence_refs: tuple[ContextReference, ...]
    omitted_evidence_summary: OmittedEvidenceSummary
    context_budget_usage: ContextBudgetUsage
