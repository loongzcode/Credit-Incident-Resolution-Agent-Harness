from enum import StrEnum

from pydantic import AwareDatetime

from credit_harness.domain.models import Model
from credit_harness.evidence.models import ClaimType


class HypothesisId(StrEnum):
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    H4 = "H4"
    H5 = "H5"
    H6 = "H6"
    H6_SCHEMA_MISMATCH = "H6_SCHEMA_MISMATCH"
    H6_STALE_CONSUMER_SCHEMA = "H6_STALE_CONSUMER_SCHEMA"
    H7 = "H7"
    H8 = "H8"


class HypothesisKind(StrEnum):
    CAUSAL = "CAUSAL"
    STATE = "STATE"
    SEMANTIC_GUARD = "SEMANTIC_GUARD"


class HypothesisStatus(StrEnum):
    UNKNOWN = "UNKNOWN"          # Insufficient evidence; never equivalent to false.
    POSSIBLE = "POSSIBLE"        # Investigation context only; no direct support.
    SUPPORTED = "SUPPORTED"      # Direct support; confirmation contract incomplete.
    CONFIRMED = "CONFIRMED"      # Deterministic confirmation rule satisfied.
    ELIMINATED = "ELIMINATED"    # Decisive evidence negates the exact proposition.


class RelationKind(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    DECISIVE_SUPPORT = "DECISIVE_SUPPORT"
    DECISIVE_CONTRADICTION = "DECISIVE_CONTRADICTION"
    CONTEXT_ONLY = "CONTEXT_ONLY"


class GapStatus(StrEnum):
    OPEN = "OPEN"
    SATISFIED = "SATISFIED"
    UNAVAILABLE = "UNAVAILABLE"


class PriorityClass(StrEnum):
    SAFETY_CRITICAL = "SAFETY_CRITICAL"
    DISCRIMINATING = "DISCRIMINATING"
    SUPPORTING = "SUPPORTING"


class UncollectedClaimType(StrEnum):
    """Required facts that current extractors cannot yet supply; not Evidence."""
    DEPLOYED_CONSUMER_SCHEMA_VERSION = "DEPLOYED_CONSUMER_SCHEMA_VERSION"
    FUND_REQUEST_PROTOCOL_APPLICABILITY = "FUND_REQUEST_PROTOCOL_APPLICABILITY"
    REQUEST_ASSOCIATION = "REQUEST_ASSOCIATION"
    CALLBACK_EVENT_ASSOCIATION = "CALLBACK_EVENT_ASSOCIATION"
    FUND_REJECTION_WITHOUT_DISBURSEMENT = "FUND_REJECTION_WITHOUT_DISBURSEMENT"


class HypothesisDefinition(Model):
    hypothesis_id: HypothesisId
    kind: HypothesisKind
    statement: str
    description: str
    confirmation_rule_id: str
    elimination_rule_id: str
    relevant_claim_types: tuple[ClaimType, ...]
    parent_hypothesis_id: HypothesisId | None = None


class EvidenceRelation(Model):
    case_id: str
    hypothesis_id: HypothesisId
    evidence_id: str
    relation: RelationKind
    reason: str
    rule_id: str


class EvidenceGap(Model):
    gap_id: str
    case_id: str
    hypothesis_ids: tuple[HypothesisId, ...]
    question: str
    required_claim_types: tuple[ClaimType | UncollectedClaimType, ...]
    status: GapStatus
    reason: str
    priority_class: PriorityClass
    evidence_refs: tuple[str, ...] = ()


class HypothesisState(Model):
    hypothesis_id: HypothesisId
    status: HypothesisStatus
    supporting_evidence_refs: tuple[str, ...]
    contradicting_evidence_refs: tuple[str, ...]
    decisive_evidence_refs: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    reason: str
    rule_version: str
    evaluated_at: AwareDatetime


class HypothesisGraphView(Model):
    case_id: str
    rule_version: str
    evaluated_at: AwareDatetime
    input_fingerprint: str
    definitions: tuple[HypothesisDefinition, ...]
    hypotheses: tuple[HypothesisState, ...]
    relations: tuple[EvidenceRelation, ...]
    gaps: tuple[EvidenceGap, ...]
    open_gaps: tuple[EvidenceGap, ...]
    confirmed: tuple[HypothesisId, ...]
    supported: tuple[HypothesisId, ...]
    eliminated: tuple[HypothesisId, ...]
