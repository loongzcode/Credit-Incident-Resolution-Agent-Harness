from datetime import datetime, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.repository import hydrate
from credit_harness.context.budget import digest
from credit_harness.authorization.models import EffectStatus
from credit_harness.recovery.case import CaseRecoveryCoordinator
from .models import WorkStatus as S, WorkType as T, WorkReason as R, WorkEvent as E, OrchestrationError, ResumeRecoveryResult, WorkBindingMode as B
from .repository import lock_case, state_in, save_state, save_work, audit, escalate_in, cancel_terminal_work


class CaseResumeService:
    """Only durable claimed work can wake a Case. No generic resume(case_id)."""
    def __init__(self, repository, evidence, *, effect_recovery=None, evaluator=None):
        if evidence.cases is not repository.cases:
            raise ValueError("resume dependencies must share Case boundary")
        if effect_recovery is not None and effect_recovery.repository.cases is not repository.cases:
            raise ValueError("recovery must share Case boundary")
        self.repository, self.evidence = repository, evidence
        self.recovery = CaseRecoveryCoordinator(repository.cases, evidence)
        self.effect_recovery = effect_recovery
        from credit_harness.evaluation.evaluator import IndependentEvaluator
        self.evaluator = evaluator or IndependentEvaluator(repository.cases, clock=repository.clock)
        if self.evaluator.cases is not repository.cases:
            raise ValueError("progress evaluator must share Case boundary")
        from .effect_work import EffectRecoveryWorkGuard
        self.effect_guard = EffectRecoveryWorkGuard(repository)

    @property
    def effect_recovery(self):
        return self._effect_recovery

    @effect_recovery.setter
    def effect_recovery(self, coordinator):
        if coordinator is not None:
            if coordinator.repository.cases is not self.repository.cases:
                raise ValueError("recovery must share Case boundary")
            # Trusted deployment wiring; no per-call retry limit override.
            self.repository.recovery_policy = coordinator.repository.policy
        self._effect_recovery = coordinator

    def _current(self, session, claim):
        repo = self.repository
        item = repo.get(claim.work_item_id)
        case = lock_case(session, repo.cases, item.case_id)
        row, item = repo.owned(session, claim, repo.clock())
        if item.binding_mode != B.CASE_SNAPSHOT:
            raise OrchestrationError("snapshot guard cannot validate effect work")
        if CaseStatus(case.status).is_terminal:
            raise OrchestrationError("terminal case")
        if item.resumed_at:
            if item.resume_lease_token != claim.lease_token:
                changed = item.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
                save_work(row, changed); audit(session, changed, E.BLOCKED, repo.clock(), item.status, claim.worker_id)
                escalate_in(session, case, R.INTERRUPTED_RUN, item.work_item_id, repo.clock())
            return None
        if (case.status != item.expected_case_status.value
                or case.updated_at != item.expected_case_revision.isoformat()):
            changed = item.model_copy(update=dict(status=S.CANCELED, lease_until=None))
            save_work(row, changed); audit(session, changed, E.STALE, repo.clock(), item.status, claim.worker_id)
            return None
        return case, row, item

    def progress(self, case_id, report=None):
        from .progress import capture_progress
        return capture_progress(self.repository.cases, self.evidence, case_id, self.evaluator, report)

    def recover_before_resume(self, claim) -> ResumeRecoveryResult | None:
        repo = self.repository
        if repo.get(claim.work_item_id).binding_mode == B.SIDE_EFFECT:
            return self.recover_effect_work(claim)
        repo.renew(claim)
        with Session(repo.engine) as session, session.begin():
            current = self._current(session, claim)
            if current is None:
                return None
            case, row, item = current
            if item.recovery_claim_token == claim.lease_token:
                return None  # One recovery invocation per claim, even with concurrent delivery.
            save_work(row, item.model_copy(update=dict(recovery_claim_token=claim.lease_token)))
            before_revision = item.expected_case_revision
        before = item.progress_before or self.progress(item.case_id)
        with Session(repo.engine) as session, session.begin():
            current = self._current(session, claim)
            if current is None:
                return None
            _, row, item = current
            if item.progress_before is None:
                save_work(row, item.model_copy(update=dict(progress_before=before,
                    before_progress_fingerprint=before.fingerprint,
                    before_evidence_fingerprint=before.evidence_fingerprint)))
        reads = []
        reader = self.recovery.read_recovery
        for call_id in reader.pending_calls(item.case_id):
            # Own publication and rebase are atomic. No arbitrary new revision
            # is accepted, including another mutation between recovered calls.
            try:
                reads.append(reader.recover_if_owned(item.case_id, call_id, repo, claim))
            except OrchestrationError:
                with Session(repo.engine) as session, session.begin():
                    if self._current(session, claim) is None:
                        return None
                raise
        from sqlalchemy import select, inspect
        from credit_harness.authorization.tables import EffectRow
        from credit_harness.authorization.models import SideEffectLedger
        from credit_harness.recovery.repository import RECOVERABLE
        with Session(repo.engine) as session, session.begin():
            current = self._current(session, claim)
            if current is None:
                return None
            _, _, item = current
            connection = session.connection()
            schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
            ledgers = tuple(SideEffectLedger.model_validate(r.payload) for r in session.scalars(
                select(EffectRow).where(EffectRow.case_id == item.case_id).order_by(EffectRow.effect_id))) if inspect(
                    connection).has_table(EffectRow.__tablename__, schema=schema) else ()
        unresolved = [e for e in ledgers if e.status in RECOVERABLE]
        target = next(iter(unresolved), None)
        effects = []
        # One effect recovery invocation per orchestration tick, never a loop.
        if target is not None and self.effect_recovery is not None:
            repo.renew(claim)
            effects.append(self.effect_recovery.recover(target.effect_id))
        from credit_harness.authorization.store import SQLApprovalStore
        blocked = any(SQLApprovalStore(repo.cases).get_ledger(e.effect_id).status == EffectStatus.PREPARED
                      for e in unresolved)
        after = self.progress(item.case_id)
        with Session(repo.engine) as session, session.begin():
            current = self._current(session, claim)
            if current is None:
                return None
            case, row, item = current
            durable_reads = item.recovery_reads
            result = ResumeRecoveryResult(case_id=item.case_id, work_item_id=item.work_item_id,
                before_case_revision=before_revision, after_case_revision=case.updated_at,
                read_recoveries=tuple(reads) or durable_reads,
                recovered_evidence_refs=tuple(sorted({r for receipt in durable_reads for r in receipt.recovered_evidence_refs})),
                effect_recoveries=tuple(effects), semantic_progress=before.fingerprint != after.fingerprint,
                prepared_blocked=blocked)
            save_work(row, item.model_copy(update=dict(recovery_result=result)))
            return result

    def recover_effect_work(self, claim) -> ResumeRecoveryResult | None:
        """One bound Step 9 call. No Case resume, Agent Run or read publication."""
        from .effect_work import effect_binding
        from credit_harness.recovery.repository import RECOVERABLE
        repo = self.repository
        with Session(repo.engine) as session, session.begin():
            current = self.effect_guard.current(session, claim)
            if current is None:
                return None
            case, row, item = current
            if item.recovery_claim_token == claim.lease_token:
                return None
            save_work(row, item.model_copy(update=dict(recovery_claim_token=claim.lease_token)))
            before_revision = case.updated_at
        before = item.progress_before or self.progress(item.case_id)
        with Session(repo.engine) as session, session.begin():
            current = self.effect_guard.current(session, claim)
            if current is None:
                return None
            case, row, item = current
            if item.progress_before is None:
                save_work(row, item.model_copy(update=dict(progress_before=before,
                    before_progress_fingerprint=before.fingerprint, before_evidence_fingerprint=before.evidence_fingerprint)))
            ledger, state = effect_binding(session, case, item.source_ref)
            requires_escalation = state.requires_escalation
        effects = ()
        if ledger.status in RECOVERABLE and not requires_escalation and self.effect_recovery is not None:
            repo.renew(claim)
            # Step 9 rechecks its own current state, lease, capability contract,
            # grace/backoff and attempt limit. No proof is authored here.
            effects = (self.effect_recovery.recover(ledger.effect_id),)
        after = self.progress(item.case_id)
        with Session(repo.engine) as session, session.begin():
            current = self.effect_guard.current(session, claim)
            if current is None:
                return None
            case, row, item = current
            ledger, _ = effect_binding(session, case, item.source_ref)
            result = ResumeRecoveryResult(case_id=item.case_id, work_item_id=item.work_item_id,
                before_case_revision=before_revision, after_case_revision=case.updated_at,
                effect_recoveries=effects, semantic_progress=before.fingerprint != after.fingerprint,
                prepared_blocked=ledger.status == EffectStatus.PREPARED)
            save_work(row, item.model_copy(update=dict(recovery_result=result)))
            return result

    def resume(self, claim):
        repo = self.repository
        old = repo.get(claim.work_item_id)
        if old.binding_mode != B.CASE_SNAPSHOT:
            raise OrchestrationError("effect lookup cannot resume investigation")
        if old.status == S.COMPLETED:
            repo.complete(claim)  # Verify the completing owner's identity.
            return None
        recovery = self.recover_before_resume(claim)
        if recovery is None:
            return None
        current_progress = self.progress(old.case_id).fingerprint
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
            if recovery.semantic_progress or (state.last_progress_version == "2" and state.last_progress_fingerprint is not None
                    and state.last_progress_fingerprint != current_progress):
                state = state.model_copy(update=dict(no_progress_count=0))
                save_state(state_row, state)
            verification = item.work_type == T.VERIFICATION_REQUIRED
            reason = (R.EFFECT_UNRESOLVED if recovery.prepared_blocked else
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
