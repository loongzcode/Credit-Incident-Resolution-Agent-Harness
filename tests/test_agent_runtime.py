import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.test_case_evidence import harness, execute, Q
from credit_harness.adapters.investigation_fake import InvestigationFakePlannerModel
from credit_harness.agent.models import AgentRunConfig, AgentRunStatus as Stop, RevalidationStatus as R, ExecutionCASStatus
from credit_harness.agent.progress import knowledge_progress
from credit_harness.agent.query import build_tool_query
from credit_harness.agent.revalidation import execution_precondition
from credit_harness.agent.runtime import InvestigationAgentRuntime
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.preconditions import AgentPreconditionFailed
from credit_harness.cases.repository import CallState
from credit_harness.cases.tables import CaseCallRow
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ScenarioId, ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.models import HypothesisId as H, HypothesisStatus as S
from credit_harness.identity.models import IdentityMatch
from credit_harness.persistence.store import ObservationRow
from credit_harness.planner.model import FakePlannerModel
from credit_harness.planner.models import (
    CallToolCandidate, WaitCandidate, EscalateCandidate, ModelInputBundle, PlannerDraft,
    PlannerUnavailable, ProposalQuery, ReasonCode, RejectionCode,
)
from credit_harness.planner.service import PlannerService
from credit_harness.tools.contracts import ToolQuery

CASE = "CASE-JD202609100001"


@pytest.fixture
def setup_agent(harness, admin):
    def make(*, scenario=ScenarioId.S6, budget=20, config=None, model=None, clock=True):
        cases, evidence, executor, sid, _ = harness(scenario, budget=budget)
        if clock:
            client = executor._client_for_case(CASE)
            original = client.observe
            def observe(*args, **kwargs):
                admin.advance(sid, 15)  # trusted synthetic world clock, outside agent
                return original(*args, **kwargs)
            client.observe = observe
        runtime = InvestigationAgentRuntime(cases, evidence, ReasoningContextAssembler(),
            PlannerService(model or InvestigationFakePlannerModel()), executor, config=config)
        return runtime
    return make


def payment(bundle):
    target = next(g.gap_id for g in bundle.deterministic_derived.open_evidence_gaps
                  if g.gap_id.endswith(":PAYMENT_FINALITY"))
    return CallToolCandidate(candidate_id="payment", tool_name=T.PAYMENT, target_gap_ids=(target,),
        query=ProposalQuery(internal_order_id=bundle.trusted_control.internal_order_id),
        expected_claim_types=(C.PAYMENT_FINALITY,), reason_summary="支付事实尚未明确。")


def fallback(bundle, kind="wait"):
    target = next(g.gap_id for g in bundle.deterministic_derived.open_evidence_gaps
                  if g.gap_id.endswith(":PAYMENT_FINALITY"))
    common = dict(candidate_id="fallback", target_gap_ids=(target,),
                  reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, reason_summary="等待或升级未决支付事实。")
    return (WaitCandidate(**common, suggested_wait_seconds=60) if kind == "wait"
            else EscalateCandidate(**common))


def model_for(candidate_fn):
    return FakePlannerModel(lambda b: PlannerDraft(snapshot_id=b.snapshot_id,
        candidates=tuple(candidate_fn(b)), observation_summary="observed only", uncertainty_summary="UNKNOWN"))


def replace_after_plan(runtime, fn):
    original = runtime.planner.plan
    def plan(snapshot):
        return fn(original(snapshot), snapshot)
    runtime.planner.plan = plan


def forged_candidate(decision, **updates):
    selected = decision.selected_action
    return decision.model_copy(update={"selected_action": selected.model_copy(update={
        "candidate": selected.candidate.model_copy(update=updates)})})


def calls(runtime):
    with Session(runtime.cases.engine) as session:
        return list(session.scalars(select(CaseCallRow).where(CaseCallRow.case_id == CASE)))


def test_selected_action_is_revalidated_before_execution(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    spy = Mock(wraps=runtime.revalidator.validate)
    runtime.revalidator.validate = spy
    result = runtime.run(CASE)
    assert spy.call_count == 1 and result.tool_calls_dispatched == 1
    assert result.turns[0].runtime_revalidation.status == R.VALID
    assert result.turns[0].execution_cas == ExecutionCASStatus.PASSED


def test_stale_snapshot_never_dispatches(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={"snapshot_id": "0" * 64}))
    result = runtime.run(CASE)
    assert result.status == Stop.STALE_REPLAN_EXHAUSTED
    assert result.tool_calls_dispatched == 0 and not calls(runtime)
    assert len(result.planner_decision_ids) == 1


def test_stale_policy_never_dispatches(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={"policy_version": "obsolete"}))
    result = runtime.run(CASE)
    assert result.turns[0].runtime_revalidation.status == R.STALE_POLICY
    assert result.tool_calls_dispatched == 0 and not calls(runtime)


def test_candidate_is_revalidated_against_fresh_context(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: forged_candidate(d, query=ProposalQuery(internal_order_id="OTHER-ORDER")))
    result = runtime.run(CASE)
    assert result.tool_calls_dispatched == 0
    assert RejectionCode.FOREIGN_ORDER in result.turns[0].runtime_revalidation.rejection_codes


def test_tool_query_is_rebuilt_not_model_dict(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    original = runtime.executor.execute_if_current
    def execute_checked(case_id, tool, query, *, precondition):
        assert type(query) is ToolQuery
        assert set(query.model_dump()) == {"internal_order_id", "protocol_version", "effective_at"}
        assert precondition.expected_snapshot_id
        return original(case_id, tool, query, precondition=precondition)
    runtime.executor.execute_if_current = execute_checked
    result = runtime.run(CASE)
    candidate = result.turns[0].selected_action.candidate
    rebuilt = build_tool_query(runtime.cases.get(CASE), candidate)
    assert rebuilt is not candidate.query and type(candidate.query) is ProposalQuery


def test_only_selected_action_is_executed(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1),
        model=model_for(lambda b: (payment(b), fallback(b))))
    result = runtime.run(CASE)
    assert len(result.turns[0].attempts[0].decision.valid_candidates) == 2
    assert result.final_case_status == CaseStatus.INVESTIGATING
    assert [c.tool for c in calls(runtime)] == [T.PAYMENT.value]


def test_one_tool_per_turn(setup_agent):
    runtime = setup_agent()
    result = runtime.run(CASE)
    assert result.tool_calls_dispatched == sum(t.tool_call_id is not None for t in result.turns)
    assert len({t.tool_call_id for t in result.turns if t.tool_call_id}) == len(calls(runtime))
    assert all(sum(a.execution_cas == ExecutionCASStatus.PASSED for a in t.attempts) <= 1 for t in result.turns)


def test_runtime_never_uses_alias_map_for_query(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: forged_candidate(d, query=ProposalQuery(internal_order_id="EXTREF-001")))
    result = runtime.run(CASE)
    assert not calls(runtime) and result.tool_calls_dispatched == 0
    assert "alias_map" not in Path("src/credit_harness/agent/runtime.py").read_text()


def test_runtime_uses_case_tool_executor(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    runtime.executor.execute = Mock(side_effect=AssertionError("manual path forbidden"))
    runtime.executor.execute_if_current = Mock(wraps=runtime.executor.execute_if_current)
    runtime.run(CASE)
    runtime.executor.execute_if_current.assert_called_once()
    runtime.executor.execute.assert_not_called()


def test_runtime_never_calls_tool_client_directly():
    for path in Path("src/credit_harness/agent").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any(i and any(x in i for x in ("simulator", "persistence.store", "adapters", "vault")) for i in imports)
        assert not any(isinstance(n, ast.Attribute) and n.attr in ("observe", "_client_for_case", "raw_observation")
                       for n in ast.walk(tree))
    for path in Path("src/credit_harness/planner").glob("*.py"):
        assert "credit_harness.agent" not in path.read_text(encoding="utf-8")


def test_concurrent_budget_reservation_only_one_wins(setup_agent):
    runtime = setup_agent(budget=1)
    case, _, snapshot = runtime.load_current_context(CASE)
    precondition = execution_precondition(case, snapshot)
    barrier = Barrier(2)
    def dispatch():
        barrier.wait(timeout=10)
        try:
            return runtime.executor.execute_if_current(CASE, T.PAYMENT, Q, precondition=precondition).call_id
        except AgentPreconditionFailed:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert sum(r is not None for r in results) == 1
    assert runtime.cases.get(CASE).budget.used_tool_calls == 1
    assert len(calls(runtime)) == len(runtime.evidence.observations(CASE)) == 1


def test_old_execution_precondition_cannot_dispatch(setup_agent):
    runtime = setup_agent()
    case, _, snapshot = runtime.load_current_context(CASE)
    precondition = execution_precondition(case, snapshot)
    runtime.executor.execute_if_current(CASE, T.PAYMENT, Q, precondition=precondition)
    with pytest.raises(AgentPreconditionFailed):
        runtime.executor.execute_if_current(CASE, T.PAYMENT, Q, precondition=precondition)
    assert len(calls(runtime)) == 1


@pytest.mark.parametrize("status", [CaseStatus.WAITING, CaseStatus.ESCALATED])
def test_lifecycle_race_blocks_dispatch(setup_agent, status):
    runtime = setup_agent()
    original = runtime.executor.execute_if_current
    def racing(*args, **kwargs):
        runtime.cases.pause(CASE, status)
        return original(*args, **kwargs)
    runtime.executor.execute_if_current = racing
    result = runtime.run(CASE)
    assert not calls(runtime) and result.tool_calls_dispatched == 0
    assert result.final_case_status == status
    assert result.turns[0].attempts[0].execution_cas == ExecutionCASStatus.FAILED


def test_case_waiting_race_blocks_tool(setup_agent):
    test_lifecycle_race_blocks_dispatch(setup_agent, CaseStatus.WAITING)


def test_case_escalated_race_blocks_tool(setup_agent):
    test_lifecycle_race_blocks_dispatch(setup_agent, CaseStatus.ESCALATED)


def test_evidence_change_between_plan_and_execute_forces_replan(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    changed = False
    def change(d, snapshot):
        nonlocal changed
        if not changed:
            changed = True
            execute(runtime.executor, T.FUND)
        return d
    replace_after_plan(runtime, change)
    result = runtime.run(CASE)
    attempts = result.turns[0].attempts
    assert attempts[0].revalidation.status == R.STALE_SNAPSHOT
    assert attempts[0].execution_cas == ExecutionCASStatus.NOT_ATTEMPTED
    assert len(attempts) == 2 and attempts[1].snapshot_id != attempts[0].snapshot_id
    assert result.tool_calls_dispatched == 1


def test_budget_change_between_plan_and_execute_forces_replan(setup_agent):
    runtime = setup_agent()
    changed = False
    def change(d, _):
        nonlocal changed
        if not changed:
            changed = True
            runtime.cases.reserve_call(CASE, T.FUND, Q)
        return d
    replace_after_plan(runtime, change)
    result = runtime.run(CASE)
    assert result.turns[0].attempts[0].revalidation.status == R.STALE_SNAPSHOT
    assert result.tool_calls_dispatched == 0
    assert result.final_case_status == CaseStatus.ESCALATED


def test_evidence_publication_after_fresh_snapshot_invalidates_cas(setup_agent):
    runtime = setup_agent()
    # Reserve a call before the snapshot, then receive its response after it.
    _, pending = runtime.cases.reserve_call(CASE, T.FUND, Q)
    case, _, snapshot = runtime.load_current_context(CASE)
    precondition = execution_precondition(case, snapshot)
    obs = runtime.executor._client_for_case(CASE).observe(T.FUND, Q, dispatch_correlation_id=pending)
    runtime.evidence.record_call(CASE, pending, obs)
    assert runtime.cases.get(CASE).budget.used_tool_calls == precondition.expected_used_tool_calls
    assert runtime.cases.get(CASE).updated_at > precondition.expected_case_updated_at
    with pytest.raises(AgentPreconditionFailed):
        runtime.executor.execute_if_current(CASE, T.PAYMENT, Q, precondition=precondition)
    assert len(calls(runtime)) == 1


def test_wait_does_not_sleep(setup_agent, monkeypatch):
    # Patch only the agent module's potential dependency, not test HTTP internals.
    import credit_harness.agent.runtime as module
    monkeypatch.setattr(module, "sleep", Mock(side_effect=AssertionError("no sleep")), raising=False)
    runtime = setup_agent(model=model_for(lambda b: (fallback(b),)))
    result = runtime.run(CASE)
    assert result.status == Stop.WAITING
    module.sleep.assert_not_called()


def test_wait_does_not_schedule():
    source = Path("src/credit_harness/agent/runtime.py").read_text()
    tree = ast.parse(source)
    assert not any(isinstance(n, ast.Attribute) and n.attr in ("sleep", "call_later", "create_task", "schedule")
                   for n in ast.walk(tree))


def test_wait_pauses_case(setup_agent):
    runtime = setup_agent(model=model_for(lambda b: (fallback(b),)))
    result = runtime.run(CASE)
    assert result.status == Stop.WAITING and result.final_case_status == CaseStatus.WAITING
    assert result.turns[0].selected_action.candidate.suggested_wait_seconds == 60
    assert not calls(runtime) and runtime.cases.get(CASE).budget.used_tool_calls == 0


def test_escalate_pauses_case(setup_agent):
    runtime = setup_agent(model=model_for(lambda b: (fallback(b, "escalate"),)))
    result = runtime.run(CASE)
    assert result.status == Stop.ESCALATED and result.final_case_status == CaseStatus.ESCALATED
    assert result.turns[0].selected_action.candidate.reason_code == ReasonCode.INSUFFICIENT_EVIDENCE
    assert not calls(runtime)


@pytest.mark.parametrize("kind", ["wait", "escalate"])
def test_stale_lifecycle_proposal_does_not_pause(setup_agent, kind):
    runtime = setup_agent(model=model_for(lambda b: (fallback(b, kind),)))
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={"snapshot_id": "0" * 64}))
    result = runtime.run(CASE)
    assert result.final_case_status == CaseStatus.NEW and not calls(runtime)


def test_wait_stale_snapshot_does_not_pause_case(setup_agent):
    test_stale_lifecycle_proposal_does_not_pause(setup_agent, "wait")


def test_escalate_stale_snapshot_does_not_pause_case(setup_agent):
    test_stale_lifecycle_proposal_does_not_pause(setup_agent, "escalate")


def test_agent_never_closes_case(setup_agent):
    runtime = setup_agent()
    result = runtime.run(CASE)
    assert result.final_case_status != CaseStatus.CLOSED
    assert all(t.case_status_after != CaseStatus.CLOSED for t in result.turns)


def test_planner_unavailable_executes_no_tool(setup_agent):
    def fail(_):
        raise PlannerUnavailable("offline")
    runtime = setup_agent(model=FakePlannerModel(fail))
    original = runtime.cases.get(CASE)
    result = runtime.run(CASE)
    assert result.status == Stop.PLANNER_UNAVAILABLE and result.tool_calls_dispatched == 0
    assert runtime.cases.get(CASE) == original and runtime.evidence.list(CASE) == ()


def test_empty_selected_action_executes_no_tool(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={"selected_action": None}))
    result = runtime.run(CASE)
    assert result.status == Stop.SAFE_NO_ACTION and not calls(runtime)
    assert runtime.cases.get(CASE).budget.used_tool_calls == 0


@pytest.fixture
def failed_transport(setup_agent):
    runtime = setup_agent()
    client = runtime.executor._client_for_case(CASE)
    client.observe = Mock(side_effect=TimeoutError("synthetic transport failure"))
    result = runtime.run(CASE)
    return runtime, client, result


def test_tool_transport_exception_not_blindly_retried(failed_transport):
    runtime, client, result = failed_transport
    client.observe.assert_called_once()
    assert result.status == Stop.TOOL_EXECUTION_ERROR and result.tool_calls_dispatched == 1
    assert result.turns[0].tool_call_id == calls(runtime)[0].call_id


def test_transport_exception_consumes_reserved_budget(failed_transport):
    runtime, _, result = failed_transport
    assert runtime.cases.get(CASE).budget.used_tool_calls == 1
    assert calls(runtime)[0].state == CallState.ERROR
    assert result.turns[0].execution_cas == ExecutionCASStatus.PASSED


def test_transport_exception_manufactures_no_business_evidence(failed_transport):
    runtime, _, result = failed_transport
    assert runtime.evidence.list(CASE) == () and runtime.evidence.observations(CASE) == ()
    _, _, fresh = runtime.load_current_context(CASE)
    assert not fresh.history_digest.lookup_history_complete
    assert fresh.financial_identity.result == IdentityMatch.UNKNOWN
    assert result.final_snapshot_id == fresh.snapshot_id
    assert not result.turns[0].knowledge_progress.has_progress


def test_no_knowledge_progress_stops_loop(setup_agent):
    runtime = setup_agent(scenario=ScenarioId.S8, clock=False,
        config=AgentRunConfig(max_consecutive_no_knowledge_progress=1))
    result = runtime.run(CASE)
    assert result.status == Stop.NO_KNOWLEDGE_PROGRESS and result.tool_calls_dispatched == 2
    assert not result.turns[-1].new_evidence_refs
    assert not result.turns[-1].knowledge_progress.has_progress


def test_max_turns_stops_loop(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=2))
    result = runtime.run(CASE)
    assert result.status == Stop.MAX_TURNS_REACHED
    assert result.turn_count == result.tool_calls_dispatched == 2


def test_stale_replan_limit_stops_loop(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_stale_replans_per_turn=2))
    def change(d, _):
        runtime.cases.reserve_call(CASE, T.FUND, Q)
        return d
    replace_after_plan(runtime, change)
    result = runtime.run(CASE)
    assert result.status == Stop.STALE_REPLAN_EXHAUSTED
    assert len(result.turns[0].attempts) == 3
    assert result.tool_calls_dispatched == 0 and len(result.planner_decision_ids) == 3


def test_budget_only_change_is_not_knowledge_progress(setup_agent):
    runtime = setup_agent()
    _, _, before = runtime.load_current_context(CASE)
    runtime.cases.reserve_call(CASE, T.PAYMENT, Q)
    _, _, after = runtime.load_current_context(CASE)
    delta = knowledge_progress(before, after)
    assert before.snapshot_id != after.snapshot_id and delta.hypothesis_graph_fingerprint_changed
    assert not delta.has_progress


def test_tool_ok_does_not_mean_business_success(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    result = runtime.run(CASE)
    assert runtime.evidence.observations(CASE)[0].observation.status.value == "OK"
    assert result.status == Stop.MAX_TURNS_REACHED and result.final_case_status == CaseStatus.INVESTIGATING
    assert result.turns[0].knowledge_progress.after.identity == IdentityMatch.UNKNOWN


@pytest.fixture
def observed_model(setup_agent):
    inputs = []
    model = InvestigationFakePlannerModel()
    def plan(bundle):
        assert type(bundle) is ModelInputBundle
        inputs.append(bundle)
        return model.plan(bundle)
    runtime = setup_agent(model=FakePlannerModel(plan))
    return runtime, inputs


def test_observation_never_goes_directly_to_planner(observed_model):
    runtime, inputs = observed_model
    runtime.run(CASE)
    assert len(inputs) > 1
    for bundle in inputs:
        assert type(bundle) is ModelInputBundle
        assert not {"observation", "raw_callback", "previous_summary", "conversation", "alias_map"} & bundle.model_dump().keys()


def test_planner_only_receives_rebuilt_context(observed_model):
    runtime, inputs = observed_model
    rebuilt = []
    original = runtime.assembler.build
    def build(case, evidence):
        snapshot = original(case, evidence)
        rebuilt.append(snapshot.snapshot_id)
        return snapshot
    runtime.assembler.build = build
    result = runtime.run(CASE)
    assert all(b.snapshot_id in rebuilt for b in inputs)
    assert len(rebuilt) >= 2 * result.turn_count


def test_new_evidence_changes_next_planner_input(observed_model):
    runtime, inputs = observed_model
    result = runtime.run(CASE)
    assert not inputs[0].untrusted_external_data.current_facts
    assert any(f.claim_type == C.PAYMENT_FINALITY for f in inputs[1].untrusted_external_data.current_facts)
    assert inputs[0].snapshot_id != inputs[1].snapshot_id
    assert result.turns[0].after_snapshot_id == inputs[1].snapshot_id


def test_unknown_remains_unknown_after_timeout(setup_agent):
    runtime = setup_agent(scenario=ScenarioId.S8)
    result = runtime.run(CASE)
    assert all(t.knowledge_progress.after.identity == IdentityMatch.UNKNOWN for t in result.turns)
    assert not any(e.claim_type == C.PAYMENT_FINALITY for e in runtime.evidence.list(CASE))


def test_hypothesis_changes_only_via_evidence_engine(setup_agent):
    runtime = setup_agent()
    result = runtime.run(CASE)
    graph = HypothesisEngine().evaluate(runtime.cases.get(CASE), runtime.evidence.list(CASE))
    observed = {h.hypothesis_id: h.status for h in result.turns[-1].knowledge_progress.after.hypotheses}
    assert observed == {h.hypothesis_id: h.status for h in graph.hypotheses}
    assert all(not e.claim_type.value.startswith("HYPOTHESIS") for e in runtime.evidence.list(CASE))


@pytest.fixture
def s6_run(setup_agent):
    runtime = setup_agent()
    return runtime, runtime.run(CASE)


def test_s6_agent_runs_multiple_turns(s6_run):
    runtime, result = s6_run
    assert result.turn_count > 3 and result.tool_calls_dispatched > 2
    assert len(runtime.trace_store.records) == 1
    assert any(t.attempts[0].decision.rejected_candidates for t in result.turns)


def test_s6_payment_evidence_changes_identity(s6_run):
    _, result = s6_run
    assert result.turns[0].knowledge_progress.before.identity == IdentityMatch.UNKNOWN
    assert result.turns[-1].knowledge_progress.after.identity == IdentityMatch.MATCH
    assert any(t.knowledge_progress.identity_changed for t in result.turns)


def test_s6_callback_evidence_changes_hypotheses(s6_run):
    _, result = s6_run
    callback = next(t for t in result.turns if t.tool_name == T.CALLBACK)
    after = {h.hypothesis_id: h.status for h in callback.knowledge_progress.after.hypotheses}
    assert after[H.H5] == S.ELIMINATED and after[H.H6] == S.SUPPORTED


def test_s6_message_evidence_confirms_h6_schema(s6_run):
    _, result = s6_run
    messages = next(t for t in result.turns if t.tool_name == T.MESSAGES)
    after = {h.hypothesis_id: h.status for h in messages.knowledge_progress.after.hypotheses}
    assert after[H.H4] == after[H.H6] == after[H.H6_SCHEMA_MISMATCH] == S.CONFIRMED


def test_s6_agent_stops_without_closing_case(s6_run):
    runtime, result = s6_run
    assert result.status in (Stop.ESCALATED, Stop.SAFE_NO_ACTION)
    assert runtime.cases.get(CASE).status != CaseStatus.CLOSED
    assert any(g.endswith(":DEPLOYED_CONSUMER_SCHEMA_VERSION") for g in result.turns[-1].knowledge_progress.after.open_gap_ids)


@pytest.fixture
def s8_run(setup_agent):
    runtime = setup_agent(scenario=ScenarioId.S8)
    return runtime, runtime.run(CASE)


def test_s8_agent_does_not_loop_forever(s8_run):
    _, result = s8_run
    assert result.turn_count <= 12 and result.status in (Stop.WAITING, Stop.ESCALATED)


def test_s8_timeout_remains_unknown(s8_run):
    runtime, result = s8_run
    assert result.turns[-1].knowledge_progress.after.identity == IdentityMatch.UNKNOWN
    assert all(e.claim_type == C.SOURCE_LOOKUP_STATUS for e in runtime.evidence.list(CASE))
    assert all(e.value == "TIMEOUT" for e in runtime.evidence.list(CASE))


def test_s8_repeated_payment_eventually_waits_or_escalates(s8_run):
    runtime, result = s8_run
    assert result.status in (Stop.WAITING, Stop.ESCALATED)
    assert len([c for c in calls(runtime) if c.tool == T.PAYMENT.value]) == 3
    assert result.turns[-1].tool_call_id is None


def test_s8_does_not_create_new_financial_intent(setup_agent, admin):
    runtime = setup_agent(scenario=ScenarioId.S8)
    # Oracle is deliberately restricted to the test, never a runtime dependency.
    from tests.support.inspector import world_state
    sid = runtime.cases.get(CASE).simulation_id
    before = world_state(runtime.cases.engine, sid)
    runtime.run(CASE)
    after = world_state(runtime.cases.engine, sid)
    assert before.disbursement_intent_count == after.disbursement_intent_count


def test_s8_respects_tool_budget(setup_agent):
    runtime = setup_agent(scenario=ScenarioId.S8, budget=2)
    result = runtime.run(CASE)
    assert result.tool_calls_dispatched == runtime.cases.get(CASE).budget.used_tool_calls == 2
    assert result.status == Stop.ESCALATED


def test_selected_proposal_policy_is_checked_independently(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={
        "selected_action": d.selected_action.model_copy(update={"policy_version": "old"})}))
    result = runtime.run(CASE)
    assert result.turns[0].runtime_revalidation.status == R.STALE_POLICY
    assert not calls(runtime)


def test_selected_proposal_snapshot_is_checked_independently(setup_agent):
    runtime = setup_agent()
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={
        "selected_action": d.selected_action.model_copy(update={"snapshot_id": "0" * 64})}))
    result = runtime.run(CASE)
    assert result.turns[0].runtime_revalidation.status == R.STALE_SNAPSHOT and not calls(runtime)


def test_cached_valid_candidates_do_not_authorize_policy_violation(setup_agent):
    runtime = setup_agent(scenario=ScenarioId.S8)
    for _ in range(3):
        execute(runtime.executor, T.PAYMENT)
    case, _, snapshot = runtime.load_current_context(CASE)
    from credit_harness.planner.renderer import ModelInputRenderer
    candidate = payment(ModelInputRenderer().render(snapshot))
    from credit_harness.planner.models import ValidatedActionProposal
    # Corrupt a trusted service output to exercise independent runtime defense.
    replace_after_plan(runtime, lambda d, _: d.model_copy(update={
        "selected_action": ValidatedActionProposal(snapshot_id=d.snapshot_id, candidate=candidate),
        "valid_candidates": (ValidatedActionProposal(snapshot_id=d.snapshot_id, candidate=candidate),)}))
    result = runtime.run(CASE)
    assert result.tool_calls_dispatched == 0 and len(calls(runtime)) == 3
    assert RejectionCode.REPEATED_NO_NEW_INFORMATION in result.turns[0].runtime_revalidation.rejection_codes


@pytest.mark.parametrize("status", [CaseStatus.WAITING, CaseStatus.ESCALATED])
def test_pause_cas_rejects_evidence_publication_without_budget_change(setup_agent, status):
    runtime = setup_agent()
    _, pending = runtime.cases.reserve_call(CASE, T.FUND, Q)
    case, _, snapshot = runtime.load_current_context(CASE)
    precondition = execution_precondition(case, snapshot)
    obs = runtime.executor._client_for_case(CASE).observe(T.FUND, Q, dispatch_correlation_id=pending)
    runtime.evidence.record_call(CASE, pending, obs)
    with pytest.raises(AgentPreconditionFailed):
        runtime.cases.pause_if_current(CASE, status, precondition=precondition)
    assert runtime.cases.get(CASE).status == CaseStatus.INVESTIGATING


def test_concurrent_same_precondition_one_wins_with_remaining_budget(setup_agent):
    runtime = setup_agent(budget=20)
    case, _, snapshot = runtime.load_current_context(CASE)
    precondition = execution_precondition(case, snapshot)
    barrier = Barrier(2)
    def reserve():
        barrier.wait(timeout=10)
        try:
            runtime.cases.reserve_call(CASE, T.PAYMENT, Q, precondition=precondition)
            return True
        except AgentPreconditionFailed:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(results) == 1 and runtime.cases.get(CASE).budget.used_tool_calls == 1


def test_execution_precondition_is_mandatory_on_agent_path(setup_agent):
    runtime = setup_agent()
    with pytest.raises(AgentPreconditionFailed):
        runtime.executor.execute_if_current(CASE, T.PAYMENT, Q, precondition=None)
    assert not calls(runtime)


def test_precondition_cannot_cross_tenant_or_case(setup_agent):
    runtime = setup_agent()
    case, _, snapshot = runtime.load_current_context(CASE)
    precondition = execution_precondition(case, snapshot)
    for update in ({"tenant_id": "other"}, {"case_id": "OTHER-CASE"}):
        with pytest.raises(AgentPreconditionFailed):
            runtime.executor.execute_if_current(CASE, T.PAYMENT, Q,
                precondition=precondition.model_copy(update=update))
    assert not calls(runtime)


def test_response_lost_after_observation_commit_is_not_recovered_or_retried(setup_agent):
    runtime = setup_agent()
    client = runtime.executor._client_for_case(CASE)
    original = client.observe
    def lost(*args, **kwargs):
        original(*args, **kwargs)  # persists genuine ObservationRow
        raise TimeoutError("response lost after commit")
    client.observe = Mock(side_effect=lost)
    result = runtime.run(CASE)
    client.observe.assert_called_once()
    assert result.status == Stop.TOOL_EXECUTION_ERROR and not runtime.evidence.list(CASE)
    row = calls(runtime)[0]
    assert row.state == CallState.ERROR and row.observation_id is None
    with Session(runtime.cases.engine) as session:
        orphan = session.scalar(select(ObservationRow).where(ObservationRow.dispatch_correlation_id == row.call_id))
        assert orphan is not None
    assert result.turns[0].new_evidence_refs == ()


def test_provider_failure_after_success_keeps_prior_evidence(setup_agent):
    model = InvestigationFakePlannerModel()
    attempts = 0
    def plan(bundle):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise PlannerUnavailable("provider unavailable")
        return model.plan(bundle)
    runtime = setup_agent(model=FakePlannerModel(plan))
    result = runtime.run(CASE)
    assert result.status == Stop.PLANNER_UNAVAILABLE and result.tool_calls_dispatched == 1
    assert len(calls(runtime)) == 1 and runtime.evidence.list(CASE)
    assert result.final_case_status == CaseStatus.INVESTIGATING


def test_callback_gap_can_be_investigated_before_first_callback_lookup(setup_agent):
    runtime = setup_agent()
    execute(runtime.executor, T.TRACE)
    execute(runtime.executor, T.PAYMENT)
    _, _, snapshot = runtime.load_current_context(CASE)
    assert any(g.gap_id.endswith(":CALLBACK_GATEWAY_OBSERVATION") for g in snapshot.open_evidence_gaps)
    assert not any(e.tool == T.CALLBACK for e in runtime.evidence.list(CASE))


def test_agent_trace_schema_excludes_observation_and_oracle():
    import json
    from credit_harness.agent.models import AgentRunResult
    schema = json.dumps(AgentRunResult.model_json_schema())
    for field in ("WorldState", "GroundTruth", "raw_callback", "scenario_id", "Observation", "alias_map"):
        assert field not in schema


def test_investigation_fake_has_no_scenario_input_or_fixed_turn_index():
    source = Path("src/credit_harness/adapters/investigation_fake.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"ScenarioId", "WorldState", "GroundTruth", "turn_number", "SimulatorAdmin"}
    assert all(not isinstance(n, ast.ImportFrom) or not n.module or "simulator" not in n.module for n in ast.walk(tree))


def test_case_revision_advances_with_frozen_clock(setup_agent, monkeypatch):
    runtime = setup_agent()
    case = runtime.cases.get(CASE)
    monkeypatch.setattr("credit_harness.cases.repository.utc_now", lambda: case.updated_at)
    runtime.executor.execute_if_current(CASE, T.PAYMENT, Q,
        precondition=execution_precondition(case, runtime.load_current_context(CASE)[2]))
    newer = runtime.cases.get(CASE)
    assert newer.updated_at > case.updated_at
    runtime.cases.pause_if_current(CASE, CaseStatus.WAITING,
        precondition=execution_precondition(newer, runtime.load_current_context(CASE)[2]))
    assert runtime.cases.get(CASE).updated_at > newer.updated_at
