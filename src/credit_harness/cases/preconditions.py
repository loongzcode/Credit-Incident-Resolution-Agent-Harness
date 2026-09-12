"""Trusted runtime concurrency contract; neither an API credential nor a capability."""
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictInt

from credit_harness.domain.models import Model
from .models import CaseStatus, CasePolicyError


class WorkLeasePrecondition(Model):
    work_item_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    worker_id: str
    lease_token: str


class AgentExecutionPrecondition(Model):
    case_id: str
    tenant_id: str
    expected_case_status: CaseStatus
    expected_used_tool_calls: Annotated[StrictInt, Field(ge=0)]
    expected_case_updated_at: AwareDatetime
    expected_snapshot_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    planner_policy_version: str
    work_lease: WorkLeasePrecondition | None = None
    registry_version: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    routing_fingerprint: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


class AgentPreconditionFailed(CasePolicyError):
    """No reservation or pause was committed; dispatch is forbidden."""
