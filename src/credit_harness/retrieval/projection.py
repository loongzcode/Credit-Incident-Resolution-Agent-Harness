"""Closed semantic vocabulary. No raw text, identifiers or full source JSON."""
import re
from typing import Annotated
from pydantic import Field, StrictBool
from credit_harness.domain.models import Model
from credit_harness.domain.enums import (ToolName, TransportStatus, FundBusinessStatus, PaymentFinality, ConsumeStatus, LoanStatus)
from credit_harness.identity.models import IdentityMatch
from credit_harness.evidence.models import ClaimType
from credit_harness.hypotheses.models import UncollectedClaimType
from credit_harness.memory.models import (IncidentSignature, SkillDefinition, SkillStatus,
    VerifiedIncidentExperience, StrategyGoal, SafetyLesson)
from credit_harness.evaluation.models import OutcomePath
from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.context.budget import digest
from credit_harness.memory.retrieval import current_signature
from .models import PROJECTION_VERSION, RetrievalError


class RetrievalIncidentState(Model):
    # A separate allowlist: adding future fields to IncidentSignature cannot
    # silently expand embedding eligibility. In particular StructuredVersion in
    # Context admits opaque release names; embeddings admit numeric versions only.
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
    protocol_version: Annotated[str, Field(pattern=r"^[0-9]{1,3}(\.[0-9]{1,3}){0,3}$")] | None = None


class SemanticProjection(Model):
    projection_version: str = PROJECTION_VERSION
    states: tuple[RetrievalIncidentState, ...] = Field(max_length=64)
    goals: tuple[StrategyGoal, ...] = ()
    claims: tuple[ClaimType | UncollectedClaimType, ...] = ()
    tools: tuple[ToolName, ...] = ()
    lessons: tuple[SafetyLesson, ...] = ()
    outcome: OutcomePath | None = None


def sanitized_state(state):
    # Revalidate model_copy/model_construct inputs. All remaining values are
    # enums, booleans or bounded numeric protocol versions, never external text.
    state = IncidentSignature.model_validate(state.model_dump())
    try:
        return RetrievalIncidentState(**{name: getattr(state, name) for name in RetrievalIncidentState.model_fields})
    except ValueError:
        raise RetrievalError("state is not eligible embedding data") from None


def skill_document(skill: SkillDefinition) -> SemanticProjection:
    if type(skill) is not SkillDefinition:
        raise RetrievalError("typed skill required")
    skill = SkillDefinition.model_validate(skill.model_dump())
    if skill.status != SkillStatus.ACTIVE:
        raise RetrievalError("inactive source")
    return SemanticProjection(states=tuple(sanitized_state(s) for s in (skill.applies_when, *skill.known_patterns)),
        goals=skill.investigation_goals,
        claims=tuple(sorted({c for s in skill.evidence_strategy for c in s.recommended_claim_types})),
        lessons=skill.anti_patterns)


def experience_document(experience: VerifiedIncidentExperience) -> SemanticProjection:
    if type(experience) is not VerifiedIncidentExperience:
        raise RetrievalError("typed verified experience required")
    e = VerifiedIncidentExperience.model_validate(experience.model_dump())
    return SemanticProjection(states=(sanitized_state(e.incident_signature),),
        claims=tuple(sorted(set(e.observed_evidence_types))),
        tools=tuple(dict.fromkeys(s.tool for s in e.investigation_sequence)),
        lessons=tuple(sorted({s.lesson for s in e.safety_lessons})), outcome=e.outcome_path)


def incident_query(snapshot: ReasoningContextSnapshot) -> SemanticProjection:
    if type(snapshot) is not ReasoningContextSnapshot:
        raise RetrievalError("only a reasoning snapshot is admitted")
    signature = current_signature(snapshot)  # envelope and content identity validation
    return SemanticProjection(states=(sanitized_state(signature),),
        claims=tuple(sorted({c for g in snapshot.open_evidence_gaps for c in g.required_claim_types})))


def embedding_text(projection: SemanticProjection) -> str:
    if type(projection) is not SemanticProjection:
        raise RetrievalError("closed projection required")
    p = SemanticProjection.model_validate(projection.model_dump())
    if p.projection_version != PROJECTION_VERSION:
        raise RetrievalError("unsupported projection version")
    parts = []
    for state in p.states:
        for name, value in state.model_dump(mode="json").items():
            if value is not None:
                parts.append(f"{name.replace('_', ' ')}: {str(value).lower().replace('_', ' ')}")
    for name in ("goals", "claims", "tools", "lessons"):
        parts.extend(f"{name}: {v.value.lower().replace('_', ' ')}" for v in getattr(p, name))
    if p.outcome:
        parts.append("verified outcome: " + p.outcome.value.lower().replace("_", " "))
    text = "\n".join(parts)
    # Defense in depth against long numeric PII even inside protocol version
    # grammar. Names/credentials/raw fields cannot be represented by this DTO.
    if len(text) > 16000 or re.search(r"(?:\d[ .-]?){7,}", text):
        raise RetrievalError("projection rejected by privacy validator")
    return text


def projection_hash(projection):
    return digest({"projection_version": PROJECTION_VERSION, "text": embedding_text(projection)})
