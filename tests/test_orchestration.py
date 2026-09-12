from math import ceil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.support.evaluation_fixture import EvaluationFixture
from credit_harness.agent.runtime import InvestigationAgentRuntime
from credit_harness.agent.models import AgentRunConfig
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.repository import CaseRepository
from credit_harness.cases.tables import CaseRow
from credit_harness.domain.enums import ScenarioId, ToolName as Tool
from credit_harness.evidence.models import ClaimType
from credit_harness.planner.model import FakePlannerModel
from credit_harness.planner.service import PlannerService
from credit_harness.planner.models import PlannerDraft, WaitCandidate, EscalateCandidate, ReasonCode
from credit_harness.evaluation.models import EvaluationVerdict as V, VerificationRequirement as Q
from credit_harness.authorization.models import EffectStatus
from credit_harness.orchestration.models import *
from credit_harness.orchestration.repository import WorkRepository, create_work, lock_case
from credit_harness.orchestration.resume import CaseResumeService, knowledge_fingerprints
from credit_harness.orchestration.service import DurableCaseOrchestrator
from credit_harness.orchestration.handoff import EvaluationHandoffService, effect_handoff


def wait_draft(bundle):
    gaps = tuple(g.gap_id for g in bundle.deterministic_derived.open_evidence_gaps)
    return PlannerDraft(snapshot_id=bundle.snapshot_id, candidates=(WaitCandidate(candidate_id="wait",
        target_gap_ids=gaps, reason_code=ReasonCode.INSUFFICIENT_EVIDENCE,
        suggested_wait_seconds=60, reason_summary="Await new observations."),),
        observation_summary="Observed only", uncertainty_summary="UNKNOWN")


@pytest.fixture
def factory(engine, monkeypatch):
    monkeypatch.setenv("CAPABILITY_SIGNING_SECRET", "synthetic-orchestration-test-only")
    instances = []
    def make(scenario=ScenarioId.S8, *, case_id="CASE-ORCH", model=None):
        x = EvaluationFixture(engine, scenario=scenario, case_id=case_id)
        instances.append(x)
        x.work = WorkRepository(x.cases, clock=lambda:x.clock.now, lease_seconds=120)
        x.agent = InvestigationAgentRuntime(x.cases, x.evidence, ReasoningContextAssembler(),
            PlannerService(FakePlannerModel(model or wait_draft)), x.executor, config=AgentRunConfig(max_turns=3))
        x.worker = DurableCaseOrchestrator(x.work, x.evidence, x.executor, x.agent, x.evaluator, x.closure)
        return x
    yield make
    for x in reversed(instances): x.close()


def wait(x):
    result = x.agent.run(x.case.case_id)
    item = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == result.run_id)
    return result, item


def due(x, item):
    x.advance(max(0, ceil((item.not_before-x.clock.now).total_seconds())))
    return x.work.claim(item.work_item_id, "worker-1")


def create(x, kind=WorkType.VERIFICATION_REQUIRED, requirement=Q.POST_EFFECT_MESSAGE_STATUS,
           reason=WorkReason.EVALUATION_INCONCLUSIVE, signal=None, source="test-source"):
    with Session(x.engine) as session, session.begin():
        case = lock_case(session, x.cases, x.case.case_id)
        return create_work(session, case, work_type=kind, reason=reason, source_ref=source,
            trigger=Trigger.EVALUATION, now=x.clock.now, requirement=requirement, required_signal=signal)


def signal(x, item, kind=None):
    return ResolutionSignal(signal_id="SIG-001", tenant_id=x.cases.tenant_id, case_id=x.case.case_id,
        work_item_id=item.work_item_id, signal_type=kind or item.required_signal,
        subject=x.case.internal_order_id, created_at=x.clock.now, source_actor_ref="TEST-OPERATOR")


def escalate(x):
    x.cases.pause(x.case.case_id, CaseStatus.ESCALATED)
    return create(x, WorkType.OPERATOR_FOLLOWUP, None, WorkReason.DEPLOYMENT_STATE_UNKNOWN,
                  SignalType.DEPLOYMENT_STATE_UPDATED)


def test_wait_creates_resume_work_atomically(factory, monkeypatch):
    x = factory(); before = x.cases.get(x.case.case_id)
    from credit_harness.orchestration import handoff
    original = handoff.pause_handoff
    def crash(*args):
        original(*args)
        raise RuntimeError("synthetic transaction crash")
    monkeypatch.setattr(handoff, "pause_handoff", crash)
    with pytest.raises(RuntimeError): wait(x)
    assert x.cases.get(x.case.case_id) == before
    assert x.work.list(x.case.case_id) == ()
    monkeypatch.setattr(handoff, "pause_handoff", original)
    run, item = wait(x)
    assert item.status == WorkStatus.PENDING and item.previous_run_id == run.run_id
    assert item.expected_case_status == CaseStatus.WAITING
    assert item.expected_case_revision == x.cases.get(x.case.case_id).updated_at


def test_wait_work_not_due_before_not_before(factory):
    x = factory(); _, item = wait(x)
    x.advance(59)
    assert x.work.poll_due_work() == ()
    assert x.work.claim(item.work_item_id, "worker") is None


def test_due_wait_can_resume(factory):
    x = factory(); _, item = wait(x); claim = due(x, item)
    assert x.worker.resume_service.resume(claim)
    after = x.cases.get(x.case.case_id)
    assert after.status == CaseStatus.INVESTIGATING and after.updated_at > item.expected_case_revision


def test_wait_time_does_not_create_evidence(factory):
    x = factory(); _, item = wait(x); before = x.evidence.list(x.case.case_id)
    due(x, item)
    assert x.evidence.list(x.case.case_id) == before


def test_wait_time_does_not_change_payment_truth(factory):
    from credit_harness.benchmark.faults import private_world
    x = factory(); _, item = wait(x); before = private_world(x).fund
    due(x, item)
    assert private_world(x).fund == before


def test_resume_uses_fresh_snapshot(factory):
    x = factory(); first, item = wait(x)
    result = x.worker.process(due(x, item))
    assert result.run_id != first.run_id
    assert result.initial_snapshot_id != first.final_snapshot_id
    assert result.lineage.parent_run_id == first.run_id
    assert result.lineage.resumed_from_work_item_id == item.work_item_id
    assert x.worker.runs(x.case.case_id)[0] == result


def test_resume_does_not_restore_chat_history(factory):
    x = factory(); first, item = wait(x)
    original = first.model_dump_json()
    seen = []
    old = x.agent.planner.plan
    def plan(snapshot):
        seen.append(snapshot); return old(snapshot)
    x.agent.planner.plan = plan
    x.worker.process(due(x, item))
    assert seen and all(type(s).__name__ == "ReasoningContextSnapshot" for s in seen)
    assert first.model_dump_json() == original


def test_duplicate_work_creation_is_idempotent(factory):
    x = factory(); a = create(x); b = create(x)
    assert a == b and len(x.work.list(x.case.case_id)) == 1


def test_two_workers_claim_one_work_item(factory):
    x = factory(); _, item = wait(x); x.advance(60); gate = Barrier(2)
    def claim(i):
        gate.wait(); return x.work.claim(item.work_item_id, f"worker-{i}")
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(claim, range(2)))
    assert sum(r is not None for r in results) == 1


def test_two_workers_only_one_resume_wins(factory):
    x = factory(); _, item = wait(x); claim = due(x, item); gate = Barrier(2)
    def resume(_):
        gate.wait(); return x.worker.resume_service.resume(claim)
    with ThreadPoolExecutor(2) as pool: results = list(pool.map(resume, range(2)))
    assert sum(r is not None for r in results) == 1
    assert x.work.state(x.case.case_id).resume_cycle_count == 1


def test_expired_lease_can_be_reclaimed(factory):
    x = factory(); _, item = wait(x); old = due(x, item); x.advance(121)
    new = x.work.claim(item.work_item_id, "worker-2")
    assert new and new.lease_token != old.lease_token
    assert x.worker.resume_service.resume(new)


def test_stale_worker_cannot_complete_after_lease_loss(factory):
    x = factory(); _, item = wait(x); old = due(x, item); x.advance(121)
    new = x.work.claim(item.work_item_id, "worker-2")
    with pytest.raises(OrchestrationError): x.work.complete(old)
    assert x.work.complete(new) == x.work.complete(new)


def test_resume_cas_rejects_stale_case_revision(factory):
    x = factory(); _, item = wait(x); x.cases.pause(x.case.case_id, CaseStatus.WAITING)
    assert x.worker.resume_service.resume(due(x, item)) is None
    assert x.cases.get(x.case.case_id).status == CaseStatus.WAITING
    assert x.work.get(item.work_item_id).status == WorkStatus.CANCELED


def test_expired_started_run_is_not_dispatched_twice(factory):
    x = factory(); _, item = wait(x); claim = due(x, item)
    x.worker.resume_service.resume(claim); x.worker.resume_service.start_once(claim)
    x.advance(121); next_claim = x.work.claim(item.work_item_id, "successor")
    assert x.worker.process(next_claim) is None
    assert x.work.get(item.work_item_id).status == WorkStatus.BLOCKED
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED


def test_escalated_case_does_not_time_resume(factory):
    x = factory(); item = escalate(x); x.advance(3600)
    assert x.work.poll_due_work() == ()
    assert x.work.claim(item.work_item_id, "worker") is None


def test_escalated_case_requires_matching_resolution_signal(factory):
    x = factory(); item = escalate(x)
    x.work.submit_signal(signal(x, item))
    assert x.worker.resume_service.resume(x.work.claim(item.work_item_id, "worker"))


def test_wrong_signal_does_not_resume(factory):
    x = factory(); item = escalate(x)
    with pytest.raises(OrchestrationError): x.work.submit_signal(signal(x, item, SignalType.SOURCE_RECOVERED))
    assert x.work.claim(item.work_item_id, "worker") is None


def test_resolution_signal_is_not_evidence(factory):
    x = factory(); item = escalate(x); before = x.evidence.list(x.case.case_id)
    x.work.submit_signal(signal(x, item))
    assert x.evidence.list(x.case.case_id) == before


def test_resolution_signal_does_not_satisfy_gap(factory):
    x = factory(); item = escalate(x)
    assembler = ReasoningContextAssembler()
    before = assembler.build(x.cases.get(x.case.case_id), x.evidence.list(x.case.case_id))
    x.work.submit_signal(signal(x, item))
    after = assembler.build(x.cases.get(x.case.case_id), x.evidence.list(x.case.case_id))
    assert before.open_evidence_gaps == after.open_evidence_gaps


def test_operator_followup_is_durable(factory):
    x = factory(); item = escalate(x)
    other = WorkRepository(CaseRepository(x.engine, x.cases.tenant_id), clock=lambda:x.clock.now)
    assert other.get(item.work_item_id) == item


@pytest.mark.parametrize("terminal", [CaseStatus.CLOSED, CaseStatus.CLOSED_VERIFIED])
def test_terminal_case_work_is_not_claimable(factory, terminal):
    x = factory(); _, item = wait(x)
    with Session(x.engine) as session, session.begin(): session.get(CaseRow, x.case.case_id).status = terminal.value
    x.advance(60)
    assert x.work.claim(item.work_item_id, "worker") is None
    assert x.work.get(item.work_item_id).status == WorkStatus.CANCELED


def terminal_resume(x, terminal):
    _, item = wait(x); claim = due(x, item)
    with Session(x.engine) as session, session.begin(): session.get(CaseRow, x.case.case_id).status = terminal.value
    with pytest.raises(OrchestrationError): x.worker.resume_service.resume(claim)
    assert x.cases.get(x.case.case_id).status == terminal


def test_closed_verified_case_cannot_resume(factory): terminal_resume(factory(), CaseStatus.CLOSED_VERIFIED)
def test_closed_case_cannot_resume(factory): terminal_resume(factory(), CaseStatus.CLOSED)
def test_late_due_work_cannot_reopen_case(factory): terminal_resume(factory(), CaseStatus.CLOSED_VERIFIED)


def test_terminal_case_cannot_accept_resolution_resume(factory):
    x = factory(); item = escalate(x)
    with Session(x.engine) as session, session.begin(): session.get(CaseRow, x.case.case_id).status = CaseStatus.CLOSED.value
    with pytest.raises(OrchestrationError): x.work.submit_signal(signal(x, item))


def test_cross_tenant_work_cannot_be_scanned_claimed_or_resumed(factory):
    x = factory(); _, item = wait(x); x.advance(60)
    other = WorkRepository(CaseRepository(x.engine, "OTHER-TENANT"), clock=lambda:x.clock.now)
    assert other.poll_due_work() == ()
    with pytest.raises(OrchestrationError): other.claim(item.work_item_id, "worker")
    with pytest.raises(OrchestrationError): other.submit_signal(signal(x, item, SignalType.OPERATOR_ACKNOWLEDGED))


def test_applied_effect_creates_verification_work(factory):
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate()
    items = [w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EFFECT_APPLIED]
    assert len(items) == 1 and items[0].source_ref == effect.effect_id
    assert items[0].requirement == Q.POST_EFFECT_MESSAGE_STATUS


def test_duplicate_applied_events_create_one_work_item(factory):
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate()
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        a = effect_handoff(session, effect); b = effect_handoff(session, effect)
    assert a.work_item_id == b.work_item_id
    assert sum(w.reason_code == WorkReason.EFFECT_APPLIED for w in x.work.list(x.case.case_id)) == 1


def test_verification_work_does_not_write_evidence(factory):
    x = factory(); before = x.evidence.list(x.case.case_id); create(x)
    assert x.evidence.list(x.case.case_id) == before


def test_verification_requires_real_read(factory):
    x = factory(ScenarioId.S6); x.read(); x.remediate()
    report = x.evaluate()
    assert report.overall_verdict != V.PASS
    assert any(r.requirement == Q.POST_EFFECT_MESSAGE_STATUS for r in report.unresolved_requirements)


def test_verification_read_produces_observation_then_evidence(factory):
    x = factory(ScenarioId.S6); x.read(); x.remediate()
    item = next(w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EFFECT_APPLIED)
    x.advance(1); report = x.worker.process(x.work.claim(item.work_item_id, "worker"))
    assert report.overall_verdict == V.INCONCLUSIVE
    observed = [e for e in x.evidence.list(x.case.case_id) if e.claim_type == ClaimType.MESSAGE_CONSUME_STATUS and e.value == "CONSUMED"]
    assert observed
    assert x.evidence.get_raw_observation(observed[-1].evidence_id, case_id=x.case.case_id).tool == Tool.MESSAGES
    assert any(w.reason_code == WorkReason.EVALUATION_INCONCLUSIVE for w in x.work.list(x.case.case_id))


def test_fail_does_not_auto_remediate(factory):
    x = factory(ScenarioId.S6); x.read(); x.advance(100)
    report = x.evaluate(); assert report.overall_verdict == V.FAIL
    items = x.worker.handoff.consume(report)
    assert items and all(w.work_type == WorkType.OPERATOR_FOLLOWUP for w in items)


def test_revision_only_change_is_not_cross_run_progress(factory):
    x = factory(); before = knowledge_fingerprints(x.evidence.list(x.case.case_id))
    x.cases.pause(x.case.case_id, CaseStatus.WAITING)
    assert knowledge_fingerprints(x.evidence.list(x.case.case_id)) == before


def test_budget_only_change_is_not_progress(factory):
    x = factory(); before = knowledge_fingerprints(x.evidence.list(x.case.case_id))
    with Session(x.engine) as session, session.begin(): session.get(CaseRow, x.case.case_id).used_tool_calls += 1
    assert knowledge_fingerprints(x.evidence.list(x.case.case_id)) == before


def test_new_observation_timeout_history_can_be_progress_but_not_truth(factory):
    x = factory(); before = knowledge_fingerprints(x.evidence.list(x.case.case_id))
    x.read(Tool.PAYMENT)
    assert knowledge_fingerprints(x.evidence.list(x.case.case_id)) != before
    assert not any(e.claim_type == ClaimType.PAYMENT_FINALITY and e.value in ("FAILED", "SETTLED", "NOT_EXECUTED")
                   for e in x.evidence.list(x.case.case_id))


def test_repeated_no_progress_escalates(factory):
    x = factory(); x.work.configure(x.case.case_id, OrchestrationBudget(max_no_progress_cycles=1))
    _, item = wait(x); x.worker.process(due(x, item))
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert any(w.reason_code == WorkReason.NO_PROGRESS for w in x.work.list(x.case.case_id))


def test_wait_budget_exhaustion_escalates(factory):
    x = factory(); x.work.configure(x.case.case_id, OrchestrationBudget(max_resume_cycles=0))
    _, item = wait(x)
    assert item.reason_code == WorkReason.RESUME_LIMIT
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED


def test_repeated_wait_is_bounded(factory):
    x = factory(); x.work.configure(x.case.case_id, OrchestrationBudget(max_resume_cycles=2))
    _, item = wait(x)
    for _ in range(2):
        result = x.worker.process(due(x, item))
        item = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == result.run_id)
    assert item.reason_code == WorkReason.RESUME_LIMIT
    assert x.work.state(x.case.case_id).resume_cycle_count == 2
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 0


def test_resume_runs_recovery_before_investigation(factory):
    x = factory(); _, item = wait(x); events=[]
    old = x.worker.resume_service.recover_before_resume
    x.worker.resume_service.recover_before_resume = lambda claim:(events.append("recovery"), old(claim))[1]
    plan = x.agent.planner.plan
    x.agent.planner.plan = lambda snap:(events.append("planner"), plan(snap))[1]
    x.worker.process(due(x, item))
    assert events[0] == "recovery" and "planner" in events


def payment_then_wait(bundle):
    from tests.test_agent_runtime import payment
    groups = [g for g in bundle.untrusted_external_data.history_digest.repeated_lookup_groups if g.tool == Tool.PAYMENT]
    draft = wait_draft(bundle)
    if not any(g.latest_consecutive_count > 0 for g in groups):
        return draft.model_copy(update=dict(candidates=(payment(bundle),)))
    return draft


def test_s8_wait_resume_new_payment_observation_remains_unknown(factory):
    x = factory(model=payment_then_wait)
    x.work.configure(x.case.case_id, OrchestrationBudget(max_resume_cycles=3))
    _, item = wait(x)
    for _ in range(3):
        result = x.worker.process(due(x, item))
        assert result.tool_calls_dispatched == 1
        item = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == result.run_id)
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 4
    assert not any(e.claim_type == ClaimType.PAYMENT_FINALITY and e.value != "UNKNOWN"
                   for e in x.evidence.list(x.case.case_id))


def test_new_evidence_resets_resume_no_progress_count(factory):
    x = factory(); _, item = wait(x)
    result = x.worker.process(due(x, item))
    assert x.work.state(x.case.case_id).no_progress_count == 1
    item = next(w for w in x.work.list(x.case.case_id) if w.previous_run_id == result.run_id)
    x.agent.planner = PlannerService(FakePlannerModel(payment_then_wait))
    x.worker.process(due(x, item))
    assert x.work.state(x.case.case_id).no_progress_count == 0


def test_inconclusive_evaluation_creates_bounded_followup(factory):
    x = factory(); x.work.configure(x.case.case_id, OrchestrationBudget(max_verification_cycles=1))
    item = create(x, requirement=Q.PAYMENT_FINALITY)
    report = x.worker.process(x.work.claim(item.work_item_id, "worker"))
    assert report.overall_verdict == V.INCONCLUSIVE
    item = next(w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EVALUATION_INCONCLUSIVE
                and w.requirement == Q.PAYMENT_FINALITY and w.status == WorkStatus.PENDING)
    assert x.worker.process(due(x, item)) is None
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert x.work.state(x.case.case_id).verification_cycle_count == 1


def test_pass_closes_and_cancels_pending_work(factory):
    x = factory(ScenarioId.S6); x.read(); x.remediate()
    first = next(w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EFFECT_APPLIED)
    x.advance(1); report = x.worker.process(x.work.claim(first.work_item_id, "worker"))
    assert report.overall_verdict == V.INCONCLUSIVE
    x.progress()  # Private external world only; no Case reopening / Evidence insertion.
    for _ in range(20):
        if x.cases.get(x.case.case_id).status.is_terminal: break
        x.advance(31)
        ids = x.work.poll_due_work()
        candidate = next((i for i in ids if x.work.get(i).work_type == WorkType.VERIFICATION_REQUIRED), None)
        if candidate:
            x.worker.process(x.work.claim(candidate, "worker"))
    assert x.cases.get(x.case.case_id).status == CaseStatus.CLOSED_VERIFIED
    assert all(w.status in (WorkStatus.CANCELED, WorkStatus.COMPLETED) for w in x.work.list(x.case.case_id))


def configure_recovery(x):
    from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
    from credit_harness.recovery.repository import RecoveryRepository
    from credit_harness.recovery.service import SideEffectRecoveryCoordinator
    from credit_harness.recovery.prepared import PreparedEffectResumer
    from credit_harness.recovery.models import RecoveryPolicy
    resolver = SyntheticEffectStatusResolver(x.engine, x.cases.tenant_id, clock=lambda:x.clock.now)
    repo = RecoveryRepository(x.runtime.store, resolver.capability,
        policy=RecoveryPolicy(grace_seconds=0, backoff_seconds=(1,)))
    recovery = SideEffectRecoveryCoordinator(repo, resolver, prepared_resumer=PreparedEffectResumer(x.runtime),
        clock=lambda:x.clock.now)
    x.worker.resume_service.effect_recovery = recovery
    return recovery


def test_recovered_applied_effect_creates_same_verification_work(factory):
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate(timeout=True)
    assert effect.status == EffectStatus.UNKNOWN
    result = configure_recovery(x).recover(effect.effect_id)
    assert result.after_status == EffectStatus.APPLIED
    item = next(w for w in x.work.list(x.case.case_id) if w.reason_code == WorkReason.EFFECT_APPLIED)
    with Session(x.engine) as session, session.begin():
        lock_case(session, x.cases, x.case.case_id)
        duplicate = effect_handoff(session, x.runtime.store.get_ledger(effect.effect_id))
    assert duplicate.work_item_id == item.work_item_id
    assert item.requirement == Q.POST_EFFECT_MESSAGE_STATUS


def test_prepared_effect_is_recovered_before_new_remediation(factory):
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate(prepared_only=True)
    recovery = configure_recovery(x)
    item = create(x, WorkType.RECOVERY_RECHECK, None, WorkReason.EFFECT_UNRESOLVED, source=effect.effect_id)
    assert x.worker.process(x.work.claim(item.work_item_id, "worker"))
    assert x.runtime.store.get_ledger(effect.effect_id).status == EffectStatus.APPLIED
    assert recovery.repository.attempts(effect.effect_id)
    assert any(w.reason_code == WorkReason.EFFECT_APPLIED for w in x.work.list(x.case.case_id))


def test_prepared_without_recovery_capability_blocks_resume(factory):
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate(prepared_only=True)
    item = create(x, WorkType.RECOVERY_RECHECK, None, WorkReason.EFFECT_UNRESOLVED, source=effect.effect_id)
    x.worker.process(x.work.claim(item.work_item_id, "worker"))
    assert x.work.get(item.work_item_id).status == WorkStatus.BLOCKED
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED


def test_unknown_effect_allows_read_but_blocks_conflicting_write(factory):
    from credit_harness.authorization.models import AuthorizationError, AuthorizationCode
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate(timeout=True)
    item = create(x, requirement=Q.POST_EFFECT_MESSAGE_STATUS)
    x.advance(1)
    x.worker.process(x.work.claim(item.work_item_id, "worker"))
    assert x.runtime.store.get_ledger(effect.effect_id).status == EffectStatus.UNKNOWN
    # Existing write fence is consulted by the authorization gateway.
    from credit_harness.recovery.identity import reconstruct_identity
    _, cap, intent = reconstruct_identity(x.runtime.store, x.runtime.store.get_ledger(effect.effect_id))
    with pytest.raises(AuthorizationError) as exc:
        x.auth.issue_capability(intent)
    assert exc.value.code in (AuthorizationCode.WRITE_FENCED_BY_UNRESOLVED_EFFECT, AuthorizationCode.STALE_AUTHORIZATION)


def test_orchestrator_cannot_bypass_write_fence():
    import ast
    from pathlib import Path
    for path in Path("src/credit_harness/orchestration").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in ("issue_capability", "decide_approval", "dispatch", "prepare", "dispatch_prepared")


def test_benchmark_policy_blocked_verification_uses_durable_resume(factory):
    from credit_harness.cases.models import CasePolicyError
    from credit_harness.tools.contracts import ToolQuery
    x = factory(); first, timer = wait(x)
    with pytest.raises(CasePolicyError):
        x.executor.execute(x.case.case_id, Tool.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))
    items = x.worker.handoff.consume(x.evaluate())
    item = next(w for w in items if w.requirement == Q.PAYMENT_FINALITY)
    assert item.not_before >= timer.not_before
    report = x.worker.process(due(x, item))
    assert report.overall_verdict == V.INCONCLUSIVE
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 1
    assert x.work.get(item.work_item_id).status == WorkStatus.COMPLETED


def test_effect_and_work_handoff_roll_back_together(factory, monkeypatch):
    from credit_harness.orchestration import handoff
    from credit_harness.authorization.models import SideEffectReceipt, ReceiptOutcome
    x = factory(ScenarioId.S6); x.read(); effect = x.remediate(prepared_only=True)
    ledger = x.runtime.store.dispatch_prepared(x.capability.payload, lambda:x.clock.now)
    original = handoff.effect_handoff
    def crash(session, ledger):
        original(session, ledger)
        if ledger.status == EffectStatus.APPLIED: raise RuntimeError("synthetic handoff crash")
    monkeypatch.setattr(handoff, "effect_handoff", crash)
    receipt = SideEffectReceipt(correlation_id=ledger.dispatch_correlation_id, outcome=ReceiptOutcome.APPLIED,
        external_effect_ref="SYNTHETIC-EFFECT", observed_at=x.clock.now)
    with pytest.raises(RuntimeError):
        x.runtime.store.transition(ledger.effect_id, EffectStatus.DISPATCHED, EffectStatus.APPLIED, x.clock.now, receipt=receipt)
    assert x.runtime.store.get_ledger(ledger.effect_id).status == EffectStatus.DISPATCHED
    assert not any(w.reason_code == WorkReason.EFFECT_APPLIED for w in x.work.list(x.case.case_id))


def test_stale_worker_cannot_reserve_tool_after_lease_loss(factory):
    from credit_harness.agent.revalidation import execution_precondition
    from credit_harness.cases.preconditions import AgentPreconditionFailed
    from credit_harness.tools.contracts import ToolQuery
    x = factory(); _, item = wait(x); claim = due(x, item)
    x.worker.resume_service.resume(claim)
    fence = x.work.renew(claim)
    case = x.cases.get(x.case.case_id)
    snap = ReasoningContextAssembler().build(case, x.evidence.list(case.case_id))
    precondition = execution_precondition(case, snap).model_copy(update=dict(work_lease=fence))
    x.advance(121); assert x.work.claim(item.work_item_id, "successor")
    with pytest.raises(AgentPreconditionFailed):
        x.executor.execute_if_current(case.case_id, Tool.PAYMENT, ToolQuery(internal_order_id=case.internal_order_id),
            precondition=precondition)
    assert x.cases.get(case.case_id).budget.used_tool_calls == 0


def test_unfenced_dispatch_cannot_enter_claimed_work(factory):
    from credit_harness.cases.preconditions import AgentPreconditionFailed
    from credit_harness.tools.contracts import ToolQuery
    x = factory(); _, item = wait(x); claim = due(x, item)
    x.worker.resume_service.resume(claim)
    with pytest.raises(AgentPreconditionFailed):
        x.executor.execute(x.case.case_id, Tool.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))


def test_evaluation_report_and_handoff_outbox_are_atomic(factory, monkeypatch):
    from credit_harness.orchestration.tables import EvaluationHandoffRow
    from credit_harness.evaluation.tables import EvaluationReportRow
    from sqlalchemy import event
    x = factory(); report = x.evaluator.evaluate(x.case.case_id)
    def crash(*args): raise RuntimeError("synthetic outbox crash")
    event.listen(EvaluationHandoffRow, "before_insert", crash)
    try:
        with pytest.raises(RuntimeError): x.repository.record(report)
    finally:
        event.remove(EvaluationHandoffRow, "before_insert", crash)
    with Session(x.engine) as session:
        assert session.get(EvaluationReportRow, report.evaluation_run_id) is None
        assert session.scalar(select(EvaluationHandoffRow)) is None
    x.repository.record(report)
    assert x.worker.handoff.poll_unhanded_reports()
    assert x.worker.handoff.poll_unhanded_reports() == ()


def test_crash_after_work_commit_replays_report_handoff_once(factory, monkeypatch):
    x = factory(); report = x.evaluate()
    original = x.worker.handoff._ack
    monkeypatch.setattr(x.worker.handoff, "_ack", lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError): x.worker.handoff.consume(report)
    items = x.work.list(x.case.case_id)
    monkeypatch.setattr(x.worker.handoff, "_ack", original)
    x.worker.handoff.poll_unhanded_reports()
    assert x.work.list(x.case.case_id) == items


def test_stale_report_does_not_starve_later_handoff(factory):
    x = factory(); x.evaluate()
    x.read(Tool.PAYMENT); current = x.evaluate()
    assert x.worker.handoff.poll_unhanded_reports(limit=1) == ()
    assert all(w.source_ref == current.report_id for w in x.worker.handoff.poll_unhanded_reports(limit=1))
    assert x.work.list(x.case.case_id)


def test_evaluator_alone_does_not_publish_work(factory):
    from credit_harness.orchestration.tables import EvaluationHandoffRow
    x = factory(); x.evaluator.evaluate(x.case.case_id)
    assert x.work.list(x.case.case_id) == ()
    with Session(x.engine) as session:
        assert session.scalar(select(EvaluationHandoffRow)) is None


def test_unsignaled_operator_work_does_not_starve_due_timer(factory):
    x = factory(); create(x, WorkType.OPERATOR_FOLLOWUP, None, signal=SignalType.OPERATOR_ACKNOWLEDGED)
    x.advance(1)
    timer = create(x, WorkType.INVESTIGATION_RESUME, None, WorkReason.WAIT_REQUESTED, source="due-timer")
    assert x.work.poll_due_work(limit=1) == (timer.work_item_id,)


def test_concurrent_process_same_claim_starts_only_one_run(factory):
    x = factory(); first, item = wait(x); claim = due(x, item); barrier = Barrier(2)
    def run():
        barrier.wait()
        return x.worker.process(claim)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(x.worker.runs(x.case.case_id)) == 1
    assert x.work.state(x.case.case_id).resume_cycle_count == 1


def test_verification_budget_exhaustion_creates_operator_followup(factory):
    x = factory()
    with Session(x.engine) as session, session.begin():
        row = session.get(CaseRow, x.case.case_id)
        row.used_tool_calls = row.max_tool_calls
    item = create(x, requirement=Q.PAYMENT_FINALITY)
    assert x.worker.process(x.work.claim(item.work_item_id, "worker")) is None
    assert x.cases.get(x.case.case_id).status == CaseStatus.ESCALATED
    assert x.evidence.list(x.case.case_id) == ()
    assert any(w.reason_code == WorkReason.TOOL_BUDGET_EXHAUSTED for w in x.work.list(x.case.case_id))
