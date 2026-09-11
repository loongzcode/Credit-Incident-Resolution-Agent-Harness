from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StrictStr

from credit_harness.domain.models import Model
from credit_harness.domain.enums import Completeness, Freshness, SourceKind, ToolName, ObservationStatus
from credit_harness.evidence.models import ClaimType, SubjectKind
from credit_harness.hypotheses.models import HypothesisId, HypothesisKind, HypothesisStatus, GapStatus, PriorityClass, UncollectedClaimType
from credit_harness.identity.models import FinancialSubject, IdentityDimension, IdentityMatch

CONTEXT_SCHEMA_VERSION = "1"
ELIGIBILITY_POLICY_VERSION = "1"
COMPACTION_POLICY_VERSION = "1"
CONTEXT_POLICY_VERSION = "1"
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[StrictInt, Field(ge=0)]
FactValue = StrictBool | StrictInt | StrictStr


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
    transaction_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    verification_version: str


class FactSubject(Model):
    kind: SubjectKind
    identifier: str
    field: str | None
    internal_order_id: str


class FactSource(Model):
    tool: ToolName
    source_kind: SourceKind
    source_version: str | None
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
    protocol_version: str | None
    evidence_refs: tuple[str, ...]


class HypothesisCapsule(Model):
    hypothesis_id: HypothesisId
    kind: HypothesisKind
    statement: str
    status: HypothesisStatus
    decisive_evidence_refs: tuple[str, ...]
    supporting_evidence_refs: tuple[str, ...]
    contradicting_evidence_refs: tuple[str, ...]
    supporting_ref_count: Count
    contradicting_ref_count: Count
    relation_refs_digest: Hash
    open_gap_ids: tuple[str, ...]
    reason: str


class ResolvedHypothesisSummary(Model):
    hypothesis_id: HypothesisId
    statement: str
    status: HypothesisStatus
    decisive_evidence_refs: tuple[str, ...]


class GapCapsule(Model):
    gap_id: str
    question: str
    required_claim_types: tuple[ClaimType | UncollectedClaimType, ...]
    priority: PriorityClass
    status: GapStatus
    related_hypotheses: tuple[HypothesisId, ...]
    evidence_refs: tuple[str, ...]


class ReferenceRange(Model):
    count: Count
    first_ref: str
    latest_ref: str
    range_digest: Hash


class LookupScope(Model):
    internal_order_id: str
    protocol_version: str | None
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


class HistoricalStateGroup(Model):
    claim_type: ClaimType
    subject: FactSubject
    protocol_version: str | None
    first_observed_value: FactValue
    last_observed_value: FactValue
    first_business_time: AwareDatetime
    last_business_time: AwareDatetime
    last_freshness: Freshness
    references: ReferenceRange


class HistoryDigest(Model):
    tool_calls_used: Count
    evidence_count: Count
    latest_observation_time: AwareDatetime | None
    repeated_lookup_groups: tuple[RepeatedLookupGroup, ...]
    state_transitions: tuple[HistoricalStateGroup, ...]


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


class ContextBudgetUsage(Model):
    serialized_chars: Count
    approximate_tokens: Count
    fact_capsules: Count
    hypothesis_capsules: Count
    gap_capsules: Count
    history_items: Count
    limits: ContextBudget
    estimator: str = "unicode_chars_div_3_ceiling_v1"


class ReasoningContextSnapshot(Model):
    snapshot_id: Hash
    case_id: str
    internal_order_id: str
    context_schema_version: str
    eligibility_policy_version: str
    compaction_policy_version: str
    context_policy_version: str
    hypothesis_rule_version: str
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
    selected_evidence_refs: tuple[str, ...]
    omitted_evidence_summary: OmittedEvidenceSummary
    context_budget_usage: ContextBudgetUsage
