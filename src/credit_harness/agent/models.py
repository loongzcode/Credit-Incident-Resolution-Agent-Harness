from enum import StrEnum
from typing import Annotated

from pydantic import Field, StrictInt

from credit_harness.domain.models import Model
from credit_harness.domain.enums import ToolName
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.preconditions import AgentExecutionPrecondition
from credit_harness.context.models import Hash
from credit_harness.hypotheses.models import HypothesisId, HypothesisStatus
from credit_harness.identity.models import IdentityMatch
from credit_harness.planner.models import PlannerDecision, RejectionCode, ValidatedActionProposal


class AgentRunConfig(Model):
    max_turns: Annotated[StrictInt, Field(ge=1, le=100)] = 12
    max_stale_replans_per_turn: Annotated[StrictInt, Field(ge=0, le=10)] = 3
    max_consecutive_no_knowledge_progress: Annotated[StrictInt, Field(ge=1, le=20)] = 2


class AgentRunStatus(StrEnum):
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    SAFE_NO_ACTION = "SAFE_NO_ACTION"
    PLANNER_UNAVAILABLE = "PLANNER_UNAVAILABLE"
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
    MAX_TURNS_REACHED = "MAX_TURNS_REACHED"
    STALE_REPLAN_EXHAUSTED = "STALE_REPLAN_EXHAUSTED"
    NO_KNOWLEDGE_PROGRESS = "NO_KNOWLEDGE_PROGRESS"
    RUNTIME_SAFETY_STOP = "RUNTIME_SAFETY_STOP"


class RevalidationStatus(StrEnum):
    VALID = "VALID"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    STALE_POLICY = "STALE_POLICY"
    CANDIDATE_REJECTED = "CANDIDATE_REJECTED"
    CASE_PRECONDITION_FAILED = "CASE_PRECONDITION_FAILED"
    QUERY_REBUILD_FAILED = "QUERY_REBUILD_FAILED"
    NO_SELECTED_ACTION = "NO_SELECTED_ACTION"


class RuntimeRevalidationResult(Model):
    status: RevalidationStatus
    rejection_codes: tuple[RejectionCode, ...] = ()


class ExecutionCASStatus(StrEnum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    PASSED = "PASSED"
    FAILED = "FAILED"


class HypothesisKnowledge(Model):
    hypothesis_id: HypothesisId
    status: HypothesisStatus
    decisive_evidence_refs: tuple[str, ...]


class KnowledgeSummary(Model):
    identity: IdentityMatch
    hypotheses: tuple[HypothesisKnowledge, ...]
    open_gap_ids: tuple[str, ...]


class KnowledgeProgress(Model):
    new_evidence: bool
    hypothesis_graph_fingerprint_changed: bool
    hypothesis_changed: bool
    identity_changed: bool
    gap_changed: bool
    history_changed: bool
    facts_changed: bool
    has_progress: bool
    before: KnowledgeSummary
    after: KnowledgeSummary


class PlanningAttemptTrace(Model):
    snapshot_id: Hash
    fresh_snapshot_id: Hash
    decision: PlannerDecision
    revalidation: RuntimeRevalidationResult
    execution_precondition: AgentExecutionPrecondition | None = None
    execution_cas: ExecutionCASStatus = ExecutionCASStatus.NOT_ATTEMPTED


class TurnOutcome(StrEnum):
    TOOL_OBSERVED = "TOOL_OBSERVED"
    WAITING = "WAITING"
    ESCALATED = "ESCALATED"
    SAFE_NO_ACTION = "SAFE_NO_ACTION"
    PLANNER_UNAVAILABLE = "PLANNER_UNAVAILABLE"
    TOOL_EXECUTION_ERROR = "TOOL_EXECUTION_ERROR"
    STALE_REPLAN_EXHAUSTED = "STALE_REPLAN_EXHAUSTED"
    RUNTIME_SAFETY_STOP = "RUNTIME_SAFETY_STOP"


class AgentTurnTrace(Model):
    turn_number: int
    before_snapshot_id: Hash
    planner_decision_id: Hash | None
    selected_action: ValidatedActionProposal | None
    runtime_revalidation: RuntimeRevalidationResult | None
    execution_precondition: AgentExecutionPrecondition | None
    execution_cas: ExecutionCASStatus
    tool_name: ToolName | None
    tool_call_id: str | None
    new_evidence_refs: tuple[str, ...]
    after_snapshot_id: Hash
    knowledge_progress: KnowledgeProgress
    case_status_after: CaseStatus
    turn_outcome: TurnOutcome
    attempts: tuple[PlanningAttemptTrace, ...]


class AgentRunResult(Model):
    run_id: str
    case_id: str
    initial_snapshot_id: Hash
    final_snapshot_id: Hash
    status: AgentRunStatus
    turn_count: int
    tool_calls_dispatched: int
    planner_decision_ids: tuple[Hash, ...]
    turns: tuple[AgentTurnTrace, ...]
    final_case_status: CaseStatus
