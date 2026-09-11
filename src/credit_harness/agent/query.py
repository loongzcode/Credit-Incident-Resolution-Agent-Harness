from credit_harness.cases.models import Case, CasePolicyError
from credit_harness.domain.enums import ToolName
from credit_harness.planner.models import CallToolCandidate
from credit_harness.tools.contracts import ToolQuery


def build_tool_query(fresh_case: Case, candidate: CallToolCandidate) -> ToolQuery:
    candidate = CallToolCandidate.model_validate(candidate.model_dump())
    proposed = candidate.query
    if (proposed.internal_order_id != fresh_case.internal_order_id
            or proposed.internal_order_id not in fresh_case.scope.allowed_order_ids
            or candidate.tool_name not in fresh_case.scope.allowed_tools
            or candidate.tool_name.value in fresh_case.constraints.forbidden_actions):
        raise CasePolicyError("query outside current case scope")
    if candidate.tool_name != ToolName.PROTOCOL and (
        proposed.protocol_version is not None or proposed.effective_at is not None
    ):
        raise CasePolicyError("protocol parameters require protocol tool")
    # Explicit allowlist construction. Never forward a model dict or alias map.
    return ToolQuery(internal_order_id=fresh_case.internal_order_id,
                     protocol_version=proposed.protocol_version, effective_at=proposed.effective_at)
