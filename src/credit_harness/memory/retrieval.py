from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.context.envelope import ContextEnvelopeInvariantValidator
from credit_harness.context.budget import snapshot_digest
from credit_harness.identity.models import IdentityMatch
from .models import IncidentSignature, SimilarityFeature, ExperienceCapsule, MemoryError
from .experience import signature_from_facts


def current_signature(snapshot):
    ContextEnvelopeInvariantValidator().validate(snapshot)
    if snapshot_digest(snapshot) != snapshot.snapshot_id:
        raise MemoryError("invalid current snapshot")
    return signature_from_facts(snapshot.current_facts, snapshot.financial_identity.result)


def known(value):
    return value is not None and value != "UNKNOWN"


def applicable(scope, signature):
    for name in ("funding_partner", "asset_partner", "product_code"):
        expected = getattr(scope, name)
        if expected is not None and expected != getattr(signature, name):
            return False
    return not scope.protocol_versions or signature.protocol_version in scope.protocol_versions


def symptom_matches(pattern, signature):
    return all(not known(value) or getattr(signature, name) == value
               for name, value in pattern.model_dump().items())


class VerifiedExperienceRetriever:
    def __init__(self, repository):
        self.repository = repository

    def retrieve(self, snapshot: ReasoningContextSnapshot, signature: IncidentSignature, *, top_k=3):
        if type(snapshot) is not ReasoningContextSnapshot or type(signature) is not IncidentSignature:
            raise MemoryError("typed retrieval input required")
        if not 0 <= top_k <= 3:
            raise MemoryError("retrieval budget exceeded")
        self.repository.cases.get(snapshot.case_id)  # tenant-scoped admission
        if current_signature(snapshot) != signature:
            raise MemoryError("signature must reflect current context")
        result = []
        for experience in self.repository.active():
            if (experience.source_case_id == snapshot.case_id or experience.applicable_since > snapshot.assembled_at
                    or experience.applicable_until is not None and snapshot.assembled_at >= experience.applicable_until
                    or not applicable(experience.partner_context, signature)):
                continue
            # Known protocol mismatch excludes old guidance; unknown current
            # protocol is not guessed from the historical incident.
            if signature.protocol_version and experience.protocol_context and signature.protocol_version not in experience.protocol_context:
                continue
            features = tuple(f for f in SimilarityFeature if known(getattr(signature, f.value))
                and getattr(signature, f.value) == getattr(experience.incident_signature, f.value))
            if not features:
                continue
            # Discrete weighted overlap only. Neither denominator nor probability.
            score = sum(3 if f in (SimilarityFeature.REQUEST_TRANSPORT_STATUS, SimilarityFeature.SCHEMA_MISMATCH)
                        else 1 for f in features)
            # Cross-case projection intentionally has no source Case, Evidence,
            # identity tokens, ToolQuery, external reference, actor or signature.
            pattern = experience.incident_signature.model_copy(update={
                "funding_partner": None, "asset_partner": None, "product_code": None})
            result.append(ExperienceCapsule(experience_id=experience.experience_id, verified_outcome_path=experience.outcome_path,
                similarity_features=features, retrieval_score=score, observed_pattern=pattern,
                observed_evidence_claim_types=experience.observed_evidence_types,
                observed_tool_sequence=tuple(s.tool for s in experience.investigation_sequence[:24]),
                verified_safety_lessons=tuple(s.lesson for s in experience.safety_lessons),
                historical_error_codes=experience.observed_symptoms.error_codes[:4]))
        return tuple(sorted(result, key=lambda c: (-c.retrieval_score, c.experience_id))[:top_k])
