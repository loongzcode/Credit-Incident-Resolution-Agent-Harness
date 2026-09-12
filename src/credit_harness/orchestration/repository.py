from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import select, update, or_
from sqlalchemy.orm import Session
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.tables import CaseRow
from credit_harness.cases.repository import hydrate, utc_now
from credit_harness.context.budget import digest
from .models import (CaseWorkItem, CaseOrchestrationState, OrchestrationBudget, OrchestrationError,
    WorkStatus as S, WorkType as T, WorkReason as R, Trigger, WorkEvent as E,
    WorkClaim, ResolutionSignal, SignalType, OrchestrationAuditRecord)
from .tables import WorkItemRow, OrchestrationStateRow, SignalRow, OrchestrationAuditRow

ACTIVE = (S.PENDING, S.READY, S.CLAIMED)


def lock_case(session, cases, case_id):
    result = session.execute(update(CaseRow).where(CaseRow.case_id == case_id,
        CaseRow.tenant_id == cases.tenant_id).values(updated_at=CaseRow.updated_at))
    if result.rowcount != 1:
        raise OrchestrationError("case unavailable")
    row = cases._row(session, case_id)
    session.refresh(row)
    return row


def state_in(session, case):
    row = session.get(OrchestrationStateRow, case.case_id)
    if row is None:
        value = CaseOrchestrationState(case_id=case.case_id, tenant_id=case.tenant_id)
        row = OrchestrationStateRow(case_id=case.case_id, payload=value.model_dump(mode="json"))
        session.add(row); session.flush()
    return row, CaseOrchestrationState.model_validate(row.payload)


def save_state(row, state):
    row.payload = state.model_dump(mode="json")


def value(row):
    return CaseWorkItem.model_validate(row.payload)


def audit(session, item, event, now, before=None, worker=None):
    record = OrchestrationAuditRecord(work_item_id=item.work_item_id, case_id=item.case_id,
        event=event, before_status=before, after_status=item.status, worker_id=worker,
        trigger_ref=item.signal_id or item.source_ref, recorded_at=now)
    session.add(OrchestrationAuditRow(case_id=item.case_id, payload=record.model_dump(mode="json")))


def save_work(row, item):
    row.payload = item.model_dump(mode="json")
    row.status, row.not_before = item.status.value, item.not_before.timestamp()
    row.lease_until = item.lease_until.timestamp() if item.lease_until else None


def create_work(session, case, *, work_type, reason, source_ref, trigger, now,
                not_before=None, requirement=None, required_signal=None, previous_run_id=None,
                snapshot_ref=None, wait_seconds=None, verification_requirements=()):
    key = digest(dict(case=case.case_id, tenant=case.tenant_id, type=work_type,
        reason=reason, source=source_ref, requirement=requirement, snapshot=snapshot_ref))
    old = session.get(WorkItemRow, key)
    if old:
        return value(old)
    state_row, state = state_in(session, case)
    status = S.CANCELED if CaseStatus(case.status).is_terminal else S.PENDING
    if case.status == CaseStatus.ESCALATED.value and required_signal is None:
        # An effect receipt or an evaluation report cannot bypass an escalation.
        required_signal = SignalType.OPERATOR_ACKNOWLEDGED
    if case.status == CaseStatus.WAITING.value and work_type == T.VERIFICATION_REQUIRED:
        timers = [value(r) for r in session.scalars(select(WorkItemRow).where(
            WorkItemRow.case_id == case.case_id, WorkItemRow.status.in_([S.PENDING.value, S.READY.value])))
            if value(r).reason_code == R.WAIT_REQUESTED]
        not_before = max([not_before or now, *(w.not_before for w in timers)])
    item = CaseWorkItem(work_item_id=key, tenant_id=case.tenant_id, case_id=case.case_id,
        work_type=work_type, status=status, reason_code=reason, source_ref=source_ref, trigger=trigger,
        expected_case_status=case.status, expected_case_revision=case.updated_at,
        created_at=now, not_before=not_before or now, required_signal=required_signal,
        previous_run_id=previous_run_id, snapshot_ref=snapshot_ref, wait_seconds=wait_seconds,
        requirement=requirement, verification_requirements=verification_requirements)
    session.add(WorkItemRow(work_item_id=key, case_id=case.case_id, tenant_id=case.tenant_id,
        status=status.value, not_before=item.not_before.timestamp(), payload=item.model_dump(mode="json")))
    save_state(state_row, state.model_copy(update=dict(last_work_item_id=key)))
    audit(session, item, E.CREATED, now)
    session.flush()
    return item


def cancel_terminal_work(session, case_id, now):
    for row in session.scalars(select(WorkItemRow).where(WorkItemRow.case_id == case_id,
            WorkItemRow.status.in_([s.value for s in (*ACTIVE, S.BLOCKED)]))):
        old = value(row)
        item = old.model_copy(update=dict(status=S.CANCELED, lease_until=None))
        save_work(row, item); audit(session, item, E.CANCELED, now, old.status)


def escalate_in(session, case, reason, source_ref, now):
    if CaseStatus(case.status).is_terminal:
        return None
    previous = datetime.fromisoformat(case.updated_at)
    case.status = CaseStatus.ESCALATED.value
    case.updated_at = max(now, previous + timedelta(microseconds=1)).isoformat()
    session.flush()
    return create_work(session, case, work_type=T.OPERATOR_FOLLOWUP, reason=reason,
        source_ref=source_ref, trigger=Trigger.RECOVERY, now=now,
        required_signal=SignalType.OPERATOR_ACKNOWLEDGED)


class WorkRepository:
    """Trusted tenant-scoped repository. All writers lock Case before Work."""
    def __init__(self, cases, *, clock=utc_now, lease_seconds=120):
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("bounded lease required")
        self.cases, self.engine, self.clock = cases, cases.engine, clock
        self.lease_seconds = lease_seconds

    def _row(self, session, work_id):
        row = session.scalar(select(WorkItemRow).where(WorkItemRow.work_item_id == work_id,
            WorkItemRow.tenant_id == self.cases.tenant_id))
        if row is None:
            raise OrchestrationError("work unavailable")
        return row

    def get(self, work_id):
        with Session(self.engine) as session:
            return value(self._row(session, work_id))

    def list(self, case_id):
        self.cases.get(case_id)
        with Session(self.engine) as session:
            return tuple(value(r) for r in session.scalars(select(WorkItemRow).where(
                WorkItemRow.case_id == case_id, WorkItemRow.tenant_id == self.cases.tenant_id)
                .order_by(WorkItemRow.not_before, WorkItemRow.work_item_id)))

    def state(self, case_id):
        with Session(self.engine) as session, session.begin():
            case = lock_case(session, self.cases, case_id)
            _, state = state_in(session, case)
            count = len(list(session.scalars(select(WorkItemRow.work_item_id).where(
                WorkItemRow.case_id == case_id, WorkItemRow.status.in_([s.value for s in ACTIVE])))))
            return state.model_copy(update=dict(pending_work_count=count))

    def configure(self, case_id, budget: OrchestrationBudget):
        budget = OrchestrationBudget.model_validate(budget.model_dump())
        with Session(self.engine) as session, session.begin():
            case = lock_case(session, self.cases, case_id)
            row, state = state_in(session, case)
            if state.resume_cycle_count or state.verification_cycle_count:
                raise OrchestrationError("budget cannot be reset after work starts")
            save_state(row, state.model_copy(update=dict(budget=budget)))

    def poll_due_work(self, now=None, *, limit=100):
        now = now or self.clock()
        if not 1 <= limit <= 1000:
            raise ValueError("bounded scan required")
        with Session(self.engine) as session:
            ids = tuple(session.scalars(select(WorkItemRow.work_item_id).where(
                WorkItemRow.tenant_id == self.cases.tenant_id,
                or_(WorkItemRow.status.in_([S.PENDING.value, S.READY.value]) &
                    or_(WorkItemRow.payload["required_signal"].as_string().is_(None),
                        WorkItemRow.payload["signal_id"].as_string().is_not(None)),
                    (WorkItemRow.status == S.CLAIMED.value) & (WorkItemRow.lease_until <= now.timestamp())),
                WorkItemRow.not_before <= now.timestamp()).order_by(WorkItemRow.not_before,
                    WorkItemRow.work_item_id).limit(limit)))
        ready = []
        for work_id in ids:
            item = self.get(work_id)
            with Session(self.engine) as session, session.begin():
                case = lock_case(session, self.cases, item.case_id)
                row = self._row(session, work_id); session.refresh(row); item = value(row)
                if CaseStatus(case.status).is_terminal:
                    cancel_terminal_work(session, case.case_id, now); continue
                if item.status in (S.PENDING, S.READY) and (
                        case.status != item.expected_case_status.value
                        or case.updated_at != item.expected_case_revision.isoformat()):
                    changed = item.model_copy(update=dict(status=S.CANCELED, lease_until=None))
                    save_work(row, changed); audit(session, changed, E.STALE, now, item.status)
                    continue
                if item.status in (S.PENDING, S.READY) and (not item.required_signal or item.signal_id):
                    if item.status == S.PENDING:
                        changed = item.model_copy(update=dict(status=S.READY))
                        save_work(row, changed); audit(session, changed, E.READY, now, item.status)
                    ready.append(work_id)
                elif item.status == S.CLAIMED and item.lease_until <= now:
                    ready.append(work_id)
        return tuple(ready)

    def claim_next(self, worker_id, now=None):
        for work_id in self.poll_due_work(now):
            if claim := self.claim(work_id, worker_id, now):
                return claim
        return None

    def claim(self, work_id, worker_id, now=None):
        now = now or self.clock()
        item = self.get(work_id)
        with Session(self.engine) as session, session.begin():
            case = lock_case(session, self.cases, item.case_id)
            row = self._row(session, work_id); session.refresh(row); item = value(row)
            if CaseStatus(case.status).is_terminal:
                cancel_terminal_work(session, case.case_id, now); return None
            if (item.status not in ACTIVE or item.not_before > now
                    or (item.required_signal and not item.signal_id)
                    or (item.lease_until and item.lease_until > now)):
                return None
            other = session.scalar(select(WorkItemRow).where(WorkItemRow.case_id == item.case_id,
                WorkItemRow.work_item_id != work_id, WorkItemRow.status == S.CLAIMED.value))
            if other is not None:
                return None  # One in-flight cycle per Case, including expired runs awaiting recovery.
            claim = WorkClaim(work_item_id=work_id, worker_id=worker_id, lease_token=str(uuid4()))
            changed = item.model_copy(update=dict(status=S.CLAIMED, claimed_by=worker_id,
                lease_token=claim.lease_token, lease_until=now+timedelta(seconds=self.lease_seconds),
                attempt_count=item.attempt_count+1))
            save_work(row, changed); audit(session, changed, E.CLAIMED, now, item.status, worker_id)
            return claim

    def owned(self, session, claim, now):
        if type(claim) is not WorkClaim:
            raise OrchestrationError("typed claim required")
        row = self._row(session, claim.work_item_id); session.refresh(row); item = value(row)
        if (item.status != S.CLAIMED or item.lease_token != claim.lease_token
                or item.claimed_by != claim.worker_id or not item.lease_until or item.lease_until <= now):
            raise OrchestrationError("lease lost")
        return row, item

    def renew(self, claim):
        item = self.get(claim.work_item_id); now = self.clock()
        with Session(self.engine) as session, session.begin():
            case = lock_case(session, self.cases, item.case_id)
            if CaseStatus(case.status).is_terminal:
                raise OrchestrationError("terminal case")
            row, item = self.owned(session, claim, now)
            save_work(row, item.model_copy(update=dict(lease_until=now+timedelta(seconds=self.lease_seconds))))
        from credit_harness.cases.preconditions import WorkLeasePrecondition
        return WorkLeasePrecondition(work_item_id=claim.work_item_id, worker_id=claim.worker_id,
            lease_token=claim.lease_token)

    def complete(self, claim):
        old = self.get(claim.work_item_id); now = self.clock()
        with Session(self.engine) as session, session.begin():
            lock_case(session, self.cases, old.case_id)
            row = self._row(session, claim.work_item_id); session.refresh(row); item = value(row)
            if (item.status == S.COMPLETED and item.lease_token == claim.lease_token
                    and item.claimed_by == claim.worker_id):
                return item
            row, item = self.owned(session, claim, now)
            result = item.model_copy(update=dict(status=S.COMPLETED, completed_at=now, lease_until=None))
            save_work(row, result); audit(session, result, E.COMPLETED, now, item.status, claim.worker_id)
            return result

    def submit_signal(self, signal):
        if type(signal) is not ResolutionSignal:
            raise OrchestrationError("typed resolution signal required")
        signal = ResolutionSignal.model_validate(signal.model_dump())
        if signal.tenant_id != self.cases.tenant_id:
            raise OrchestrationError("signal unavailable")
        with Session(self.engine) as session, session.begin():
            case = lock_case(session, self.cases, signal.case_id)
            row = self._row(session, signal.work_item_id); item = value(row)
            if (item.case_id != case.case_id or CaseStatus(case.status).is_terminal
                    or signal.created_at > self.clock() or signal.created_at < item.created_at
                    or item.status not in (S.PENDING, S.READY)
                    or item.required_signal != signal.signal_type
                    or signal.subject != hydrate(case).internal_order_id
                    or case.status != item.expected_case_status.value
                    or case.updated_at != item.expected_case_revision.isoformat()):
                raise OrchestrationError("signal does not match current escalation")
            old = session.get(SignalRow, signal.signal_id)
            if old and old.payload != signal.model_dump(mode="json"):
                raise OrchestrationError("signal identity conflict")
            if item.signal_id and item.signal_id != signal.signal_id:
                raise OrchestrationError("signal already bound")
            if not old:
                session.add(SignalRow(signal_id=signal.signal_id, tenant_id=signal.tenant_id,
                    case_id=signal.case_id, payload=signal.model_dump(mode="json")))
            changed = item.model_copy(update=dict(signal_id=signal.signal_id, trigger=Trigger.RESOLUTION_SIGNAL))
            save_work(row, changed)
            return changed

    def audit(self, case_id):
        self.cases.get(case_id)
        with Session(self.engine) as session:
            return tuple(OrchestrationAuditRecord.model_validate(r.payload) for r in session.scalars(
                select(OrchestrationAuditRow).where(OrchestrationAuditRow.case_id == case_id)
                .order_by(OrchestrationAuditRow.sequence)))
