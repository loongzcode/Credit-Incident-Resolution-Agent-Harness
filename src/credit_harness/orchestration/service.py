from sqlalchemy.orm import Session
from credit_harness.cases.models import CaseStatus
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
from credit_harness.domain.enums import ToolName as Tool
from credit_harness.evaluation.models import VerificationRequirement as Q
from credit_harness.evaluation.repository import EvaluationRepository
from credit_harness.tools.contracts import ToolQuery
from credit_harness.agent.revalidation import execution_precondition
from .models import WorkType as T, WorkStatus as S, WorkReason as R, WorkEvent as E, RunLineage, OrchestrationError
from .tables import ResumedRunRow
from .repository import lock_case, save_work, state_in, save_state, audit, escalate_in
from .resume import CaseResumeService, knowledge_fingerprints
from .handoff import EvaluationHandoffService

# Small read-only verification contract. No planner, write command, arbitrary
# query or all-tools sweep. Unsupported requirements go to an operator.
VERIFICATION_TOOLS = {
    Q.PAYMENT_FINALITY: Tool.PAYMENT, Q.PAYMENT_IDENTITY: Tool.PAYMENT,
    Q.REQUEST_ASSOCIATION: Tool.GUARANTEE, Q.FUND_FINAL_STATE: Tool.FUND,
    Q.GUARANTEE_FINAL_STATE: Tool.GUARANTEE, Q.ASSET_FINAL_STATE: Tool.ASSET,
    Q.ACCOUNTING_ENTRY: Tool.ACCOUNTING, Q.CALLBACK_CONSUMPTION: Tool.MESSAGES,
    Q.ASSET_DELIVERY: Tool.ASSET_DELIVERY, Q.POST_EFFECT_MESSAGE_STATUS: Tool.MESSAGES,
    Q.POST_EFFECT_DELIVERY_STATUS: Tool.ASSET_DELIVERY,
}


class DurableCaseOrchestrator:
    """One explicit tick / one work item. No background daemon or automatic L2."""
    def __init__(self, repository, evidence, executor, runtime, evaluator, closure, *, effect_recovery=None):
        cases = repository.cases
        if (executor.cases is not cases or runtime.cases is not cases or evaluator.cases is not cases
                or executor.evidence is not evidence or runtime.evidence is not evidence):
            raise ValueError("worker dependencies must share Case boundary")
        self.repository, self.evidence, self.executor = repository, evidence, executor
        self.runtime, self.evaluator = runtime, evaluator
        self.resume_service = CaseResumeService(repository, evidence, effect_recovery=effect_recovery)
        self.handoff = EvaluationHandoffService(repository, closure)
        self.reports = EvaluationRepository(cases)

    def tick(self, worker_id):
        claim = self.repository.claim_next(worker_id)
        return self.process(claim) if claim else None

    def process(self, claim):
        repo = self.repository
        work = self.resume_service.resume(claim)
        if work is None:
            return None
        work = self.resume_service.start_once(claim)
        before = knowledge_fingerprints(self.evidence.list(work.case_id))
        with Session(repo.engine) as session, session.begin():
            lock_case(session, repo.cases, work.case_id)
            row, work = repo.owned(session, claim, repo.clock())
            save_work(row, work.model_copy(update=dict(before_evidence_fingerprint=before[0],
                before_progress_fingerprint=before[1])))
        result = None
        if work.work_type == T.VERIFICATION_REQUIRED:
            case = repo.cases.get(work.case_id)
            available = {t.tool_name for t in ToolCapabilityCatalog().for_case(case)}
            tool = VERIFICATION_TOOLS.get(work.requirement)
            if tool not in available:
                self._block(claim, R.NO_TOOL)
                return None
            requirements = work.verification_requirements or (work.requirement,)
            # A report's finite read set avoids evaluating half-refreshed state.
            # Unsupported requirements remain gaps; they never become fake reads.
            tools = tuple(dict.fromkeys(VERIFICATION_TOOLS[q] for q in requirements
                if q in VERIFICATION_TOOLS and VERIFICATION_TOOLS[q] in available))
            for tool in tools:
                fence = repo.renew(claim)
                case = repo.cases.get(work.case_id)
                if case.budget.used_tool_calls >= case.budget.max_tool_calls:
                    self._block(claim, R.TOOL_BUDGET_EXHAUSTED)
                    return None
                snapshot = ReasoningContextAssembler().build(case, self.evidence.list(work.case_id))
                self.executor.execute_if_current(work.case_id, tool, ToolQuery(internal_order_id=case.internal_order_id),
                    precondition=execution_precondition(case, snapshot).model_copy(update=dict(work_lease=fence)))
        elif work.work_type != T.RECOVERY_RECHECK:
            result = self.runtime.run(work.case_id, run_id=work.run_id,
                lineage=RunLineage(parent_run_id=work.previous_run_id,
                    resumed_from_work_item_id=work.work_item_id, resume_reason=work.reason_code, resume_trigger=work.trigger),
                lease_guard=lambda: repo.renew(claim))
        # No evidence is created by this evaluation/handoff. Real read publication
        # above remains the only Evidence entry point.
        repo.renew(claim)
        report = self.reports.record(self.evaluator.evaluate(work.case_id))
        after = knowledge_fingerprints(self.evidence.list(work.case_id))
        escalated = self._finish(claim, before, after, report, result)
        if not escalated:
            self.handoff.consume(report)
        return result or report

    def _block(self, claim, reason):
        repo = self.repository; work = repo.get(claim.work_item_id); now = repo.clock()
        with Session(repo.engine) as session, session.begin():
            case = lock_case(session, repo.cases, work.case_id)
            row, item = repo.owned(session, claim, now)
            changed = item.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
            save_work(row, changed); audit(session, changed, E.BLOCKED, now, item.status, claim.worker_id)
            escalate_in(session, case, reason, item.work_item_id, now)

    def _finish(self, claim, before, after, report, result):
        repo = self.repository; old = repo.get(claim.work_item_id); now = repo.clock()
        with Session(repo.engine) as session, session.begin():
            case = lock_case(session, repo.cases, old.case_id)
            row, work = repo.owned(session, claim, now)
            state_row, state = state_in(session, case)
            requirements = tuple(sorted({q.requirement for q in report.unresolved_requirements}))
            progress = before[1] != after[1] or (state.last_progress_fingerprint is not None
                and state.last_progress_fingerprint != after[1])
            no_progress = 0 if progress else state.no_progress_count+1
            save_state(state_row, state.model_copy(update=dict(no_progress_count=no_progress,
                last_progress_fingerprint=after[1])))
            finished = work.model_copy(update=dict(status=S.COMPLETED, completed_at=now, lease_until=None,
                after_progress_fingerprint=after[1], after_evidence_fingerprint=after[0],
                verification_requirements=requirements))
            save_work(row, finished); audit(session, finished, E.COMPLETED, now, work.status, claim.worker_id)
            if result is not None:
                session.add(ResumedRunRow(run_id=result.run_id, work_item_id=work.work_item_id,
                    payload=result.model_dump(mode="json")))
            escalated = no_progress >= state.budget.max_no_progress_cycles and report.overall_verdict.value != "PASS"
            if escalated:
                escalate_in(session, case, R.NO_PROGRESS, work.work_item_id, now)
            return escalated

    def runs(self, case_id):
        from sqlalchemy import select
        from credit_harness.agent.models import AgentRunResult
        ids = [w.work_item_id for w in self.repository.list(case_id)]
        with Session(self.repository.engine) as session:
            return tuple(AgentRunResult.model_validate(r.payload) for r in session.scalars(
                select(ResumedRunRow).where(ResumedRunRow.work_item_id.in_(ids))))
