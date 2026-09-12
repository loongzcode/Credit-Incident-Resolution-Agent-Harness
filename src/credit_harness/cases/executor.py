from collections.abc import Callable
from typing import Protocol

from credit_harness.domain.enums import ToolName
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.tools.contracts import DispatchCorrelationId, Observation, ToolQuery
from .models import CaseToolResult
from .repository import CaseRepository
from .preconditions import AgentExecutionPrecondition, AgentPreconditionFailed


class CaseToolExecutionError(RuntimeError):
    def __init__(self, call_id: str):
        super().__init__("reserved tool call failed; transport outcome may be ambiguous")
        self.call_id = call_id


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
        return self._execute(case_id, tool, query)

    def execute_if_current(self, case_id: str, tool: ToolName, query: ToolQuery, *,
                           precondition: AgentExecutionPrecondition) -> CaseToolResult:
        if type(precondition) is not AgentExecutionPrecondition:
            raise AgentPreconditionFailed("agent execution requires precondition")
        return self._execute(case_id, tool, query, precondition=precondition)

    def _execute(self, case_id, tool, query, *, precondition=None):
        tool = ToolName(tool)
        query = ToolQuery.model_validate(query)
        _, call_id = self.cases.reserve_call(case_id, tool, query, precondition=precondition)
        try:
            # The configured client uses the existing tool credential and HTTP API.
            # Its result is verified against durable ObservationRow before extraction.
            client = (self.cases.registry_guard.client(case_id, call_id) if self.cases.registry_guard
                      else self._client_for_case(case_id))
            observation = client.observe(
                tool, query, dispatch_correlation_id=call_id,
            )
            refs = self.evidence.record_call(case_id, call_id, observation)
        except Exception:
            # A dispatched attempt consumes budget even on transport failure/crash.
            # We never manufacture a business Observation from a local HTTP exception.
            self.cases.mark_error(case_id, call_id)
            if precondition is not None:
                raise CaseToolExecutionError(call_id) from None
            raise
        return CaseToolResult(case_id=case_id, call_id=call_id, observation=observation, evidence_refs=refs)
