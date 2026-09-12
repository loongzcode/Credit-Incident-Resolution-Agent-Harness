from credit_harness.memory.retrieval import StructuredExperienceReranker, known
from credit_harness.memory.models import SimilarityFeature
from credit_harness.hypotheses.models import PriorityClass
from .models import DocumentType as D


def rank_key(document, candidate, snapshot, signature):
    source = document.source
    if document.kind == D.SKILL:
        claims = {c for strategy in source.evidence_strategy for c in strategy.recommended_claim_types}
        pattern = source.applies_when
        safety = len(source.safety_invariants)
    else:
        claims = set(source.observed_evidence_types)
        pattern = source.incident_signature
        safety = len(source.safety_lessons)
    safety_coverage = sum(len(claims.intersection(g.required_claim_types)) for g in snapshot.open_evidence_gaps
        if g.priority == PriorityClass.SAFETY_CRITICAL)
    coverage = sum(len(claims.intersection(g.required_claim_types)) for g in snapshot.open_evidence_gaps)
    available_claims = {c for tool in snapshot.available_tools for c in tool.produces_claim_types}
    actionable = len(claims & available_claims)
    overlap = sum(3 if f in (SimilarityFeature.REQUEST_TRANSPORT_STATUS, SimilarityFeature.SCHEMA_MISMATCH) else 1
        for f in SimilarityFeature if known(getattr(signature, f.value)) and getattr(signature, f.value) == getattr(pattern, f.value))
    # Lexicographic, discrete policy. Similarity is only the last relevance key.
    return (-safety_coverage, -coverage, -overlap, -candidate.specificity, -actionable, -safety,
        round(candidate.distance, 6), candidate.document_id, candidate.source_version)


def rerank(candidates, documents, snapshot, signature):
    pairs = sorted(zip(candidates, documents), key=lambda pair: rank_key(pair[1], pair[0], snapshot, signature))
    return tuple(document for _, document in pairs)
