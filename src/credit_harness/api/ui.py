"""Read-only browser boundary. Never serialize CaseEvidenceView or Observation.

Bindings are independent, read-only bearer grants scoped to a tenant repository.
The existing Harness credentials and write/dispatch routes are not mounted here.
"""
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, ValidationError

from credit_harness.cases.models import CaseAccessError, CaseBudget, CaseStatus
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.budget import MandatoryContextOverflow
from credit_harness.context.compaction import fact
from credit_harness.context.eligibility import ContextEligibilityError
from credit_harness.context.envelope import ContextEnvelopeInvariantValidator
from credit_harness.context.models import FactCapsule, ReasoningContextSnapshot
from credit_harness.context.structured_values import ContextReference, OpaqueSubjectRef, StructuredVersion
from credit_harness.domain.models import Model
from credit_harness.evidence.models import EvidenceStrength
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.hypotheses.models import HypothesisDefinition, HypothesisState, EvidenceGap
from credit_harness.identity.models import FinancialSubject
from credit_harness.persistence.store import token_hash


class UICase(Model):
    case_id: OpaqueSubjectRef
    internal_order_id: OpaqueSubjectRef
    status: CaseStatus
    updated_at: AwareDatetime
    budget: CaseBudget
    financial_subject: FinancialSubject | None


class UIEvidence(FactCapsule):
    evidence_id: ContextReference
    observation_id: ContextReference
    event_time: AwareDatetime | None
    strength: EvidenceStrength
    extractor_version: StructuredVersion


class UIEvidenceList(Model):
    items: tuple[UIEvidence, ...]
    total: int
    eligibility_denied_count: int


class UIHypothesisState(HypothesisState):
    supporting_evidence_refs: tuple[ContextReference, ...]
    contradicting_evidence_refs: tuple[ContextReference, ...]
    decisive_evidence_refs: tuple[ContextReference, ...]
    missing_evidence: tuple[ContextReference, ...]


class UIGap(EvidenceGap):
    case_id: OpaqueSubjectRef
    gap_id: ContextReference
    evidence_refs: tuple[ContextReference, ...]


class UIHypothesisGraph(Model):
    case_id: OpaqueSubjectRef
    rule_version: StructuredVersion
    definitions: tuple[HypothesisDefinition, ...]
    hypotheses: tuple[UIHypothesisState, ...]
    open_gaps: tuple[UIGap, ...]


def create_ui_app(bindings: dict[str, EvidenceRepository], *,
                  assembler: ReasoningContextAssembler | None = None) -> FastAPI:
    app = FastAPI(title="Incident Investigation Console API", version="0.1.0")
    assembler = assembler or ReasoningContextAssembler()
    policy = assembler.eligibility
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        repository = bindings.get(token_hash(credentials.credentials)) if credentials else None
        if repository is None:
            raise HTTPException(403, detail={"code": "ACCESS_DENIED"})
        return repository

    Repository = Annotated[EvidenceRepository, Depends(authenticate)]

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        return JSONResponse(status_code=422, content={"detail": {"code": "INVALID_REQUEST"}})

    def safe(operation):
        try:
            return operation()
        except CaseAccessError:
            raise HTTPException(404, detail={"code": "CASE_NOT_FOUND"}) from None
        except MandatoryContextOverflow:
            raise HTTPException(409, detail={"code": "MANDATORY_CONTEXT_OVERFLOW"}) from None
        except (ContextEligibilityError, ValidationError):
            raise HTTPException(409, detail={"code": "CONTEXT_ELIGIBILITY_ERROR"}) from None

    def case_view(repository, case_id):
        case = repository.cases.get(case_id)
        policy.validate_case(case)
        return UICase(**{key: getattr(case, key) for key in UICase.model_fields})

    @app.get("/ui/cases/{case_id}", response_model=UICase)
    def get_case(case_id: str, repository: Repository):
        return safe(lambda: case_view(repository, case_id))

    def evidence_view(repository, case_id):
        case = repository.cases.get(case_id)
        policy.validate_case(case)
        # Reuse the existing domain scope/provenance validator for the timeline,
        # too; a valid field grammar does not establish membership in this Case.
        try:
            evidence = EvidenceIndex(case, repository.list(case_id)).evidence
        except ValueError:
            raise ContextEligibilityError("evidence does not satisfy case boundary") from None
        items = []
        for e in evidence:
            if not policy.allows(e):
                continue
            # Explicit allowlist: metadata, raw_ref and raw body never cross.
            items.append(UIEvidence(**fact(e).model_dump(), evidence_id=e.evidence_id,
                observation_id=e.observation_id, event_time=e.event_time,
                strength=e.strength, extractor_version=e.metadata.extractor_version))
        return UIEvidenceList(items=tuple(items), total=len(evidence),
                              eligibility_denied_count=len(evidence)-len(items))

    @app.get("/ui/cases/{case_id}/evidence", response_model=UIEvidenceList)
    def get_evidence(case_id: str, repository: Repository):
        return safe(lambda: evidence_view(repository, case_id))

    def graph_view(repository, case_id):
        case = repository.cases.get(case_id)
        policy.validate_case(case)
        evidence = repository.list(case_id)
        graph = HypothesisEngine().evaluate(case, evidence)
        eligible = {e.evidence_id for e in evidence if policy.allows(e)}
        refs = {r for h in graph.hypotheses for r in (*h.supporting_evidence_refs,
                *h.contradicting_evidence_refs, *h.decisive_evidence_refs)}
        refs.update(r for g in graph.open_gaps for r in g.evidence_refs)
        if not refs <= eligible:
            raise ContextEligibilityError("graph depends on ineligible evidence")
        return UIHypothesisGraph(case_id=graph.case_id, rule_version=graph.rule_version,
            definitions=graph.definitions,
            hypotheses=tuple(UIHypothesisState(**h.model_dump()) for h in graph.hypotheses),
            open_gaps=tuple(UIGap(**g.model_dump()) for g in graph.open_gaps))

    @app.get("/ui/cases/{case_id}/hypotheses", response_model=UIHypothesisGraph)
    def get_hypotheses(case_id: str, repository: Repository):
        return safe(lambda: graph_view(repository, case_id))

    def context_view(repository, case_id):
        snapshot = assembler.build(repository.cases.get(case_id), repository.list(case_id))
        ContextEnvelopeInvariantValidator().validate(snapshot)
        return snapshot

    @app.get("/ui/cases/{case_id}/reasoning-context", response_model=ReasoningContextSnapshot)
    def get_context(case_id: str, repository: Repository):
        return safe(lambda: context_view(repository, case_id))

    return app
