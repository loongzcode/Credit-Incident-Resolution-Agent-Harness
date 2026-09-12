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
from .resume import CaseResumeService
from .handoff import EvaluationHandoffService

from .routing import VERIFICATION_TOOLS


class DurableCaseOrchestrator:
    """One explicit tick / one work item. No background daemon or automatic L2."""
    def __init__(self, repository, evidence, executor, runtime, evaluator, closure, *, effect_recovery=None):
        cases = repository.cases
        if (executor.cases is not cases or runtime.cases is not cases or evaluator.cases is not cases
                or executor.evidence is not evidence or runtime.evidence is not evidence):
            raise ValueError("worker dependencies must share Case boundary")
        self.repository, self.evidence, self.executor = repository, evidence, executor
        self.runtime, self.evaluator = runtime, evaluator
        self.resume_service = CaseResumeService(repository, evidence, effect_recovery=effect_recovery, evaluator=evaluator)
        self.handoff = EvaluationHandoffService(repository, closure)
        self.reports = EvaluationRepository(cases)

    def tick(self, worker_id):
        claim = self.repository.claim_next(worker_id)
        return self.process(claim) if claim else None

    def process(self, claim):
        repo = self.repository
        if repo.get(claim.work_item_id).work_type == T.RECOVERY_RECHECK:
            return self._recheck(claim)
        work = self.resume_service.resume(claim)
        if work is None:
            return None
        work = self.resume_service.start_once(claim)
        before = work.progress_before
        with Session(repo.engine) as session, session.begin():
            lock_case(session, repo.cases, work.case_id)
            row, work = repo.owned(session, claim, repo.clock())
            save_work(row, work.model_copy(update=dict(before_evidence_fingerprint=before.evidence_fingerprint,
                before_progress_fingerprint=before.fingerprint)))
        result = None
        if work.work_type == T.VERIFICATION_REQUIRED:
            case = repo.cases.get(work.case_id)
            from credit_harness.registry.verification import verification_tools
            guard = repo.cases.registry_guard
            mapping = verification_tools(case, resolver=guard.resolver if guard else None)
            tool = mapping.get(work.requirement)
            if tool is None:
                self._block(claim, R.NO_TOOL)
                return None
            requirements = work.verification_requirements or (work.requirement,)
            # A report's finite read set avoids evaluating half-refreshed state.
            # Unsupported requirements remain gaps; they never become fake reads.
            tools = tuple(dict.fromkeys(mapping[q] for q in requirements if q in mapping))
            for tool in tools:
                fence = repo.renew(claim)
                case = repo.cases.get(work.case_id)
                if case.budget.used_tool_calls >= case.budget.max_tool_calls:
                    self._block(claim, R.TOOL_BUDGET_EXHAUSTED)
                    return None
                from credit_harness.registry.snapshot import RegistryBackedCatalog
                snapshot = ReasoningContextAssembler(catalog=RegistryBackedCatalog(guard.resolver) if guard else None
                    ).build(case, self.evidence.list(work.case_id))
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
        after = self.resume_service.progress(work.case_id, report)
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
            progress = before.fingerprint != after.fingerprint or (state.last_progress_version == after.version and state.last_progress_fingerprint is not None
                and state.last_progress_fingerprint != after.fingerprint)
            no_progress = 0 if progress else state.no_progress_count+1
            save_state(state_row, state.model_copy(update=dict(no_progress_count=no_progress,
                last_progress_fingerprint=after.fingerprint, last_progress_version=after.version)))
            finished = work.model_copy(update=dict(status=S.COMPLETED, completed_at=now, lease_until=None,
                after_progress_fingerprint=after.fingerprint, after_evidence_fingerprint=after.evidence_fingerprint,
                verification_requirements=requirements))
            save_work(row, finished); audit(session, finished, E.COMPLETED, now, work.status, claim.worker_id)
            if result is not None:
                session.add(ResumedRunRow(run_id=result.run_id, work_item_id=work.work_item_id,
                    payload=result.model_dump(mode="json")))
            escalated = no_progress >= state.budget.max_no_progress_cycles and report.overall_verdict.value != "PASS"
            if escalated:
                escalate_in(session, case, R.NO_PROGRESS, work.work_item_id, now)
            return escalated

    def _recheck(self, claim):
        """One Step 9 attempt; never route unresolved finality to a read Tool."""
        from datetime import datetime, timezone
        from credit_harness.authorization.tables import EffectRow
        from credit_harness.authorization.models import SideEffectLedger
        from credit_harness.recovery.tables import EffectRecoveryStateRow
        from credit_harness.recovery.repository import RECOVERABLE
        from .handoff import effect_handoff
        repo = self.repository
        old = repo.get(claim.work_item_id)
        if old.status == S.COMPLETED:
            repo.complete(claim)
            return None
        recovery = self.resume_service.recover_before_resume(claim)
        if recovery is None:
            return None
        after = self.resume_service.progress(old.case_id)
        with Session(repo.engine) as session, session.begin():
            current = self.resume_service.effect_guard.current(session, claim)
            if current is None:
                return None
            case, row, work = current
            effect = session.get(EffectRow, work.source_ref)
            state_row, state = state_in(session, case)
            progress = work.progress_before.fingerprint != after.fingerprint or (
                state.last_progress_version == after.version and state.last_progress_fingerprint is not None
                and state.last_progress_fingerprint != after.fingerprint)
            # Recovery retry limits belong exclusively to Step 9. Preserve the
            # persisted investigation count on UNKNOWN -> UNKNOWN.
            save_state(state_row, state.model_copy(update=dict(no_progress_count=0 if progress else state.no_progress_count,
                last_progress_fingerprint=after.fingerprint, last_progress_version=after.version)))
            changed = work.model_copy(update=dict(after_progress_fingerprint=after.fingerprint,
                after_evidence_fingerprint=after.evidence_fingerprint, verification_requirements=after.requirements))
            if effect is None or effect.case_id != case.case_id or self.resume_service.effect_recovery is None:
                self._block_recheck(session, case, row, changed, claim)
                return recovery
            ledger = SideEffectLedger.model_validate(effect.payload)
            policy_state = session.get(EffectRecoveryStateRow, ledger.effect_id)
            policy = self.resume_service.effect_recovery.repository.policy
            if ledger.status not in RECOVERABLE:
                changed = changed.model_copy(update=dict(status=S.COMPLETED, completed_at=repo.clock(), lease_until=None))
                save_work(row, changed); audit(session, changed, E.COMPLETED, repo.clock(), work.status, claim.worker_id)
                effect_handoff(session, ledger)
            elif policy_state.requires_escalation or policy_state.attempt_count >= policy.max_attempts:
                self._block_recheck(session, case, row, changed, claim)
            else:
                eligible = max(policy_state.next_eligible_at,
                    policy_state.ledger_updated_at + policy.grace_seconds, policy_state.lease_until or 0)
                changed = changed.model_copy(update=dict(status=S.PENDING,
                    not_before=datetime.fromtimestamp(eligible, timezone.utc), claimed_by=None,
                    lease_token=None, lease_until=None, progress_before=None, recovery_reads=()))
                save_work(row, changed); audit(session, changed, E.REARMED, repo.clock(), work.status, claim.worker_id)
            return recovery

    def _block_recheck(self, session, case, row, work, claim):
        changed = work.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
        save_work(row, changed)
        audit(session, changed, E.BLOCKED, self.repository.clock(), work.status, claim.worker_id)
        escalate_in(session, case, R.EFFECT_UNRESOLVED, work.work_item_id, self.repository.clock())

    def runs(self, case_id):
        from sqlalchemy import select
        from credit_harness.agent.models import AgentRunResult
        ids = [w.work_item_id for w in self.repository.list(case_id)]
        with Session(self.repository.engine) as session:
            return tuple(AgentRunResult.model_validate(r.payload) for r in session.scalars(
                select(ResumedRunRow).where(ResumedRunRow.work_item_id.in_(ids))))
