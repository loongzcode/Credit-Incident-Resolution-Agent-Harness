from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictInt, model_validator

from credit_harness.domain.enums import ToolName
from credit_harness.domain.models import Model
from credit_harness.tools.contracts import Observation

Identifier = Annotated[str, Field(min_length=1, max_length=80)]
Text = Annotated[str, Field(min_length=1)]


class CaseStatus(StrEnum):
    NEW = "NEW"
    INVESTIGATING = "INVESTIGATING"
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    CLOSED = "CLOSED"


class TaskContract(Model):
    goal: Text
    success_criteria: tuple[Text, ...] = Field(min_length=1)
    stop_conditions: tuple[Text, ...] = Field(min_length=1)
    escalation_conditions: tuple[Text, ...] = Field(min_length=1)
    forbidden_outcomes: tuple[Text, ...] = Field(min_length=1)


class CaseScope(Model):
    allowed_order_ids: frozenset[Identifier] = Field(min_length=1)
    allowed_tools: frozenset[ToolName] = Field(min_length=1)


class CaseConstraints(Model):
    forbidden_actions: tuple[Text, ...]


class CaseBudget(Model):
    max_tool_calls: Annotated[StrictInt, Field(ge=0)]
    used_tool_calls: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def bounded(self):
        if self.used_tool_calls > self.max_tool_calls:
            raise ValueError("used budget exceeds maximum")
        return self


class Case(Model):
    case_id: Identifier
    simulation_id: Identifier
    tenant_id: Identifier
    internal_order_id: Identifier
    goal: Text
    task_contract: TaskContract
    status: CaseStatus = CaseStatus.NEW
    created_at: AwareDatetime
    updated_at: AwareDatetime
    scope: CaseScope
    constraints: CaseConstraints
    budget: CaseBudget

    @model_validator(mode="after")
    def consistent(self):
        if self.goal != self.task_contract.goal:
            raise ValueError("goal must match task contract")
        if self.internal_order_id not in self.scope.allowed_order_ids:
            raise ValueError("primary order must be in scope")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        return self


class CaseToolResult(Model):
    case_id: str
    call_id: str
    observation: Observation
    evidence_refs: tuple[str, ...]


class CaseAccessError(Exception):
    """Missing case, tenant mismatch, or invalid upstream grant."""


class CasePolicyError(Exception):
    """Case lifecycle, scope or budget prevents dispatch."""
