from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy.orm import Session
from tests.test_orchestration import factory, create, wait, due
from tests.test_resume_recovery_integrity import recovering, tick, no_progress_near_limit
from credit_harness.cases.models import CaseStatus
from credit_harness.domain.enums import ToolName as Tool
from credit_harness.evidence.models import ClaimType
from credit_harness.authorization.models import EffectStatus as ES
from credit_harness.recovery.models import LookupStatus as L
from credit_harness.evaluation.models import (UnresolvedVerificationRequirement, VerificationRequirement as Q,
    EvaluationDimension as D, EvaluationReason as R, EvaluationVerdict as V)
from credit_harness.orchestration.routing import RequirementRouter, RequirementRoute as Route, RecoveryRouteState
from credit_harness.orchestration.models import WorkType as T, WorkStatus as S, WorkReason as W, OrchestrationBudget
from credit_harness.orchestration.repository import ACTIVE, lock_case
from credit_harness.orchestration.handoff import effect_handoff


def requirement(q, effect_ref=None, reason=R.RECOVERY_UNRESOLVED):
    return UnresolvedVerificationRequirement(requirement=q, dimension=D.RECOVERY, reason_code=reason, effect_ref=effect_ref)


def routed(q, *, effect_ref=None, effects=(), tools=tuple(Tool)):
    return RequirementRouter().route(requirement(q, effect_ref), available_tools=tools, effects=effects)


def consume(x):
    report = x.evaluate()
    assert report.overall_verdict == V.INCONCLUSIVE
    items = x.worker.handoff.consume(report)
    assert_no_finality_reads(x)
    return items


def assert_no_finality_reads(x):
    assert not any(w.work_type == T.VERIFICATION_REQUIRED and (
        w.requirement in (Q.EFFECT_FINALITY, Q.RECOVERY_FINALITY) or
        set(w.verification_requirements) & {Q.EFFECT_FINALITY, Q.RECOVERY_FINALITY})
        for w in x.work.list(x.case.case_id))
    assert not any(w.reason_code == W.NO_TOOL for w in x.work.list(x.case.case_id))


def test_evaluator_effect_finality_routes_to_existing_recovery_work(factory):
    x = recovering(factory)
    items = consume(x)
    active = [w for w in x.work.list(x.case.case_id) if w.work_type == T.RECOVERY_RECHECK and w.status in ACTIVE]
    assert len(active) == 1 and active[0].work_item_id == x.item.work_item_id
    assert x.item.work_item_id in {w.work_item_id for w in items}


def test_finality_handoff_cannot_bypass_recovery_backoff(factory):
    x = recovering(factory); consume(x); tick(x)
    before = len(x.resolver.calls)
    # Other legitimate reads may be ready; no finality read can race recovery.
    claim = x.work.claim_next('scheduler')
    if claim:
        item = x.work.get(claim.work_item_id)
        assert item.work_type == T.VERIFICATION_REQUIRED
        assert item.requirement not in (Q.EFFECT_FINALITY, Q.RECOVERY_FINALITY)
    assert x.work.claim(x.item.work_item_id, 'early') is None
    assert len(x.resolver.calls) == before == 1
    assert x.cases.get(x.case.case_id).status != CaseStatus.ESCALATED
    assert_no_finality_reads(x)


def test_unknown_twice_then_applied_routes_to_real_message_verification(factory):
    x = recovering(factory, max_attempts=4); consume(x)
    for _ in range(2):
        result = tick(x)
        assert not result.semantic_progress
        assert x.cases.get(x.case.case_id).status != CaseStatus.ESCALATED
    x.resolver.status = L.FOUND_APPLIED
    assert tick(x).semantic_progress
    item = next(w for w in x.work.list(x.case.case_id) if w.reason_code == W.EFFECT_APPLIED)
    x.advance(1)
    result = x.worker.process(x.work.claim(item.work_item_id, 'verification'))
    assert result is not None
    assert any(e.claim_type == ClaimType.MESSAGE_CONSUME_STATUS and e.value == 'CONSUMED'
        for e in x.evidence.list(x.case.case_id))
    assert x.runtime.store.get_ledger(x.effect.effect_id).attempt_count == 1
    assert len(x.resolver.calls) == 3
    assert_no_finality_reads(x)


def test_finality_handoff_waits_for_step9_limit_and_one_operator(factory):
    x = recovering(factory, max_attempts=4); consume(x)
    for _ in range(3):
        tick(x)
        assert x.cases.get(x.case.case_id).status != CaseStatus.ESCALATED
    tick(x)
    # Real downstream failures may now exceed the Evaluator's grace window;
    # preserve that verdict instead of forcing an INCONCLUSIVE report.
    x.worker.handoff.consume(x.evaluate())
    ops = [w for w in x.work.list(x.case.case_id) if w.work_type == T.OPERATOR_FOLLOWUP and w.reason_code == W.EFFECT_UNRESOLVED]
    assert len(ops) == 1 and ops[0].source_ref == x.item.work_item_id
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert x.runtime.store.get_ledger(x.effect.effect_id).status == ES.UNKNOWN
    assert len(x.resolver.calls) == 4
    assert_no_finality_reads(x)


def test_step9_exhaustion_report_reuses_existing_operator_followup(factory):
    x = recovering(factory, max_attempts=1)
    tick(x)
    consume_with_long_convergence_window(x)
    assert sum(w.work_type == T.OPERATOR_FOLLOWUP and w.reason_code == W.EFFECT_UNRESOLVED
        for w in x.work.list(x.case.case_id)) == 1


def consume_with_long_convergence_window(x):
    # Isolate finality routing from the separate downstream convergence deadline.
    # This is an existing configurable Evaluator policy, not a forged verdict.
    from credit_harness.evaluation.contract import EvaluationPolicy
    from credit_harness.evaluation.evaluator import IndependentEvaluator
    evaluator = IndependentEvaluator(x.cases, clock=lambda: x.clock.now,
        policy=EvaluationPolicy(convergence_grace_seconds=300))
    report = x.repository.record(evaluator.evaluate(x.case.case_id))
    assert report.overall_verdict == V.INCONCLUSIVE
    return x.worker.handoff.consume(report)


def test_handoff_consumes_step9_escalation_without_own_retry_counter(factory):
    x = recovering(factory, max_attempts=1)
    x.advance(30)
    x.effect_recovery.recover(x.effect.effect_id)
    consume_with_long_convergence_window(x)
    consume_with_long_convergence_window(x)
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert len(x.resolver.calls) == 1
    assert sum(w.work_type == T.OPERATOR_FOLLOWUP and w.reason_code == W.EFFECT_UNRESOLVED
        for w in x.work.list(x.case.case_id)) == 1
    assert_no_finality_reads(x)


def test_unknown_recovery_does_not_increment_investigation_no_progress(factory):
    x = recovering(factory); no_progress_near_limit(x)
    before = x.work.state(x.case.case_id).no_progress_count
    assert not tick(x).semantic_progress
    assert x.work.state(x.case.case_id).no_progress_count == before


def test_repeated_unknown_recovery_uses_step9_budget_only(factory):
    x = recovering(factory, max_attempts=4); no_progress_near_limit(x)
    for _ in range(4): tick(x)
    assert len(x.resolver.calls) == 4
    assert x.work.state(x.case.case_id).no_progress_count == 2
    assert not any(w.reason_code == W.NO_PROGRESS for w in x.work.list(x.case.case_id))


def test_payment_verification_can_run_while_recovery_backoff_exists(factory):
    x = recovering(factory, max_attempts=4); no_progress_near_limit(x)
    for _ in range(3): tick(x)
    item = create(x, requirement=Q.PAYMENT_FINALITY)
    before = x.cases.get(x.case.case_id).budget.used_tool_calls
    count = len(x.resolver.calls)
    assert x.worker.process(x.work.claim(item.work_item_id, 'payment')) is not None
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == before + 1
    assert len(x.resolver.calls) == count  # Resume hook respects Step 9 backoff too.
    assert x.work.get(item.work_item_id).status == S.COMPLETED


@pytest.mark.parametrize('status', [L.FOUND_APPLIED, L.FOUND_FAILED_NO_EFFECT])
def test_recovery_final_state_resets_investigation_no_progress(factory, status):
    x = recovering(factory, status); no_progress_near_limit(x)
    assert tick(x).semantic_progress
    assert x.work.state(x.case.case_id).no_progress_count == 0


def test_investigation_no_progress_still_escalates_independently(factory):
    x = factory(); x.work.configure(x.case.case_id, OrchestrationBudget(max_no_progress_cycles=1))
    _, item = wait(x); x.worker.process(due(x, item))
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert any(w.reason_code == W.NO_PROGRESS for w in x.work.list(x.case.case_id))


def test_effect_finality_never_routes_to_read_verification():
    assert routed(Q.EFFECT_FINALITY)[0].route == Route.OPERATOR_FOLLOWUP


def test_recovery_finality_never_routes_to_read_verification():
    assert routed(Q.RECOVERY_FINALITY)[0].route == Route.OPERATOR_FOLLOWUP


def test_supported_payment_requirement_routes_to_read():
    assert routed(Q.PAYMENT_FINALITY)[0].route == Route.READ_VERIFICATION


def test_post_effect_requirement_routes_to_read():
    assert routed(Q.POST_EFFECT_MESSAGE_STATUS)[0].route == Route.READ_VERIFICATION


@pytest.mark.parametrize('q', [Q.PROVENANCE, Q.SAFE_POLICY, Q.SAFETY_GAP, Q.READ_PUBLICATION])
def test_unmachine_resolvable_requirement_routes_to_operator(q):
    assert routed(q)[0].route == Route.OPERATOR_FOLLOWUP


def test_unavailable_payment_tool_routes_to_operator():
    assert routed(Q.PAYMENT_FINALITY, tools=(Tool.MESSAGES,))[0].route == Route.OPERATOR_FOLLOWUP


def test_reason_summary_never_controls_route():
    router = RequirementRouter()
    a = requirement(Q.RECOVERY_FINALITY)
    b = a.model_copy(update=dict(reason_code=R.PAYMENT_FINALITY_UNKNOWN))
    assert router.route(a, available_tools=tuple(Tool)) == router.route(b, available_tools=tuple(Tool))
    assert 'reason_summary' not in UnresolvedVerificationRequirement.model_fields


@pytest.mark.parametrize('status', [ES.PREPARED, ES.DISPATCHED, ES.ACCEPTED, ES.UNKNOWN])
def test_each_unresolved_effect_state_routes_to_recovery(status):
    state = RecoveryRouteState(effect_ref='a'*64, status=status, recovery_available=True, requires_escalation=False)
    assert routed(Q.EFFECT_FINALITY, effect_ref=state.effect_ref, effects=(state,))[0].route == Route.EFFECT_RECOVERY


def test_aggregate_finality_routes_each_current_effect_without_cross_join():
    a = RecoveryRouteState(effect_ref='a'*64, status=ES.UNKNOWN, recovery_available=True, requires_escalation=False)
    b = a.model_copy(update=dict(effect_ref='b'*64, requires_escalation=True))
    c = a.model_copy(update=dict(effect_ref='c'*64, status=ES.APPLIED))
    results = routed(Q.RECOVERY_FINALITY, effects=(c,b,a))
    assert [(r.effect_ref, r.route) for r in results] == [('a'*64, Route.EFFECT_RECOVERY)]
    assert routed(Q.RECOVERY_FINALITY, effects=(b,c))[0].route == Route.OPERATOR_FOLLOWUP
    assert routed(Q.EFFECT_FINALITY, effect_ref='d'*64, effects=(a,))[0].route == Route.OPERATOR_FOLLOWUP


def test_concurrent_evaluator_and_effect_handoff_reuse_one_recovery_work(factory):
    x = recovering(factory); report = x.evaluate(); gate = Barrier(2)
    # Remove only the test fixture's unclaimed handoff to exercise concurrent creation,
    # rather than merely querying an already existing key.
    from credit_harness.orchestration.tables import WorkItemRow
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        session.delete(session.get(WorkItemRow, x.item.work_item_id))
    def handoff(i):
        gate.wait()
        if i == 0: return x.worker.handoff.consume(report)
        with Session(x.engine) as session, session.begin():
            lock_case(session, x.cases, x.case.case_id)
            return (effect_handoff(session, x.runtime.store.get_ledger(x.effect.effect_id)),)
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(handoff, range(2)))
    ids = {w.work_item_id for result in results for w in result if w.work_type == T.RECOVERY_RECHECK}
    assert len(ids) == 1
    assert sum(w.work_type == T.RECOVERY_RECHECK and w.status in ACTIVE for w in x.work.list(x.case.case_id)) == 1
    assert_no_finality_reads(x)
