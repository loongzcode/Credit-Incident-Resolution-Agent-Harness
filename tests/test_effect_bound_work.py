from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import pytest
from sqlalchemy.orm import Session
from tests.test_orchestration import factory, create, due
from tests.test_resume_recovery_integrity import recovering, tick
from tests.test_finality_routing import routed, consume_with_long_convergence_window
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.tables import CaseRow
from credit_harness.tools.contracts import ToolQuery
from credit_harness.domain.enums import ToolName as Tool
from credit_harness.evidence.models import ClaimType
from credit_harness.authorization.models import EffectStatus as ES
from credit_harness.authorization.tables import EffectRow
from credit_harness.recovery.models import LookupStatus as L
from credit_harness.recovery.tables import EffectRecoveryStateRow
from credit_harness.evaluation.models import VerificationRequirement as Q
from credit_harness.orchestration.models import (WorkBindingMode as B, WorkType as T, WorkStatus as S,
    WorkReason as R, WorkEvent as E, OrchestrationError)
from credit_harness.orchestration.effect_work import ensure_recovery_work
from credit_harness.orchestration.routing import RequirementRoute as Route, RecoveryRouteState
from credit_harness.orchestration.repository import lock_case, save_work, value, audit, ACTIVE
from credit_harness.orchestration.tables import WorkItemRow


def ensure(x, effect_id=None):
    with Session(x.engine) as session, session.begin():
        case = lock_case(session, x.cases, x.case.case_id)
        return ensure_recovery_work(session, case, effect_id or x.effect.effect_id, x.clock.now,
            policy=x.work.recovery_policy)


def legacy_cancel(x, *, event=E.STALE):
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        row = session.get(WorkItemRow, x.item.work_item_id); before = value(row)
        changed = before.model_copy(update=dict(status=S.CANCELED, lease_until=None))
        save_work(row, changed); audit(session, changed, event, x.clock.now, before.status)


def verify(x, tool_requirement):
    item = create(x, requirement=tool_requirement, source=f'verify-{tool_requirement}-{x.clock.now.timestamp()}')
    result = x.worker.process(due(x, item))
    assert result is not None
    assert x.work.get(item.work_item_id).status == S.COMPLETED
    return result


def test_recovery_work_survives_case_revision_change(factory):
    x = recovering(factory); tick(x)
    old = x.work.get(x.item.work_item_id)
    x.cases.pause(x.case.case_id, CaseStatus.WAITING)
    assert x.cases.get(x.case.case_id).updated_at != old.expected_case_revision
    tick(x)
    assert len(x.resolver.calls) == 2
    assert x.work.get(old.work_item_id).status == S.PENDING


def test_recovery_work_survives_payment_verification_during_backoff(factory):
    x = recovering(factory, max_attempts=4); tick(x)
    before = x.cases.get(x.case.case_id)
    verify(x, Q.PAYMENT_FINALITY)
    current = x.cases.get(x.case.case_id)
    assert current.updated_at > before.updated_at
    assert current.budget.used_tool_calls == before.budget.used_tool_calls + 1
    assert len(x.resolver.calls) == 1
    consume_with_long_convergence_window(x)
    assert ensure(x).work_item_id == x.item.work_item_id
    tick(x)
    assert len(x.resolver.calls) == 2
    assert x.work.get(x.item.work_item_id).status == S.PENDING


def test_recovery_work_survives_investigation_evidence_publication(factory):
    x = recovering(factory); tick(x)
    x.read(Tool.PAYMENT)
    tick(x)
    assert len(x.resolver.calls) == 2
    assert x.work.get(x.item.work_item_id).binding_mode == B.SIDE_EFFECT


def test_recovery_work_survives_wait_to_investigating_change(factory):
    x = recovering(factory); tick(x)
    x.cases.pause(x.case.case_id, CaseStatus.WAITING)
    verify(x, Q.PAYMENT_FINALITY)
    assert x.cases.get(x.case.case_id).status == CaseStatus.INVESTIGATING
    tick(x)
    assert len(x.resolver.calls) == 2


@pytest.mark.parametrize('status', [CaseStatus.CLOSED, CaseStatus.CLOSED_VERIFIED])
def test_terminal_case_cancels_recovery_work(factory, status):
    x = recovering(factory); tick(x)
    # Synthetic inconsistent terminal state tests the fence, not closure permission.
    with Session(x.engine) as session, session.begin():
        row = lock_case(session, x.cases, x.case.case_id); row.status = status.value
    x.advance(10)
    assert x.item.work_item_id not in x.work.poll_due_work()
    assert x.work.get(x.item.work_item_id).status == S.CANCELED
    assert x.work.claim(x.item.work_item_id, 'late') is None
    ensure(x)
    assert len(x.resolver.calls) == 1


def test_terminal_after_claim_blocks_recovery(factory):
    x = recovering(factory); x.advance(30)
    claim = x.work.claim(x.item.work_item_id, 'claimed')
    with Session(x.engine) as session, session.begin():
        row = lock_case(session, x.cases, x.case.case_id); row.status = CaseStatus.CLOSED_VERIFIED.value
    assert x.worker.process(claim) is None
    assert not x.resolver.calls
    assert x.work.get(x.item.work_item_id).status == S.CANCELED


def test_recovery_work_revalidates_current_effect_not_old_case_revision(factory):
    x = recovering(factory); tick(x); x.read(Tool.PAYMENT)
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        row = session.get(EffectRow, x.effect.effect_id)
        row.payload = {**row.payload, 'tenant_id': 'foreign-tenant'}
    x.advance(10)
    assert x.work.claim(x.item.work_item_id, 'worker') is None
    assert x.work.get(x.item.work_item_id).status == S.BLOCKED
    assert len(x.resolver.calls) == 1


def test_legacy_stale_canceled_recovery_work_can_rearm(factory):
    x = recovering(factory, max_attempts=4); tick(x); legacy_cancel(x)
    before = x.work.get(x.item.work_item_id)
    x.read(Tool.PAYMENT)
    resumed = ensure(x)
    assert resumed.status == S.PENDING and resumed.work_item_id == before.work_item_id
    assert resumed.expected_case_revision == before.expected_case_revision  # Diagnostic, not rebased.
    assert x.work.audit(x.case.case_id)[-1].event == E.REARMED
    tick(x)
    assert len(x.resolver.calls) == 2


def test_legacy_rearm_keeps_configured_step9_attempt_limit(factory):
    x = recovering(factory, max_attempts=4)
    for _ in range(3): tick(x)
    legacy_cancel(x)
    assert ensure(x).status == S.PENDING  # Custom max 4, not an invented default 3.
    tick(x)
    assert len(x.resolver.calls) == 4
    assert x.work.get(x.item.work_item_id).status == S.BLOCKED


def test_exhausted_blocked_recovery_work_cannot_rearm(factory):
    x = recovering(factory, max_attempts=1); tick(x)
    assert ensure(x).status == S.BLOCKED
    consume_with_long_convergence_window(x)
    assert ensure(x).status == S.BLOCKED
    assert x.work.claim(x.item.work_item_id, 'again') is None
    assert len(x.resolver.calls) == 1
    assert sum(w.reason_code == R.EFFECT_UNRESOLVED and w.work_type == T.OPERATOR_FOLLOWUP
        for w in x.work.list(x.case.case_id)) == 1


def test_nonstale_canceled_work_is_not_revived(factory):
    x = recovering(factory); legacy_cancel(x, event=E.CANCELED)
    assert ensure(x).status == S.CANCELED


def test_legacy_canceled_at_attempt_limit_is_not_rearmed(factory):
    x = recovering(factory, max_attempts=1); tick(x); legacy_cancel(x)
    assert ensure(x).status == S.BLOCKED
    assert len(x.resolver.calls) == 1


def test_legacy_stale_rearm_is_atomic(factory, monkeypatch):
    x = recovering(factory); tick(x); legacy_cancel(x)
    from credit_harness.orchestration import repository
    original = repository.audit
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        if args[2] == E.REARMED: raise RuntimeError('synthetic rearm crash')
    monkeypatch.setattr(repository, 'audit', crash)
    with pytest.raises(RuntimeError): ensure(x)
    assert x.work.get(x.item.work_item_id).status == S.CANCELED


def test_one_effect_still_has_one_active_recovery_work(factory):
    x = recovering(factory); tick(x); legacy_cancel(x)
    for _ in range(3): ensure(x)
    assert sum(w.work_type == T.RECOVERY_RECHECK and w.status in ACTIVE for w in x.work.list(x.case.case_id)) == 1


def test_stale_effect_finality_report_after_applied_is_noop(factory):
    x = recovering(factory); report = x.evaluate()
    x.advance(30); x.resolver.status = L.FOUND_APPLIED
    x.effect_recovery.recover(x.effect.effect_id)
    items = x.worker.handoff.consume(report)
    assert not any(w.work_type == T.OPERATOR_FOLLOWUP and w.reason_code == R.EFFECT_UNRESOLVED for w in items)
    assert routed(Q.EFFECT_FINALITY, effect_ref=x.effect.effect_id, effects=(
        RecoveryRouteState(effect_ref=x.effect.effect_id, status=ES.APPLIED, recovery_available=True,
            requires_escalation=False),))[0].route == Route.NO_ACTION


def test_stale_recovery_finality_report_after_resolution_is_noop(factory):
    x = recovering(factory); report = x.evaluate()
    x.advance(30); x.resolver.status = L.FOUND_FAILED_NO_EFFECT
    x.effect_recovery.recover(x.effect.effect_id)
    items = x.worker.handoff.consume(report)
    assert not any(w.work_type == T.RECOVERY_RECHECK or w.reason_code == R.EFFECT_UNRESOLVED for w in items)
    assert routed(Q.RECOVERY_FINALITY)[0].route == Route.NO_ACTION


def test_missing_effect_ref_is_not_silently_noop(factory):
    x = recovering(factory)
    assert routed(Q.EFFECT_FINALITY, effect_ref='f'*64)[0].route == Route.OPERATOR_FOLLOWUP
    with pytest.raises(OrchestrationError): ensure(x, 'f'*64)


def test_foreign_effect_ref_is_not_silently_noop(factory):
    x = recovering(factory); other = factory(case_id='OTHER-CASE')
    with Session(x.engine) as session, session.begin():
        case = lock_case(session, other.cases, other.case.case_id)
        with pytest.raises(OrchestrationError):
            ensure_recovery_work(session, case, x.effect.effect_id, x.clock.now)


def test_current_final_effect_completes_recovery_work_without_new_lookup(factory):
    x = recovering(factory); x.advance(30); x.resolver.status = L.FOUND_APPLIED
    x.effect_recovery.recover(x.effect.effect_id)
    assert x.item.work_item_id not in x.work.poll_due_work()
    assert ensure(x).status == S.COMPLETED
    assert len(x.resolver.calls) == 1


def test_effect_binding_mode_cannot_be_overridden_or_resume_agent(factory):
    from pydantic import ValidationError
    x = recovering(factory)
    with pytest.raises(ValidationError):
        type(x.item).model_validate({**x.item.model_dump(), 'binding_mode': 'CASE_SNAPSHOT'})
    assert x.item.binding_mode == B.SIDE_EFFECT
    x.advance(30); claim = x.work.claim(x.item.work_item_id, 'worker')
    with pytest.raises(OrchestrationError): x.worker.resume_service.resume(claim)
    assert not x.worker.runs(x.case.case_id)


def test_missing_step9_state_blocks_recovery_claim(factory):
    x = recovering(factory)
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        session.delete(session.get(EffectRecoveryStateRow, x.effect.effect_id))
    x.advance(30)
    assert x.work.claim(x.item.work_item_id, 'worker') is None
    assert x.work.get(x.item.work_item_id).status == S.BLOCKED
    assert not x.resolver.calls


def test_step9_bootstrap_can_atomically_register_effect_bound_work(factory):
    from credit_harness.recovery.tables import create_recovery_schema
    x = recovering(factory)
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        session.delete(session.get(EffectRecoveryStateRow, x.effect.effect_id))
        session.delete(session.get(WorkItemRow, x.item.work_item_id))
    create_recovery_schema(x.engine)  # Existing Step 9 bootstrap remains unchanged.
    assert ensure(x).binding_mode == B.SIDE_EFFECT
    tick(x)
    assert len(x.resolver.calls) == 1


def test_step9_bootstrap_reuses_existing_recovery_descriptor(factory):
    from credit_harness.recovery.tables import create_recovery_schema
    x = recovering(factory)
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        session.delete(session.get(EffectRecoveryStateRow, x.effect.effect_id))
    create_recovery_schema(x.engine)
    assert ensure(x).work_item_id == x.item.work_item_id
    assert sum(w.work_type == T.RECOVERY_RECHECK for w in x.work.list(x.case.case_id)) == 1


def test_effect_bound_work_still_rejects_old_work_lease(factory):
    x = recovering(factory); x.advance(30)
    old = x.work.claim(x.item.work_item_id, 'old'); x.advance(121)
    current = x.work.claim(x.item.work_item_id, 'current')
    with pytest.raises(OrchestrationError): x.worker.process(old)
    assert not x.resolver.calls
    x.worker.process(current)
    assert len(x.resolver.calls) == 1


def test_snapshot_work_still_stales_on_new_evidence(factory):
    x = recovering(factory); item = create(x, requirement=Q.PAYMENT_FINALITY)
    x.read(Tool.PAYMENT)
    x.work.poll_due_work()
    assert x.work.get(item.work_item_id).status == S.CANCELED
    assert x.work.get(x.item.work_item_id).status in ACTIVE


def test_unknown_recovery_survives_two_verifications_then_applied(factory):
    x = recovering(factory, max_attempts=4); tick(x)
    verify(x, Q.PAYMENT_FINALITY)
    tick(x)
    verify(x, Q.ACCOUNTING_ENTRY)
    x.resolver.status = L.FOUND_APPLIED
    tick(x)
    assert x.work.get(x.item.work_item_id).status == S.COMPLETED
    item = next(w for w in x.work.list(x.case.case_id) if w.reason_code == R.EFFECT_APPLIED)
    assert x.worker.process(due(x, item)) is not None
    assert any(e.claim_type == ClaimType.MESSAGE_CONSUME_STATUS and e.value == 'CONSUMED'
        for e in x.evidence.list(x.case.case_id))
    assert len(x.resolver.calls) == 3
    assert x.runtime.store.get_ledger(x.effect.effect_id).attempt_count == 1
    assert not any(a.event == E.STALE and a.work_item_id == x.item.work_item_id for a in x.work.audit(x.case.case_id))


def test_concurrent_publication_and_evaluation_keep_one_effect_bound_work(factory):
    x = recovering(factory, max_attempts=4); tick(x); x.advance(1)
    gate = Barrier(3)
    def concurrent(i):
        gate.wait()
        if i == 0:
            return x.executor.execute(x.case.case_id, Tool.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))
        if i == 1:
            try: return consume_with_long_convergence_window(x)
            except OrchestrationError: return None  # A report cut before publication remains correctly stale.
        return ensure(x)
    with ThreadPoolExecutor(3) as pool: list(pool.map(concurrent, range(3)))
    consume_with_long_convergence_window(x)
    active = [w for w in x.work.list(x.case.case_id) if w.work_type == T.RECOVERY_RECHECK and w.status in ACTIVE]
    assert len(active) == 1 and active[0].work_item_id == x.item.work_item_id
    tick(x)
    assert len(x.resolver.calls) == 2
    assert x.runtime.store.get_ledger(x.effect.effect_id).attempt_count == 1
