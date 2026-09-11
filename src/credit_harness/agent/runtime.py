from uuid import uuid4

from credit_harness.cases.executor import CaseToolExecutor, CaseToolExecutionError
from credit_harness.cases.models import CaseStatus, CasePolicyError
from credit_harness.cases.preconditions import AgentPreconditionFailed
from credit_harness.cases.repository import CaseRepository
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.planner.models import CallToolCandidate, WaitCandidate, PlannerUnavailable
from credit_harness.planner.service import PlannerService
from .models import (
    AgentRunConfig, AgentRunResult, AgentRunStatus as Stop, AgentTurnTrace,
    ExecutionCASStatus as CAS, PlanningAttemptTrace, RevalidationStatus as R,
    RuntimeRevalidationResult, TurnOutcome as Outcome,
)
from .progress import knowledge_progress
from .query import build_tool_query
from .revalidation import RuntimeActionRevalidator, execution_precondition
from .trace import InMemoryAgentRunTraceStore


class InvestigationAgentRuntime:
    def __init__(self, cases: CaseRepository, evidence: EvidenceRepository,
                 assembler: ReasoningContextAssembler, planner: PlannerService,
                 executor: CaseToolExecutor, *, config: AgentRunConfig | None = None,
                 trace_store=None):
        if (evidence.cases.tenant_id != cases.tenant_id or evidence.engine is not cases.engine
                or executor.cases is not cases or executor.evidence is not evidence):
            raise ValueError("runtime dependencies must share case/evidence execution boundary")
        self.cases, self.evidence, self.assembler = cases, evidence, assembler
        self.planner, self.executor = planner, executor
        self.config = config or AgentRunConfig()
        self.revalidator = RuntimeActionRevalidator()
        self.trace_store = trace_store if trace_store is not None else InMemoryAgentRunTraceStore()

    def load_current_context(self, case_id):
        # Never use an old snapshot, observation, conversation or previous summary
        # as the source of truth. Concurrent changes are fenced by the later CAS.
        case = self.cases.get(case_id)
        evidence = self.evidence.list(case_id)
        return case, evidence, self.assembler.build(case, evidence)

    def run(self, case_id: str) -> AgentRunResult:
        case, evidence, initial = self.load_current_context(case_id)
        current = initial
        turns, decision_ids = [], []
        dispatched, no_progress = 0, 0
        stop = Stop.MAX_TURNS_REACHED
        for number in range(1, self.config.max_turns + 1):
            case, evidence, before = self.load_current_context(case_id)
            current = before
            if case.status not in (CaseStatus.NEW, CaseStatus.INVESTIGATING):
                stop = Stop.RUNTIME_SAFETY_STOP
                break
            attempts, seen_snapshots = [], set()
            tool_name, call_id, new_refs = None, None, ()
            outcome = Outcome.RUNTIME_SAFETY_STOP
            turn_stop = Stop.RUNTIME_SAFETY_STOP
            for _ in range(self.config.max_stale_replans_per_turn + 1):
                # Do not ask the same model input repeatedly until it says yes.
                if current.snapshot_id in seen_snapshots:
                    outcome, turn_stop = Outcome.STALE_REPLAN_EXHAUSTED, Stop.STALE_REPLAN_EXHAUSTED
                    break
                seen_snapshots.add(current.snapshot_id)
                planned_snapshot = current
                try:
                    decision = self.planner.plan(planned_snapshot)
                except PlannerUnavailable:
                    case, evidence, current = self.load_current_context(case_id)
                    outcome, turn_stop = Outcome.PLANNER_UNAVAILABLE, Stop.PLANNER_UNAVAILABLE
                    break
                decision_ids.append(decision.decision_id)
                case, evidence, current = self.load_current_context(case_id)
                result = self.revalidator.validate(case, current, decision)
                attempt = PlanningAttemptTrace(snapshot_id=planned_snapshot.snapshot_id,
                    fresh_snapshot_id=current.snapshot_id, decision=decision, revalidation=result)
                attempts.append(attempt)
                if result.status == R.NO_SELECTED_ACTION:
                    outcome, turn_stop = Outcome.SAFE_NO_ACTION, Stop.SAFE_NO_ACTION
                    break
                if result.status in (R.STALE_SNAPSHOT, R.STALE_POLICY):
                    outcome, turn_stop = Outcome.STALE_REPLAN_EXHAUSTED, Stop.STALE_REPLAN_EXHAUSTED
                    continue
                if result.status != R.VALID:
                    break
                precondition = execution_precondition(case, current)
                attempt = attempt.model_copy(update={"execution_precondition": precondition})
                attempts[-1] = attempt
                candidate = decision.selected_action.candidate
                try:
                    if isinstance(candidate, CallToolCandidate):
                        query = build_tool_query(case, candidate)
                        tool_name = candidate.tool_name
                        known_ids = {e.evidence_id for e in evidence}
                        # The ONLY agent dispatch path. No tool client access here.
                        receipt = self.executor.execute_if_current(case_id, tool_name, query,
                                                                  precondition=precondition)
                        call_id = receipt.call_id
                        new_refs = tuple(ref for ref in receipt.evidence_refs if ref not in known_ids)
                        dispatched += 1
                        outcome, turn_stop = Outcome.TOOL_OBSERVED, None
                    else:
                        status = CaseStatus.WAITING if isinstance(candidate, WaitCandidate) else CaseStatus.ESCALATED
                        self.cases.pause_if_current(case_id, status, precondition=precondition)
                        outcome = Outcome.WAITING if status == CaseStatus.WAITING else Outcome.ESCALATED
                        turn_stop = Stop(outcome.value)
                    attempts[-1] = attempt.model_copy(update={"execution_cas": CAS.PASSED})
                except AgentPreconditionFailed:
                    attempts[-1] = attempt.model_copy(update={"execution_cas": CAS.FAILED,
                        "revalidation": RuntimeRevalidationResult(status=R.CASE_PRECONDITION_FAILED)})
                    tool_name = None  # nothing dispatched
                    case, evidence, current = self.load_current_context(case_id)
                    outcome, turn_stop = Outcome.STALE_REPLAN_EXHAUSTED, Stop.STALE_REPLAN_EXHAUSTED
                    continue
                except CaseToolExecutionError as error:
                    call_id = error.call_id
                    dispatched += 1
                    attempts[-1] = attempt.model_copy(update={"execution_cas": CAS.PASSED})
                    outcome, turn_stop = Outcome.TOOL_EXECUTION_ERROR, Stop.TOOL_EXECUTION_ERROR
                    # Budget is already consumed, CaseCall ERROR. No fabricated
                    # observation/evidence, no transport retry or orphan recovery.
                except (CasePolicyError, ValueError):
                    attempts[-1] = attempt.model_copy(update={"revalidation":
                        RuntimeRevalidationResult(status=R.QUERY_REBUILD_FAILED)})
                    outcome, turn_stop = Outcome.RUNTIME_SAFETY_STOP, Stop.RUNTIME_SAFETY_STOP
                case, evidence, current = self.load_current_context(case_id)
                break  # at most one dispatch or lifecycle action in this turn
            last = attempts[-1] if attempts else None
            progress = knowledge_progress(before, current)
            turns.append(AgentTurnTrace(turn_number=number, before_snapshot_id=before.snapshot_id,
                planner_decision_id=last.decision.decision_id if last else None,
                selected_action=last.decision.selected_action if last else None,
                runtime_revalidation=last.revalidation if last else None,
                execution_precondition=last.execution_precondition if last else None,
                execution_cas=last.execution_cas if last else CAS.NOT_ATTEMPTED,
                tool_name=tool_name, tool_call_id=call_id, new_evidence_refs=new_refs,
                after_snapshot_id=current.snapshot_id, knowledge_progress=progress,
                case_status_after=case.status, turn_outcome=outcome, attempts=tuple(attempts)))
            if turn_stop is not None:
                stop = turn_stop
                break
            no_progress = 0 if progress.has_progress else no_progress + 1
            if no_progress >= self.config.max_consecutive_no_knowledge_progress:
                stop = Stop.NO_KNOWLEDGE_PROGRESS
                break
        result = AgentRunResult(run_id=str(uuid4()), case_id=case_id,
            initial_snapshot_id=initial.snapshot_id, final_snapshot_id=current.snapshot_id,
            status=stop, turn_count=len(turns), tool_calls_dispatched=dispatched,
            planner_decision_ids=tuple(decision_ids), turns=tuple(turns), final_case_status=case.status)
        self.trace_store.append(result)
        return result
