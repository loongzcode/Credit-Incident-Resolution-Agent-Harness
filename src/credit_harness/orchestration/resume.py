from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.repository import hydrate
from credit_harness.context.budget import digest
from credit_harness.authorization.models import EffectStatus
from credit_harness.recovery.case import CaseRecoveryCoordinator
from .models import WorkStatus as S, WorkType as T, WorkReason as R, WorkEvent as E, OrchestrationError
from .repository import lock_case, state_in, save_state, save_work, audit, escalate_in, cancel_terminal_work


class CaseResumeService:
    """Only durable claimed work can wake a Case. No generic resume(case_id)."""
    def __init__(self, repository, evidence, *, effect_recovery=None):
        if evidence.cases is not repository.cases:
            raise ValueError("resume dependencies must share Case boundary")
        if effect_recovery is not None and effect_recovery.repository.cases is not repository.cases:
            raise ValueError("recovery must share Case boundary")
        self.repository, self.evidence = repository, evidence
        self.recovery = CaseRecoveryCoordinator(repository.cases, evidence)
        self.effect_recovery = effect_recovery

    def recover_before_resume(self, claim):
        repo = self.repository
        repo.renew(claim)
        item = repo.get(claim.work_item_id)
        inspection = self.recovery.before_investigation(item.case_id)
        blocked_prepared = False
        for effect_id in inspection.unresolved_effect_ids:
            repo.renew(claim)
            if self.effect_recovery is not None:
                self.effect_recovery.recover(effect_id)
            from credit_harness.authorization.store import SQLApprovalStore
            ledger = SQLApprovalStore(repo.cases).get_ledger(effect_id)
            blocked_prepared |= ledger.status == EffectStatus.PREPARED
        return blocked_prepared

    def resume(self, claim):
        repo = self.repository
        old = repo.get(claim.work_item_id)
        if old.status == S.COMPLETED:
            repo.complete(claim)  # Verify the completing owner's identity.
            return None
        blocked_prepared = self.recover_before_resume(claim)
        current_progress = knowledge_fingerprints(self.evidence.list(old.case_id))[1]
        now = repo.clock()
        with Session(repo.engine) as session, session.begin():
            case = lock_case(session, repo.cases, old.case_id)
            if CaseStatus(case.status).is_terminal:
                cancel_terminal_work(session, case.case_id, now); return None
            row, item = repo.owned(session, claim, now)
            if item.resumed_at:
                if item.resume_lease_token == claim.lease_token:
                    return None  # Same dispatch is idempotent, never a second run.
                # A lost worker may have started a run. Never dispatch it twice.
                changed = item.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
                save_work(row, changed); audit(session, changed, E.BLOCKED, now, item.status, claim.worker_id)
                escalate_in(session, case, R.INTERRUPTED_RUN, item.work_item_id, now)
                return None
            if (case.status != item.expected_case_status.value
                    or case.updated_at != item.expected_case_revision.isoformat()):
                changed = item.model_copy(update=dict(status=S.CANCELED, lease_until=None))
                save_work(row, changed); audit(session, changed, E.STALE, now, item.status, claim.worker_id)
                return None
            if (case.status == CaseStatus.ESCALATED.value and not item.signal_id
                    or item.required_signal and not item.signal_id or item.not_before > now):
                raise OrchestrationError("resume trigger not eligible")
            state_row, state = state_in(session, case)
            if state.last_progress_fingerprint is not None and state.last_progress_fingerprint != current_progress:
                state = state.model_copy(update=dict(no_progress_count=0))
                save_state(state_row, state)
            verification = item.work_type == T.VERIFICATION_REQUIRED
            reason = (R.EFFECT_UNRESOLVED if blocked_prepared else
                R.NO_PROGRESS if state.no_progress_count >= state.budget.max_no_progress_cycles else
                R.VERIFICATION_LIMIT if verification and state.verification_cycle_count >= state.budget.max_verification_cycles else
                R.RESUME_LIMIT if not verification and state.resume_cycle_count >= state.budget.max_resume_cycles else None)
            if reason:
                changed = item.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
                save_work(row, changed); audit(session, changed, E.BLOCKED, now, item.status, claim.worker_id)
                escalate_in(session, case, reason, item.work_item_id, now)
                return None
            # lock_case serializes the comparison and this update on SQLite and
            # PostgreSQL, also against all existing Case CAS writers.
            case.status = CaseStatus.INVESTIGATING.value
            case.updated_at = max(now, datetime.fromisoformat(case.updated_at)+timedelta(microseconds=1)).isoformat()
            case.payload = {**case.payload, "lookup_retry_after": now.isoformat()}
            changed = item.model_copy(update=dict(resumed_at=now, run_id=str(uuid4()), resume_lease_token=claim.lease_token))
            save_work(row, changed)
            save_state(state_row, state.model_copy(update=dict(
                resume_cycle_count=state.resume_cycle_count+(not verification),
                verification_cycle_count=state.verification_cycle_count+verification,
                last_work_item_id=item.work_item_id)))
            audit(session, changed, E.RESUMED, now, item.status, claim.worker_id)
            return changed

    def start_once(self, claim):
        repo = self.repository; old = repo.get(claim.work_item_id); now = repo.clock()
        with Session(repo.engine) as session, session.begin():
            case = lock_case(session, repo.cases, old.case_id)
            row, item = repo.owned(session, claim, now)
            if item.started_at or not item.resumed_at or CaseStatus(case.status).is_terminal:
                raise OrchestrationError("run already started or unavailable")
            changed = item.model_copy(update=dict(started_at=now))
            save_work(row, changed); audit(session, changed, E.STARTED, now, item.status, claim.worker_id)
            return changed


def knowledge_fingerprints(evidence, requirements=()):
    # Includes real new lookup evidence/history, never Case revision or budget.
    evidence_hash = digest(sorted(e.model_dump_json() for e in evidence))
    return evidence_hash, digest(dict(evidence=evidence_hash, requirements=sorted(requirements)))
