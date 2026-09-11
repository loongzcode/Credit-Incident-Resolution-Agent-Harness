from collections.abc import Callable
from typing import Protocol

from credit_harness.domain.enums import ToolName
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.tools.contracts import DispatchCorrelationId, Observation, ToolQuery
from .models import CaseToolResult
from .repository import CaseRepository


class ReadOnlyToolClient(Protocol):
    def observe(self, tool: ToolName, query: ToolQuery, *,
                dispatch_correlation_id: DispatchCorrelationId) -> Observation: ...


class CaseToolExecutor:
    def __init__(self, cases: CaseRepository, evidence: EvidenceRepository,
                 client_for_case: Callable[[str], ReadOnlyToolClient]):
        if evidence.cases.tenant_id != cases.tenant_id or evidence.engine is not cases.engine:
            raise ValueError("case and evidence repositories must share tenant and engine")
        self.cases, self.evidence, self._client_for_case = cases, evidence, client_for_case

    def execute(self, case_id: str, tool: ToolName, query: ToolQuery) -> CaseToolResult:
        tool = ToolName(tool)
        query = ToolQuery.model_validate(query)
        _, call_id = self.cases.reserve_call(case_id, tool, query)
        try:
            # The configured client uses the existing tool credential and HTTP API.
            # Its result is verified against durable ObservationRow before extraction.
            observation = self._client_for_case(case_id).observe(
                tool, query, dispatch_correlation_id=call_id,
            )
            refs = self.evidence.record_call(case_id, call_id, observation)
        except Exception:
            # A dispatched attempt consumes budget even on transport failure/crash.
            # We never manufacture a business Observation from a local HTTP exception.
            self.cases.mark_error(case_id, call_id)
            raise
        return CaseToolResult(case_id=case_id, call_id=call_id, observation=observation, evidence_refs=refs)
