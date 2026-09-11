"""Agent-facing surface. Only CaseToolExecutor can dispatch observation tools.

Static bearer-to-tenant bindings are injected by trusted deployment configuration;
this does not mint capabilities, expose upstream credentials or provision cases.
"""
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx

from credit_harness.cases.evidence_view import CaseEvidenceService, CaseEvidenceView
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.cases.models import Case, CaseAccessError, CasePolicyError, CaseToolResult
from credit_harness.domain.enums import ToolName
from credit_harness.evidence.models import RawObservation
from credit_harness.evidence.repository import ProvenanceError
from credit_harness.persistence.store import token_hash
from credit_harness.tools.contracts import ToolQuery


@dataclass(frozen=True)
class HarnessBinding:
    executor: CaseToolExecutor
    evidence: CaseEvidenceService


def create_harness_app(bindings: dict[str, HarnessBinding]) -> FastAPI:
    """bindings keys are SHA-256 digests of separate Harness bearer secrets."""
    app = FastAPI(title="Credit Case Harness", version="0.2.0")
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
        binding = bindings.get(token_hash(credentials.credentials)) if credentials else None
        if binding is None:
            raise HTTPException(401, "harness credential required")
        return binding

    Binding = Annotated[HarnessBinding, Depends(authenticate)]

    def safe(operation):
        try:
            return operation()
        except CaseAccessError as error:
            raise HTTPException(404, "case or evidence unavailable") from error
        except CasePolicyError as error:
            raise HTTPException(409, str(error)) from error
        except (ProvenanceError, httpx.HTTPError) as error:
            raise HTTPException(502, "upstream observation unavailable or invalid; no business conclusion") from error

    @app.get("/cases/{case_id}", response_model=Case)
    def get_case(case_id: str, binding: Binding):
        return safe(lambda: binding.executor.cases.get(case_id))

    @app.post("/cases/{case_id}/tools/{tool}", response_model=CaseToolResult)
    def execute(case_id: str, tool: ToolName, query: ToolQuery, binding: Binding):
        return safe(lambda: binding.executor.execute(case_id, tool, query))

    @app.get("/cases/{case_id}/evidence", response_model=CaseEvidenceView)
    def get_evidence(case_id: str, binding: Binding):
        return safe(lambda: binding.evidence.get_case_evidence(case_id))

    @app.get("/cases/{case_id}/evidence/{evidence_id}/raw", response_model=RawObservation)
    def get_raw(case_id: str, evidence_id: str, binding: Binding):
        return safe(lambda: binding.evidence.repository.get_raw_observation(evidence_id, case_id=case_id))

    return app
