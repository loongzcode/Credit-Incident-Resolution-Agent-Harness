"""Trusted runtime concurrency contract; neither an API credential nor a capability."""
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictInt

from credit_harness.domain.models import Model
from .models import CaseStatus, CasePolicyError


class AgentExecutionPrecondition(Model):
    case_id: str
    tenant_id: str
    expected_case_status: CaseStatus
    expected_used_tool_calls: Annotated[StrictInt, Field(ge=0)]
    expected_case_updated_at: AwareDatetime
    expected_snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    planner_policy_version: str


class AgentPreconditionFailed(CasePolicyError):
    """No reservation or pause was committed; dispatch is forbidden."""
