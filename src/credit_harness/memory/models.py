from enum import StrEnum
from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, StrictBool, model_validator, model_serializer, AliasChoices
from credit_harness.domain.models import Model
from credit_harness.domain.enums import (ToolName, TransportStatus, FundBusinessStatus,
    PaymentFinality, ConsumeStatus, LoanStatus, FieldType)
from credit_harness.context.models import Hash
from credit_harness.context.structured_values import OpaqueSubjectRef, StructuredVersion, StructuredErrorCode
from credit_harness.evidence.models import ClaimType
from credit_harness.evaluation.models import OutcomePath, EvidenceRef
from credit_harness.hypotheses.models import HypothesisId, PriorityClass
from credit_harness.identity.models import IdentityMatch
from credit_harness.remediation.models import RemediationActionType
from credit_harness.authorization.models import EffectStatus

EXPERIENCE_SCHEMA_VERSION = "2"
GUIDANCE_SCHEMA_VERSION = "1"
SkillId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{1,79}$")]


class MemoryError(RuntimeError):
    """Sanitized boundary error; never contains source payloads."""


class SkillStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class ExperienceStatus(StrEnum):
    ACTIVE = "ACTIVE"
    REVOKED = "REVOKED"


class GuidanceBuildStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    RETRIEVAL_FAILED = "RETRIEVAL_FAILED"
    INVALID_SKILL = "INVALID_SKILL"
    BUDGET_DROPPED = "BUDGET_DROPPED"  # Legacy audit reads only; builders use degradation.


class GuidanceDegradation(StrEnum):
    NONE = "NONE"
    BUDGET_DROPPED = "BUDGET_DROPPED"


class GuidanceTrustClass(StrEnum):
    TRUSTED_ORGANIZATIONAL_GUIDANCE = "TRUSTED_ORGANIZATIONAL_GUIDANCE"
    VERIFIED_HISTORICAL_GUIDANCE = "VERIFIED_HISTORICAL_GUIDANCE"


class SafetyLesson(StrEnum):
    HTTP_TIMEOUT_DOES_NOT_PROVE_PAYMENT_FAILURE = "HTTP_TIMEOUT_DOES_NOT_PROVE_PAYMENT_FAILURE"
    PAYMENT_IDENTITY_REQUIRED_BEFORE_L2_REMEDIATION = "PAYMENT_IDENTITY_REQUIRED_BEFORE_L2_REMEDIATION"
    UNKNOWN_SIDE_EFFECT_MUST_NOT_BE_BLINDLY_RETRIED = "UNKNOWN_SIDE_EFFECT_MUST_NOT_BE_BLINDLY_RETRIED"
    APPLIED_DOES_NOT_PROVE_BUSINESS_CONVERGENCE = "APPLIED_DOES_NOT_PROVE_BUSINESS_CONVERGENCE"


class GuidanceInvariant(StrEnum):
    NO_NEW_DISBURSEMENT_WHILE_PAYMENT_UNKNOWN = "NO_NEW_DISBURSEMENT_WHILE_PAYMENT_UNKNOWN"
    HISTORICAL_EXPERIENCE_CANNOT_SATISFY_CURRENT_GAP = "HISTORICAL_EXPERIENCE_CANNOT_SATISFY_CURRENT_GAP"
    MEMORY_CANNOT_AUTHORIZE_SIDE_EFFECT = "MEMORY_CANNOT_AUTHORIZE_SIDE_EFFECT"
    CURRENT_EVIDENCE_OVERRIDES_HISTORICAL_PATTERN = "CURRENT_EVIDENCE_OVERRIDES_HISTORICAL_PATTERN"


class InvariantRule(Model):
    invariant: GuidanceInvariant
    # False is representable for validation, but never admitted by composition.
    enforced: StrictBool = True


class StrategyGoal(StrEnum):
    ESTABLISH_PAYMENT_FINALITY = "ESTABLISH_PAYMENT_FINALITY"
    ESTABLISH_PAYMENT_IDENTITY = "ESTABLISH_PAYMENT_IDENTITY"
    INVESTIGATE_CALLBACK_CONSUMPTION = "INVESTIGATE_CALLBACK_CONSUMPTION"
    ESTABLISH_STATE_CONVERGENCE = "ESTABLISH_STATE_CONVERGENCE"


class RationaleCode(StrEnum):
    MONEY_TRUTH_FIRST = "MONEY_TRUTH_FIRST"
    IDENTITY_BEFORE_EFFECT = "IDENTITY_BEFORE_EFFECT"
    CURRENT_GAP_DRIVEN = "CURRENT_GAP_DRIVEN"
    VERIFY_DOWNSTREAM = "VERIFY_DOWNSTREAM"


class AllowedGuidance(StrEnum):
    SUGGEST_CURRENT_EVIDENCE_PRIORITY = "SUGGEST_CURRENT_EVIDENCE_PRIORITY"


class SkillScope(Model):
    funding_partner: OpaqueSubjectRef | None = None
    asset_partner: OpaqueSubjectRef | None = None
    product_code: OpaqueSubjectRef | None = None
    protocol_versions: tuple[StructuredVersion, ...] = ()


class IncidentSignature(Model):
    request_transport_status: TransportStatus | None = None
    fund_business_status: FundBusinessStatus | None = None
    payment_finality_at_detection: PaymentFinality | None = None
    payment_identity_state: IdentityMatch = IdentityMatch.UNKNOWN
    callback_gateway_state: StrictBool | None = None
    message_consume_state: ConsumeStatus | None = None
    schema_mismatch_present: StrictBool | None = None
    asset_state: LoanStatus | None = None
    guarantee_state: LoanStatus | None = None
    accounting_state: StrictBool | None = None
    protocol_version: StructuredVersion | None = None
    funding_partner: OpaqueSubjectRef | None = None
    asset_partner: OpaqueSubjectRef | None = None
    product_code: OpaqueSubjectRef | None = None


class SkillEvidenceStrategy(Model):
    goal: StrategyGoal
    recommended_claim_types: Annotated[tuple[ClaimType, ...], Field(min_length=1, max_length=12)]
    priority: PriorityClass
    rationale_code: RationaleCode


class SkillDefinition(Model):
    skill_id: SkillId
    version: StructuredVersion
    status: SkillStatus = SkillStatus.DRAFT
    scope: SkillScope = SkillScope()
    applies_when: IncidentSignature = IncidentSignature()
    investigation_goals: tuple[StrategyGoal, ...]
    evidence_strategy: Annotated[tuple[SkillEvidenceStrategy, ...], Field(max_length=24)]
    safety_invariants: Annotated[tuple[InvariantRule, ...], Field(min_length=1, max_length=16)]
    known_patterns: tuple[IncidentSignature, ...] = ()
    anti_patterns: tuple[SafetyLesson, ...] = ()
    allowed_guidance: tuple[AllowedGuidance, ...] = (AllowedGuidance.SUGGEST_CURRENT_EVIDENCE_PRIORITY,)
    created_at: AwareDatetime


class SkillRef(Model):
    skill_id: SkillId
    version: StructuredVersion


class SkillGuidance(Model):
    skill: SkillRef
    evidence_strategy: tuple[SkillEvidenceStrategy, ...]
    safety_invariants: tuple[InvariantRule, ...]
    anti_patterns: tuple[SafetyLesson, ...]


class HistoricalSymptom(Model):
    # Closed projection, no source IDs or arbitrary claim/value dictionary.
    pattern: IncidentSignature
    evidence_refs: tuple[EvidenceRef, ...]
    error_codes: tuple[StructuredErrorCode, ...] = ()
    expected_field_types: tuple[FieldType, ...] = ()
    actual_field_types: tuple[FieldType, ...] = ()


class HistoricalHypothesis(Model):
    hypothesis_id: HypothesisId
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(min_length=1)]
    rule_version: StructuredVersion
    at_call_sequence: int = Field(ge=1)


class HistoricalScope(Model):
    order_token: Hash
    protocol_version: StructuredVersion | None = None
    effective_at: AwareDatetime | None = None


class InvestigationStep(Model):
    sequence: int = Field(ge=1)
    tool: ToolName
    scope: HistoricalScope
    new_evidence_count: int = Field(ge=0)
    produced_claim_types: tuple[ClaimType, ...]


class HistoricalEffect(Model):
    action: RemediationActionType
    outcome: EffectStatus


class RecoveryPattern(Model):
    source_status: EffectStatus
    result_status: EffectStatus | None
    completed: StrictBool


class BoundSafetyLesson(Model):
    lesson: SafetyLesson
    evidence_refs: tuple[EvidenceRef, ...] = ()
    effect_actions: tuple[RemediationActionType, ...] = ()


class ExperienceProvenance(Model):
    source_extractor_versions: tuple[StructuredVersion, ...]
    hypothesis_rule_version: StructuredVersion
    source_evidence_fingerprint: Hash
    source_call_history_fingerprint: Hash
    evaluation_policy_version: StructuredVersion
    verification_contract_version: StructuredVersion


class VerifiedIncidentExperience(Model):
    experience_id: Hash
    source_case_id: OpaqueSubjectRef
    closure_id: Hash
    report_id: Hash
    verification_snapshot_id: Hash
    tenant_id: OpaqueSubjectRef
    internal_order_ref: Hash
    outcome_path: OutcomePath
    incident_signature: IncidentSignature
    partner_context: SkillScope
    protocol_context: tuple[StructuredVersion, ...]
    observed_symptoms: HistoricalSymptom
    confirmed_hypotheses: tuple[HistoricalHypothesis, ...]
    investigation_sequence: tuple[InvestigationStep, ...]
    observed_evidence_types: tuple[ClaimType, ...] = Field(validation_alias=AliasChoices("observed_evidence_types", "useful_evidence_types"))
    remediation_actions: tuple[RemediationActionType, ...]
    side_effect_outcomes: tuple[HistoricalEffect, ...]
    recovery_patterns: tuple[RecoveryPattern, ...]
    safety_lessons: tuple[BoundSafetyLesson, ...]
    provenance: ExperienceProvenance
    created_at: AwareDatetime
    applicable_since: AwareDatetime
    applicable_until: AwareDatetime | None = None
    experience_schema_version: Literal["1", "2"] = EXPERIENCE_SCHEMA_VERSION

    @property
    def useful_evidence_types(self):
        """Legacy Python alias; not a claim of usefulness."""
        return self.observed_evidence_types

    @model_serializer(mode="wrap")
    def preserve_v1_content_identity(self, handler):
        result = handler(self)
        if self.experience_schema_version == "1" and "observed_evidence_types" in result:
            result["useful_evidence_types"] = result.pop("observed_evidence_types")
        return result


class SimilarityFeature(StrEnum):
    REQUEST_TRANSPORT_STATUS = "request_transport_status"
    FUND_BUSINESS_STATUS = "fund_business_status"
    PAYMENT_FINALITY = "payment_finality_at_detection"
    PAYMENT_IDENTITY = "payment_identity_state"
    CALLBACK_GATEWAY = "callback_gateway_state"
    MESSAGE_CONSUME = "message_consume_state"
    SCHEMA_MISMATCH = "schema_mismatch_present"
    ASSET = "asset_state"
    GUARANTEE = "guarantee_state"
    ACCOUNTING = "accounting_state"
    PROTOCOL = "protocol_version"


class ExperienceCapsule(Model):
    experience_id: Hash
    verified_outcome_path: OutcomePath
    similarity_features: tuple[SimilarityFeature, ...]
    retrieval_score: int = Field(ge=0)
    observed_pattern: IncidentSignature
    observed_evidence_claim_types: Annotated[tuple[ClaimType, ...], Field(max_length=48)] = Field(
        validation_alias=AliasChoices("observed_evidence_claim_types", "useful_evidence_claim_types"))
    observed_tool_sequence: Annotated[tuple[ToolName, ...], Field(max_length=24)]
    verified_safety_lessons: tuple[SafetyLesson, ...]
    historical_error_codes: Annotated[tuple[StructuredErrorCode, ...], Field(max_length=4)] = ()


class InvestigationGuidanceBundle(Model):
    schema_version: Literal["1"] = GUIDANCE_SCHEMA_VERSION
    # Runtime binding, excluded from the model's guidance projection.
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    snapshot_id: Hash
    active_skills: Annotated[tuple[SkillGuidance, ...], Field(max_length=4)]
    verified_experiences: Annotated[tuple[ExperienceCapsule, ...], Field(max_length=3)]
    guidance_fingerprint: Hash
    skill_versions: tuple[SkillRef, ...]
    experience_ids: tuple[Hash, ...]


class OrganizationalGuidanceSection(Model):
    trust_class: Literal[GuidanceTrustClass.TRUSTED_ORGANIZATIONAL_GUIDANCE] = GuidanceTrustClass.TRUSTED_ORGANIZATIONAL_GUIDANCE
    active_skills: tuple[SkillGuidance, ...]


class HistoricalGuidanceSection(Model):
    trust_class: Literal[GuidanceTrustClass.VERIFIED_HISTORICAL_GUIDANCE] = GuidanceTrustClass.VERIFIED_HISTORICAL_GUIDANCE
    notice: Literal["HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE"] = "HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE"
    verified_experiences: tuple[ExperienceCapsule, ...]


class EvidencePriorityChange(Model):
    claim_type: ClaimType
    proposed_priority: PriorityClass


class SkillImprovementProposal(Model):
    skill_id: SkillId
    base_version: StructuredVersion
    proposed_evidence_priority_changes: tuple[EvidencePriorityChange, ...]
    supporting_experience_ids: tuple[Hash, ...]
    sample_size: int = Field(ge=1)
    generated_at: AwareDatetime
    status: Literal["PROPOSED"] = "PROPOSED"


class GuidanceBuildResult(Model):
    bundle: InvestigationGuidanceBundle | None
    status: GuidanceBuildStatus
    degradation: GuidanceDegradation = GuidanceDegradation.NONE
