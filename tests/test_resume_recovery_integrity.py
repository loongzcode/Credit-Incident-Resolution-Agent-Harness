from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_orchestration import factory, wait, due, create, configure_recovery
from tests.test_recovery import FakeResolver
from credit_harness.domain.enums import ScenarioId, ToolName as Tool
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.tables import CaseRow, CaseCallRow
from credit_harness.tools.contracts import ToolQuery
from credit_harness.persistence.store import ObservationRow
from credit_harness.evidence.models import ClaimType
from credit_harness.recovery.models import LookupStatus as L, RecoveryPolicy, DispatchRecoveryStatus as D
from credit_harness.recovery.repository import RecoveryRepository
from credit_harness.recovery.service import SideEffectRecoveryCoordinator
from credit_harness.recovery.tables import EffectRecoveryStateRow, ReadDispatchRecoveryRow
from credit_harness.authorization.models import EffectStatus as ES
from credit_harness.orchestration.models import *
from credit_harness.orchestration.repository import lock_case, state_in, save_state
from credit_harness.orchestration.progress import progress_fingerprint
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.evaluation.models import EvaluationVerdict as V, VerificationRequirement as Q


def orphan_wait(factory):
    x = factory()
    query = ToolQuery(internal_order_id=x.case.internal_order_id)
    _, call_id = x.cases.reserve_call(x.case.case_id, Tool.PAYMENT, query)
    first, work = wait(x)
    observation = x.client.observe(Tool.PAYMENT, query, dispatch_correlation_id=call_id)
    claim = due(x, work)
    return x, work, claim, call_id, observation


def recover_read(x, claim, call_id):
    return x.worker.resume_service.recovery.read_recovery.recover_if_owned(x.case.case_id, call_id, x.work, claim)


def test_orphan_read_recovery_during_wait_resume_does_not_self_stale(factory):
    x, work, claim, call_id, observation = orphan_wait(factory)
    result = x.worker.process(claim)
    assert result and result.tool_calls_dispatched == 0
    saved = x.work.get(work.work_item_id)
    assert saved.status == WorkStatus.COMPLETED
    assert saved.recovery_result.read_recoveries[0].action_taken == D.OBSERVATION_RECOVERED
    assert saved.recovery_result.after_case_revision > saved.recovery_result.before_case_revision
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 1
    assert all(e.observation_id == observation.observation_id for e in x.evidence.list(x.case.case_id))
    assert not any(e.claim_type == ClaimType.PAYMENT_FINALITY and e.value in ('FAILED','SETTLED','NOT_EXECUTED')
                   for e in x.evidence.list(x.case.case_id))


def test_resume_recovery_rebase_is_atomic(factory, monkeypatch):
    from credit_harness.orchestration import repository
    x, item, claim, call_id, _ = orphan_wait(factory)
    revision = x.cases.get(x.case.case_id).updated_at
    original = repository.save_work
    def crash(row, value):
        original(row, value)
        if value.expected_case_revision != item.expected_case_revision:
            raise RuntimeError('synthetic crash after publication')
    monkeypatch.setattr(repository, 'save_work', crash)
    with pytest.raises(RuntimeError): recover_read(x, claim, call_id)
    assert x.cases.get(x.case.case_id).updated_at == revision
    assert x.work.get(item.work_item_id).expected_case_revision == revision
    assert not x.evidence.list(x.case.case_id)
    with Session(x.engine) as session:
        assert session.get(CaseCallRow, call_id).observation_id is None
        assert session.get(ReadDispatchRecoveryRow, call_id).status == D.OBSERVATION_NOT_FOUND.value
    monkeypatch.setattr(repository, 'save_work', original)
    recover_read(x, claim, call_id)
    assert x.work.get(item.work_item_id).expected_case_revision == x.cases.get(x.case.case_id).updated_at


def test_resume_recovery_keeps_same_work_item(factory):
    x, item, claim, call_id, _ = orphan_wait(factory)
    recover_read(x, claim, call_id)
    assert x.work.get(item.work_item_id).work_item_id == item.work_item_id
    assert len(x.work.list(x.case.case_id)) == 1


def test_resume_recovery_keeps_same_claim(factory):
    x, item, claim, call_id, _ = orphan_wait(factory)
    recover_read(x, claim, call_id)
    saved = x.work.get(item.work_item_id)
    assert saved.lease_token == claim.lease_token and saved.claimed_by == claim.worker_id
    assert saved.attempt_count == 1


def test_recovered_observation_does_not_consume_second_tool_budget(factory):
    x, _, claim, call_id, _ = orphan_wait(factory)
    before = x.cases.get(x.case.case_id).budget
    recover_read(x, claim, call_id)
    assert x.cases.get(x.case.case_id).budget == before
    with Session(x.engine) as session: assert len(list(session.scalars(select(CaseCallRow)))) == 1


def test_recovered_evidence_is_visible_in_fresh_resume_snapshot(factory):
    x, _, claim, _, _ = orphan_wait(factory)
    assert x.worker.resume_service.resume(claim)
    case = x.cases.get(x.case.case_id)
    assert case.status == CaseStatus.INVESTIGATING
    snap = ReasoningContextAssembler().build(case, x.evidence.list(case.case_id))
    assert all(e.evidence_id in snap.model_dump_json() for e in x.evidence.list(case.case_id))


def test_unrelated_case_revision_change_still_cancels_work(factory, monkeypatch):
    x, item, claim, call_id, _ = orphan_wait(factory)
    reader = x.worker.resume_service.recovery.read_recovery
    original = reader.recover_if_owned
    def mutate(*args):
        result = original(*args)
        x.cases.pause(x.case.case_id, CaseStatus.WAITING)
        return result
    monkeypatch.setattr(reader, 'recover_if_owned', mutate)
    assert x.worker.resume_service.resume(claim) is None
    assert x.work.get(item.work_item_id).status == WorkStatus.CANCELED
    assert x.work.get(item.work_item_id).expected_case_revision < x.cases.get(x.case.case_id).updated_at


def test_already_stale_work_does_not_run_recovery(factory, monkeypatch):
    x, _, claim, _, _ = orphan_wait(factory)
    x.cases.pause(x.case.case_id, CaseStatus.WAITING)
    monkeypatch.setattr(x.worker.resume_service.recovery.read_recovery, 'recover_if_owned',
        lambda *args: pytest.fail('stale work must not recover'))
    assert x.worker.resume_service.resume(claim) is None
    assert not x.evidence.list(x.case.case_id)


def test_foreign_observation_cannot_rebase_work(factory):
    from credit_harness.simulator.scenarios import build_scenario
    x, item, claim, call_id, observation = orphan_wait(factory)
    foreign = x.admin.seed(build_scenario(ScenarioId.S6))
    with Session(x.engine) as session, session.begin():
        session.get(ObservationRow, observation.observation_id).simulation_id = foreign
    result = recover_read(x, claim, call_id)
    assert result.action_taken == D.PROVENANCE_INVALID
    assert x.work.get(item.work_item_id).expected_case_revision == item.expected_case_revision
    assert not x.evidence.list(x.case.case_id)


def test_stale_lease_cannot_rebase_work(factory):
    x, item, claim, call_id, _ = orphan_wait(factory)
    x.advance(121); assert x.work.claim(item.work_item_id, 'successor')
    with pytest.raises(OrchestrationError): recover_read(x, claim, call_id)
    assert not x.evidence.list(x.case.case_id)


def test_terminal_case_cannot_rebase_work(factory):
    x, _, claim, call_id, _ = orphan_wait(factory)
    with Session(x.engine) as session, session.begin():
        session.get(CaseRow, x.case.case_id).status = CaseStatus.CLOSED_VERIFIED.value
    with pytest.raises(OrchestrationError): recover_read(x, claim, call_id)
    assert not x.evidence.list(x.case.case_id)


def test_concurrent_recovery_rebase_publishes_once(factory):
    x, item, claim, call_id, _ = orphan_wait(factory); gate = Barrier(2)
    def recover():
        gate.wait(); return recover_read(x, claim, call_id)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: recover(), range(2)))
    assert sorted(r.action_taken for r in results) == sorted([D.OBSERVATION_RECOVERED, D.OBSERVATION_ALREADY_PUBLISHED])
    assert x.work.get(item.work_item_id).expected_case_revision == x.cases.get(x.case.case_id).updated_at
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 1


def recovering(factory, status=L.INDETERMINATE, max_attempts=3):
    x = factory(ScenarioId.S6); x.read(); x.effect = x.remediate(timeout=True)
    x.resolver = FakeResolver(lambda: x.clock.now, status=status)
    repo = RecoveryRepository(x.runtime.store, x.resolver.capability,
        policy=RecoveryPolicy(grace_seconds=0, max_attempts=max_attempts, backoff_seconds=(5, 10, 20)))
    x.effect_recovery = SideEffectRecoveryCoordinator(repo, x.resolver, clock=lambda: x.clock.now)
    x.worker.resume_service.effect_recovery = x.effect_recovery
    x.item = next(w for w in x.work.list(x.case.case_id) if w.work_type == WorkType.RECOVERY_RECHECK)
    return x


def tick(x):
    item = x.work.get(x.item.work_item_id)
    x.advance(max(0, int((item.not_before-x.clock.now).total_seconds())))
    return x.worker.process(x.work.claim(item.work_item_id, 'recovery-worker'))


def no_progress_near_limit(x):
    with Session(x.engine) as session, session.begin():
        case = lock_case(session, x.cases, x.case.case_id); row, state = state_in(session, case)
        save_state(row, state.model_copy(update=dict(no_progress_count=state.budget.max_no_progress_cycles-1)))


def final_recovery_resets_no_progress(factory, lookup):
    x = recovering(factory, lookup); no_progress_near_limit(x)
    before = x.evidence.list(x.case.case_id)
    result = tick(x)
    assert result.semantic_progress
    assert x.work.state(x.case.case_id).no_progress_count == 0
    assert x.evidence.list(x.case.case_id) == before
    assert x.cases.get(x.case.case_id).status != CaseStatus.ESCALATED
    assert x.work.get(x.item.work_item_id).status == WorkStatus.COMPLETED
    assert x.evaluate().overall_verdict != V.PASS


def test_unknown_to_applied_resets_no_progress(factory):
    final_recovery_resets_no_progress(factory, L.FOUND_APPLIED)


def test_unknown_to_failed_confirmed_resets_no_progress(factory):
    final_recovery_resets_no_progress(factory, L.FOUND_FAILED_NO_EFFECT)


def test_unknown_to_unknown_does_not_reset_no_progress(factory):
    x = recovering(factory); no_progress_near_limit(x)
    result = tick(x)
    assert not result.semantic_progress
    assert x.work.state(x.case.case_id).no_progress_count == 2
    assert x.work.get(x.item.work_item_id).status == WorkStatus.PENDING
    assert x.cases.get(x.case.case_id).status != CaseStatus.ESCALATED


def test_new_recovery_attempt_without_status_change_is_not_progress(factory):
    x = recovering(factory)
    before = x.worker.resume_service.progress(x.case.case_id)
    tick(x)
    assert x.effect_recovery.repository.attempts(x.effect.effect_id)
    assert x.worker.resume_service.progress(x.case.case_id).fingerprint == before.fingerprint


def test_verification_requirement_change_is_progress():
    assert progress_fingerprint((), requirements=(Q.EFFECT_FINALITY,)).fingerprint != progress_fingerprint(
        (), requirements=(Q.POST_EFFECT_MESSAGE_STATUS,)).fingerprint


def test_same_verification_requirements_are_not_progress():
    assert progress_fingerprint((), requirements=(Q.PAYMENT_FINALITY,Q.EFFECT_FINALITY)).fingerprint == progress_fingerprint(
        (), requirements=(Q.EFFECT_FINALITY,Q.PAYMENT_FINALITY,Q.PAYMENT_FINALITY)).fingerprint


def nonsemantic_mutations_are_not_progress(factory, change):
    x = factory(); before = x.worker.resume_service.progress(x.case.case_id)
    if change == 'timer': x.advance(1)
    else:
        with Session(x.engine) as session, session.begin():
            row = session.get(CaseRow, x.case.case_id)
            if change == 'revision': row.updated_at = (x.clock.now+timedelta(microseconds=1)).isoformat()
            else: row.used_tool_calls += 1
    assert x.worker.resume_service.progress(x.case.case_id).fingerprint == before.fingerprint


def test_still_unknown_rearms_recovery_work(factory):
    x = recovering(factory); tick(x)
    item = x.work.get(x.item.work_item_id)
    assert item.status == WorkStatus.PENDING and item.lease_token is None
    assert item.attempt_count == 1 and len(x.resolver.calls) == 1


def test_rearmed_recovery_uses_next_eligible_at(factory):
    x = recovering(factory); tick(x)
    with Session(x.engine) as session:
        next_at = session.get(EffectRecoveryStateRow, x.effect.effect_id).next_eligible_at
    assert x.work.get(x.item.work_item_id).not_before.timestamp() == next_at


def test_recovery_work_not_claimable_before_backoff(factory):
    x = recovering(factory); tick(x)
    assert x.work.claim(x.item.work_item_id, 'another') is None
    assert len(x.resolver.calls) == 1


def test_repeated_unknown_uses_step9_max_attempts(factory):
    x = recovering(factory, max_attempts=4)
    for _ in range(4): tick(x)
    assert len(x.resolver.calls) == 4
    assert len(x.effect_recovery.repository.attempts(x.effect.effect_id)) == 4
    assert x.work.get(x.item.work_item_id).status == WorkStatus.BLOCKED


def test_recovery_limit_creates_operator_followup(factory):
    x = recovering(factory, max_attempts=1); tick(x)
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert any(w.work_type == WorkType.OPERATOR_FOLLOWUP and w.reason_code == WorkReason.EFFECT_UNRESOLVED
               for w in x.work.list(x.case.case_id))


def test_recovery_limit_keeps_ledger_unknown(factory):
    x = recovering(factory, max_attempts=1); tick(x)
    assert x.runtime.store.get_ledger(x.effect.effect_id).status == ES.UNKNOWN


def test_one_effect_has_at_most_one_active_recovery_work(factory):
    from credit_harness.orchestration.repository import create_work
    x = recovering(factory)
    for _ in range(2):
        tick(x)
        with Session(x.engine) as session, session.begin():
            case = lock_case(session, x.cases, x.case.case_id)
            create_work(session, case, work_type=WorkType.RECOVERY_RECHECK, reason=WorkReason.OPERATOR_REQUIRED,
                source_ref=x.effect.effect_id, trigger=Trigger.RECOVERY, now=x.clock.now)
        assert sum(w.work_type == WorkType.RECOVERY_RECHECK and w.status in (WorkStatus.PENDING, WorkStatus.READY, WorkStatus.CLAIMED)
                   for w in x.work.list(x.case.case_id)) == 1


def final_recovery_does_not_rearm(factory, lookup):
    x = recovering(factory, lookup); tick(x)
    assert x.work.get(x.item.work_item_id).status == WorkStatus.COMPLETED
    x.advance(60)
    assert x.work.claim(x.item.work_item_id, 'again') is None


def test_recovery_recheck_never_redispatches_effect(factory):
    x = recovering(factory)
    tick(x); x.resolver.status = L.FOUND_APPLIED; tick(x)
    assert x.runtime.store.get_ledger(x.effect.effect_id).attempt_count == 1
    assert len(x.resolver.calls) == 2
    item = next(w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EFFECT_APPLIED)
    x.advance(1)
    result = x.worker.process(x.work.claim(item.work_item_id, 'verification'))
    assert result.overall_verdict != V.PASS
    assert any(e.claim_type == ClaimType.MESSAGE_CONSUME_STATUS and e.value == 'CONSUMED'
               for e in x.evidence.list(x.case.case_id))


def test_case_revision_only_is_not_progress(factory):
    nonsemantic_mutations_are_not_progress(factory, 'revision')


def test_budget_only_is_not_progress(factory):
    nonsemantic_mutations_are_not_progress(factory, 'budget')


def test_timer_only_is_not_progress(factory):
    nonsemantic_mutations_are_not_progress(factory, 'timer')


def test_recovered_applied_does_not_rearm_recovery(factory):
    final_recovery_does_not_rearm(factory, L.FOUND_APPLIED)


def test_recovered_failed_does_not_rearm_recovery(factory):
    final_recovery_does_not_rearm(factory, L.FOUND_FAILED_NO_EFFECT)


def test_busy_recovery_rearms_at_step9_lease_boundary(factory):
    x = recovering(factory)
    x.advance(30)
    claimed = x.effect_recovery.repository.claim(x.effect.effect_id, 'other-recovery-worker', x.clock.now)
    assert claimed
    result = x.worker.process(x.work.claim(x.item.work_item_id, 'orchestration-worker'))
    assert result.effect_recoveries[0].action_taken.value == 'BUSY_OR_NOT_DUE'
    with Session(x.engine) as session:
        lease = session.get(EffectRecoveryStateRow, x.effect.effect_id).lease_until
    assert x.work.get(x.item.work_item_id).not_before.timestamp() == lease
    assert not x.resolver.calls


def test_recovery_recheck_concurrent_claims_one_attempt(factory):
    x = recovering(factory); x.advance(30); gate = Barrier(2)
    def process(i):
        gate.wait()
        claim = x.work.claim(x.item.work_item_id, f'worker-{i}')
        return x.worker.process(claim) if claim else None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(process, range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(x.resolver.calls) == 1


def test_committed_recovery_rebase_survives_worker_crash(factory):
    x, item, claim, call_id, _ = orphan_wait(factory)
    recover_read(x, claim, call_id)  # Both publication and rebase have committed.
    x.advance(121)
    successor = x.work.claim(item.work_item_id, 'successor')
    assert x.worker.resume_service.resume(successor)
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 1


def test_concurrent_mutation_after_rebase_cannot_be_absorbed(factory, monkeypatch):
    from threading import Event
    x, item, claim, call_id, _ = orphan_wait(factory)
    rebased, mutated = Event(), Event()
    reader = x.worker.resume_service.recovery.read_recovery
    original = reader.recover_if_owned
    def recover(*args):
        result = original(*args)
        rebased.set()
        assert mutated.wait(10)
        return result
    def mutate():
        assert rebased.wait(10)
        x.cases.pause(x.case.case_id, CaseStatus.WAITING)
        mutated.set()
    monkeypatch.setattr(reader, 'recover_if_owned', recover)
    with ThreadPoolExecutor(2) as pool:
        other = pool.submit(mutate)
        result = pool.submit(x.worker.resume_service.resume, claim).result()
        other.result()
    assert result is None
    saved = x.work.get(item.work_item_id)
    assert saved.status == WorkStatus.CANCELED
    assert saved.expected_case_revision < x.cases.get(x.case.case_id).updated_at


def test_concurrent_recovery_same_claim_invokes_step9_once(factory, monkeypatch):
    from threading import Event
    x = recovering(factory); x.advance(30)
    claim = x.work.claim(x.item.work_item_id, 'shared-worker')
    entered, release = Event(), Event()
    original = x.effect_recovery.recover
    def paused(effect_id):
        entered.set()
        assert release.wait(10)
        return original(effect_id)
    monkeypatch.setattr(x.effect_recovery, 'recover', paused)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(x.worker.process, claim)
        assert entered.wait(10)
        assert pool.submit(x.worker.process, claim).result() is None
        release.set()
        assert first.result() is not None
    assert len(x.resolver.calls) == 1


def test_legacy_progress_hash_upgrade_is_not_progress(factory):
    x = factory(); _, item = wait(x)
    with Session(x.engine) as session, session.begin():
        case = lock_case(session, x.cases, x.case.case_id); row, state = state_in(session, case)
        save_state(row, state.model_copy(update=dict(no_progress_count=1,
            last_progress_fingerprint='0'*64, last_progress_version=None)))
    x.worker.process(due(x, item))
    assert x.work.state(x.case.case_id).no_progress_count == 2


def test_lease_expiry_during_rebase_rolls_back_publication(factory, monkeypatch):
    x, item, claim, call_id, _ = orphan_wait(factory)
    reader = x.worker.resume_service.recovery.read_recovery
    original = reader._recover
    def expire(*args):
        result = original(*args)
        x.clock.now += timedelta(seconds=121)
        return result
    monkeypatch.setattr(reader, '_recover', expire)
    with pytest.raises(OrchestrationError): recover_read(x, claim, call_id)
    assert not x.evidence.list(x.case.case_id)
    assert x.work.get(item.work_item_id).expected_case_revision == item.expected_case_revision
