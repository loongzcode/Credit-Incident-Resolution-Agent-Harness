"""Effect-bound scheduling only. Step 9 owns recovery proofs and retry policy."""
from datetime import datetime, timezone
from sqlalchemy import select
from credit_harness.cases.models import CaseStatus
from credit_harness.authorization.models import SideEffectLedger
from credit_harness.authorization.tables import EffectRow
from credit_harness.recovery.tables import EffectRecoveryStateRow
from credit_harness.recovery.repository import RECOVERABLE
from .models import (WorkBindingMode as B, WorkType as T, WorkStatus as S,
    WorkReason as R, WorkEvent as E, Trigger, SignalType, OrchestrationError)
from .tables import WorkItemRow, OrchestrationAuditRow


def effect_binding(session, case, effect_id):
    row = session.get(EffectRow, effect_id)
    if row is None or row.case_id != case.case_id:
        raise OrchestrationError("effect binding unavailable")
    ledger = SideEffectLedger.model_validate(row.payload)
    state = session.get(EffectRecoveryStateRow, effect_id)
    if (ledger.effect_id != effect_id or ledger.case_id != case.case_id or ledger.tenant_id != case.tenant_id
            or state is None or state.effect_id != effect_id or state.case_id != case.case_id
            or state.tenant_id != case.tenant_id):
        raise OrchestrationError("effect recovery boundary mismatch")
    return ledger, state


def recovery_operator(session, case, work, now):
    from .repository import create_work, escalate_in
    if CaseStatus(case.status).is_terminal:
        return None
    if case.status != CaseStatus.ESCALATED.value:
        return escalate_in(session, case, R.EFFECT_UNRESOLVED, work.work_item_id, now)
    return create_work(session, case, work_type=T.OPERATOR_FOLLOWUP, reason=R.EFFECT_UNRESOLVED,
        source_ref=work.work_item_id, trigger=Trigger.RECOVERY, now=now,
        required_signal=SignalType.OPERATOR_ACKNOWLEDGED)


def check_recovery_work(session, case, row, work, now, *, settle=True):
    """Under Case lock; no comparison with the Work's old Case snapshot."""
    from .repository import save_work, audit, cancel_terminal_work
    if work.binding_mode != B.SIDE_EFFECT or work.case_id != case.case_id or work.tenant_id != case.tenant_id:
        raise OrchestrationError("invalid recovery work binding")
    if CaseStatus(case.status).is_terminal:
        cancel_terminal_work(session, case.case_id, now)
        return None
    try:
        ledger, state = effect_binding(session, case, work.source_ref)
    except OrchestrationError:
        changed = work.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
        save_work(row, changed); audit(session, changed, E.BLOCKED, now, work.status)
        recovery_operator(session, case, changed, now)
        return None
    if settle and ledger.status not in RECOVERABLE:
        if work.status != S.COMPLETED:
            changed = work.model_copy(update=dict(status=S.COMPLETED, completed_at=now, lease_until=None))
            save_work(row, changed); audit(session, changed, E.COMPLETED, now, work.status)
        from .handoff import effect_handoff
        effect_handoff(session, ledger)
        return None
    if settle and state.requires_escalation:
        if work.status != S.BLOCKED:
            changed = work.model_copy(update=dict(status=S.BLOCKED, lease_until=None))
            save_work(row, changed); audit(session, changed, E.BLOCKED, now, work.status)
        recovery_operator(session, case, work, now)
        return None
    return ledger, state


def existing_recovery_work(session, case, effect_id):
    return session.scalar(select(WorkItemRow).where(WorkItemRow.case_id == case.case_id,
        WorkItemRow.payload["work_type"].as_string() == T.RECOVERY_RECHECK.value,
        WorkItemRow.payload["source_ref"].as_string() == effect_id))


def ensure_recovery_work(session, case, effect_id, now, *, not_before=None, policy=None):
    """Callers hold the shared Case lock. Never rearm from status alone."""
    from .repository import _create_work, value, save_work, audit, ACTIVE
    row = existing_recovery_work(session, case, effect_id)
    if CaseStatus(case.status).is_terminal:
        if row:
            check_recovery_work(session, case, row, value(row), now)
            return value(row)
        return None
    ledger, state = effect_binding(session, case, effect_id)
    if row is None:
        if ledger.status not in RECOVERABLE:
            from .handoff import effect_handoff
            effect_handoff(session, ledger)
            return None
        item = _create_work(session, case, work_type=T.RECOVERY_RECHECK, reason=R.EFFECT_UNRESOLVED,
            source_ref=effect_id, trigger=Trigger.RECOVERY, now=now,
            not_before=max(not_before or now, datetime.fromtimestamp(
                max(state.next_eligible_at, state.lease_until or 0), timezone.utc)))
        row = session.get(WorkItemRow, item.work_item_id)
    work = value(row)
    # Do not steal a live Work lease from a worker completing its Step 9 call.
    if work.status == S.CLAIMED and work.lease_until and work.lease_until > now:
        return work
    if check_recovery_work(session, case, row, work, now) is None:
        return value(row)
    if work.status in ACTIVE:
        return work
    last = session.scalar(select(OrchestrationAuditRow).where(OrchestrationAuditRow.case_id == case.case_id,
        OrchestrationAuditRow.payload["work_item_id"].as_string() == work.work_item_id)
        .order_by(OrchestrationAuditRow.sequence.desc()).limit(1))
    # Without the configured Step 9 policy, only zero-attempt legacy work can
    # be proved below the limit. Normal active work always delegates to Step 9.
    below_limit = state.attempt_count < policy.max_attempts if policy is not None else state.attempt_count == 0
    if (work.status == S.CANCELED and last and last.payload["event"] == E.STALE.value
            and below_limit and not state.requires_escalation and state.lease_token is None):
        eligible = max(state.next_eligible_at, state.ledger_updated_at + (policy.grace_seconds if policy else 0))
        changed = work.model_copy(update=dict(status=S.PENDING, not_before=max(now, datetime.fromtimestamp(eligible, timezone.utc)),
            claimed_by=None, lease_token=None, lease_until=None, recovery_claim_token=None,
            progress_before=None, recovery_reads=(), recovery_result=None))
        save_work(row, changed); audit(session, changed, E.REARMED, now, work.status)
        return changed
    # BLOCKED, non-stale cancellations and anomalous COMPLETED/unresolved
    # records are never revived by a new evaluation report.
    return work


class EffectRecoveryWorkGuard:
    def __init__(self, repository):
        self.repository = repository

    def current(self, session, claim):
        from .repository import lock_case, cancel_terminal_work
        repo = self.repository
        item = repo.get(claim.work_item_id)
        case = lock_case(session, repo.cases, item.case_id)
        if CaseStatus(case.status).is_terminal:
            cancel_terminal_work(session, case.case_id, repo.clock())
            return None
        row, item = repo.owned(session, claim, repo.clock())
        if check_recovery_work(session, case, row, item, repo.clock(), settle=False) is None:
            return None
        return case, row, item
