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
from credit_harness.recovery.case import CaseRecoveryCoordinator
from credit_harness.recovery.checkpoint import AgentCheckpointStore


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
        if trace_store is None:
            from credit_harness.investigation.trace_store import SQLInvestigationTraceStore
            trace_store = SQLInvestigationTraceStore(cases)
            trace_store.create_schema()
        self.trace_store = trace_store
        self.recovery = CaseRecoveryCoordinator(cases, evidence)
        self.checkpoints = AgentCheckpointStore(cases)

    def load_current_context(self, case_id):
        # Never use an old snapshot, observation, conversation or previous summary
        # as the source of truth. Concurrent changes are fenced by the later CAS.
        case = self.cases.get(case_id)
        evidence = self.evidence.list(case_id)
        return case, evidence, self.assembler.build(case, evidence)

    def run(self, case_id: str, *, lineage=None, run_id=None, lease_guard=None) -> AgentRunResult:
        self.recovery.before_investigation(case_id)
        case, evidence, initial = self.load_current_context(case_id)
        run_id = run_id or str(uuid4())
        self.checkpoints.save(run_id, case_id, 0, initial.snapshot_id)
        current = initial
        turns, decision_ids = [], []
        dispatched, no_progress = 0, 0
        stop = Stop.MAX_TURNS_REACHED
        for number in range(1, self.config.max_turns + 1):
            if lease_guard is not None:
                lease_guard()
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
                    if lease_guard is not None:
                        precondition = precondition.model_copy(update=dict(work_lease=lease_guard()))
                        attempt = attempt.model_copy(update=dict(execution_precondition=precondition))
                        attempts[-1] = attempt
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
                        from credit_harness.orchestration.models import PauseReservation, WorkReason, SignalType
                        waiting = isinstance(candidate, WaitCandidate)
                        from credit_harness.hypotheses.models import UncollectedClaimType
                        deployed = getattr(candidate, "requested_capability", None) == UncollectedClaimType.DEPLOYED_CONSUMER_SCHEMA_VERSION
                        source_failure = candidate.reason_code.value == "REPEATED_SOURCE_FAILURE"
                        reason = (WorkReason.WAIT_REQUESTED if waiting else
                            WorkReason.DEPLOYMENT_STATE_UNKNOWN if deployed else
                            WorkReason.SOURCE_UNAVAILABLE if source_failure else
                            WorkReason.CAPABILITY_UNAVAILABLE if candidate.reason_code.value == "NO_AVAILABLE_TOOL" else
                            WorkReason.OPERATOR_REQUIRED)
                        signal = (SignalType.DEPLOYMENT_STATE_UPDATED if reason == WorkReason.DEPLOYMENT_STATE_UNKNOWN else
                            SignalType.SOURCE_RECOVERED if source_failure else
                            SignalType.CAPABILITY_AVAILABLE if reason == WorkReason.CAPABILITY_UNAVAILABLE else
                            SignalType.OPERATOR_ACKNOWLEDGED)
                        paused = self.cases.pause_if_current(case_id, status, precondition=precondition,
                            orchestration=PauseReservation(decision_id=decision.decision_id,
                                snapshot_id=planned_snapshot.snapshot_id, run_id=run_id, reason=reason,
                                wait_seconds=candidate.suggested_wait_seconds if waiting else None,
                                required_signal=None if waiting else signal))
                        status = paused.status
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
            self.checkpoints.save(run_id, case_id, number, current.snapshot_id,
                                  last.decision.decision_id if last else None, call_id, turn_stop,
                                  completed_turn=turns[-1], trace_store=self.trace_store
                                  if hasattr(self.trace_store, "record_turn") else None)
            if turn_stop is not None:
                stop = turn_stop
                break
            no_progress = 0 if progress.has_progress else no_progress + 1
            if no_progress >= self.config.max_consecutive_no_knowledge_progress:
                stop = Stop.NO_KNOWLEDGE_PROGRESS
                break
        result = AgentRunResult(run_id=run_id, case_id=case_id, lineage=lineage,
            initial_snapshot_id=initial.snapshot_id, final_snapshot_id=current.snapshot_id,
            status=stop, turn_count=len(turns), tool_calls_dispatched=dispatched,
            planner_decision_ids=tuple(decision_ids), turns=tuple(turns), final_case_status=case.status)
        self.trace_store.append(result)
        last_turn = turns[-1] if turns else None
        self.checkpoints.save(run_id, case_id, len(turns), current.snapshot_id,
            last_turn.planner_decision_id if last_turn else None, last_turn.tool_call_id if last_turn else None, stop)
        return result
