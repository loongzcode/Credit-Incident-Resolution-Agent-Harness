import ast
import inspect
import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.budget import MandatoryContextOverflow, referenced_ids, snapshot_digest
from credit_harness.context.eligibility import ContextEligibilityPolicy, ContextEligibilityError, MandatoryContextFactPolicy
from credit_harness.context.models import (
    COMPACTION_POLICY_VERSION, CONTEXT_SCHEMA_VERSION, ELIGIBILITY_POLICY_VERSION,
    ContextBudget, InformationClass, OmissionReason, ReasoningContextSnapshot,
)
from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
from credit_harness.domain.enums import Completeness, FaultKind, Freshness, ScenarioId, ToolName as T
from credit_harness.evidence.models import ClaimType as C, Evidence
from credit_harness.hypotheses.catalog import HYPOTHESIS_RULESET_VERSION
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.models import HypothesisId as H, HypothesisStatus as S, PriorityClass as P
from credit_harness.identity.models import IdentityMatch as M
from credit_harness.simulator.faults import ObservationFault
from credit_harness.simulator.scenarios import T0
from tests.test_case_evidence import harness, execute, FORBIDDEN_KEYS
from tests.test_hypotheses import changed, investigate, status


def build(harness):
    case, evidence, _, executor, repository, sid = investigate(harness)
    return ReasoningContextAssembler().build(case, evidence), case, evidence, executor, repository, sid


@pytest.mark.parametrize("remove", [(C.CALLBACK_GATEWAY_RECEIVED,),
                                  (C.CALLBACK_GATEWAY_RECEIVED, C.CALLBACK_PROTOCOL_VERSION)])
def test_stale_schema_support_requires_parent_gateway_witness(harness, remove):
    case, evidence, _, _, _, _ = investigate(harness)
    subset = tuple(e for e in evidence if e.claim_type not in remove)
    graph = HypothesisEngine().evaluate(case, subset)
    assert status(graph, H.H6_STALE_CONSUMER_SCHEMA) != S.SUPPORTED


def test_stale_schema_support_contains_parent_evidence(harness):
    _, evidence, graph, _, _, _ = investigate(harness)
    child = next(h for h in graph.hypotheses if h.hypothesis_id == H.H6_STALE_CONSUMER_SCHEMA)
    assert {C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS} <= {
        e.claim_type for e in evidence if e.evidence_id in child.supporting_evidence_refs}


def test_context_assembler_accepts_only_case_and_evidence(harness):
    snapshot, case, evidence, executor, _, _ = build(harness)
    assembler = ReasoningContextAssembler()
    assert tuple(inspect.signature(assembler.build).parameters) == ("case", "evidence")
    for kwargs in ({"previous_context": snapshot}, {"previous_summary": "text"},
                   {"previous_hypothesis_graph": HypothesisEngine().evaluate(case, evidence)}):
        with pytest.raises(TypeError):
            assembler.build(case, evidence, **kwargs)
    observation = execute(executor, T.FUND).observation
    with pytest.raises(TypeError):
        assembler.build(case, [observation])
    with pytest.raises(TypeError):
        assembler.build(case.model_dump(), evidence)


def test_context_recomputes_hypothesis_graph(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    assembler = ReasoningContextAssembler()
    partial = tuple(e for e in evidence if e.claim_type != C.PAYMENT_ACCOUNT_REF)
    result = assembler.build(case, partial)
    assert result.financial_identity.result == M.UNKNOWN
    assert not any(h.hypothesis_id == H.H4 and h.status == S.CONFIRMED for h in result.active_hypotheses)
    assert result.hypothesis_input_fingerprint != snapshot.hypothesis_input_fingerprint
    assert assembler.build(case, evidence) == snapshot


def test_context_snapshot_is_deterministic(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    assert snapshot == ReasoningContextAssembler().build(case, tuple(reversed(evidence)))
    assert snapshot == ReasoningContextAssembler().build(case, (*evidence, *evidence))


def test_same_input_same_snapshot(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    assert snapshot.model_dump_json() == ReasoningContextAssembler().build(case, evidence).model_dump_json()
    assert snapshot.snapshot_id == snapshot_digest(snapshot)
    with pytest.raises(ValidationError):
        snapshot.case_id = "OTHER"


def test_context_has_rule_and_policy_versions(harness):
    snapshot, _, _, _, _, _ = build(harness)
    assert snapshot.context_schema_version == CONTEXT_SCHEMA_VERSION
    assert snapshot.eligibility_policy_version == ELIGIBILITY_POLICY_VERSION
    assert snapshot.compaction_policy_version == COMPACTION_POLICY_VERSION
    assert snapshot.hypothesis_rule_version == HYPOTHESIS_RULESET_VERSION


def test_policy_change_changes_snapshot_identity(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    larger = ReasoningContextAssembler(budget=ContextBudget(max_serialized_chars=90000)).build(case, evidence)
    assert snapshot.snapshot_id != larger.snapshot_id and snapshot.policy_fingerprint != larger.policy_fingerprint


def test_raw_pii_type_cannot_enter_context(harness):
    snapshot, _, _, _, _, _ = build(harness)
    from credit_harness.identity.vault import synthetic_identity_fixture
    for value in (synthetic_identity_fixture(), SecretStr("TEST-ID-0001")):
        with pytest.raises(ValidationError):
            type(snapshot.current_facts[0]).model_validate({**snapshot.current_facts[0].model_dump(), "value": value})
    with pytest.raises(ValidationError):
        ReasoningContextSnapshot.model_validate({**snapshot.model_dump(), "raw_text": "TEST-ID-0001"})
    schema = json.dumps(ReasoningContextSnapshot.model_json_schema())
    for word in ("SyntheticIdentityRecord", "SecretStr", "PIIField", "RawObservation", "WorldState", "GroundTruth"):
        assert word not in schema


def test_raw_pii_value_never_serializes(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    forbidden = ("TEST-ID-0001", "TEST-CARD-0001", "TEST-MOBILE-0001",
                 "310101199001011234", "6222021234567890123", "13800138000")
    # Poison an optional structured protocol value, not an invented raw-text input interface.
    original = next(e for e in evidence if e.claim_type == C.PROTOCOL_BUSINESS_SEMANTICS and e.subject.field == "FAILED")
    for raw in forbidden:
        result = ReasoningContextAssembler().build(case, tuple(changed(e, value=raw) if e is original else e for e in evidence))
        assert raw not in result.model_dump_json()
        assert any(o.reason == OmissionReason.ELIGIBILITY_DENIED for o in result.omitted_evidence_summary.omitted)
    for word in forbidden:
        assert word not in snapshot.model_dump_json()


def test_eligibility_precedes_size_and_mandatory_denial_fails_closed(harness):
    _, case, evidence, _, _, _ = build(harness)
    policy = ContextEligibilityPolicy(denied_claims={C.PAYMENT_BENEFICIARY_REF})
    with pytest.raises(ContextEligibilityError):
        ReasoningContextAssembler(budget=ContextBudget(max_serialized_chars=1000000), eligibility=policy).build(case, evidence)
    for kind in (InformationClass.RAW_PII, InformationClass.MASKED_PII, InformationClass.ORACLE):
        assert not policy.allows_class(kind)


def test_oracle_never_enters_context(harness, engine):
    from tests.support.inspector import world_state, ground_truth
    snapshot, case, evidence, _, _, sid = build(harness)
    for oracle in (world_state(engine, sid), ground_truth(engine, sid)):
        with pytest.raises(TypeError):
            ReasoningContextAssembler().build(case, [oracle])
    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value))
        return set()
    assert not keys(snapshot.model_dump(mode="json")).intersection((*FORBIDDEN_KEYS, "tenant_id", "simulation_id", "raw_ref"))


def test_identity_match_is_first_class_context(harness):
    snapshot, _, _, _, _, _ = build(harness)
    assert snapshot.financial_identity.result == M.MATCH
    assert snapshot.financial_identity.transaction_refs == ("PAY-001",)
    assert snapshot.financial_identity.evidence_refs


def test_identity_mismatch_is_mandatory(harness):
    _, case, evidence, _, _, _ = build(harness)
    evidence = tuple(changed(e, value="BEN-OTHER") if e.claim_type == C.PAYMENT_BENEFICIARY_REF else e for e in evidence)
    budget = ContextBudget(max_hypothesis_capsules=2, max_history_items=0)
    snapshot = ReasoningContextAssembler(budget=budget).build(case, evidence)
    assert snapshot.financial_identity.result == M.MISMATCH
    assert snapshot.financial_identity.mismatch_dimensions == ("BENEFICIARY",)
    assert any(f.claim_type == C.PAYMENT_BENEFICIARY_REF and f.value == "BEN-OTHER" for f in snapshot.current_facts)


def test_identity_unknown_is_not_false(harness):
    case, evidence, _, _, _, _ = investigate(harness, ScenarioId.S8, tools=(T.FUND, T.PAYMENT))
    snapshot = ReasoningContextAssembler().build(case, evidence)
    assert snapshot.financial_identity.result == M.UNKNOWN
    assert snapshot.financial_identity.unknown_dimensions


def test_safety_critical_gaps_never_omitted(harness):
    case, evidence, graph, _, _, _ = investigate(harness, ScenarioId.S8, tools=(T.FUND, T.PAYMENT))
    snapshot = ReasoningContextAssembler(budget=ContextBudget(max_gap_capsules=2, max_hypothesis_capsules=0, max_history_items=0)).build(case, evidence)
    assert {g.gap_id for g in graph.open_gaps if g.priority_class == P.SAFETY_CRITICAL} == {
        g.gap_id for g in snapshot.open_evidence_gaps}


@pytest.mark.parametrize("kwargs", [{"max_serialized_chars": 10}, {"max_fact_capsules": 0},
                                  {"max_hypothesis_capsules": 0}, {"max_gap_capsules": 0}])
def test_mandatory_context_overflow_fails_closed(harness, kwargs):
    _, case, evidence, _, _, _ = build(harness)
    with pytest.raises(MandatoryContextOverflow):
        ReasoningContextAssembler(budget=ContextBudget(**kwargs)).build(case, evidence)


def test_repeated_lookup_is_compacted(harness, admin):
    cases, repository, executor, sid, _ = harness(ScenarioId.S5)
    for _ in range(10):
        execute(executor, T.CALLBACK)
        admin.advance(sid, 1)
    snapshot = ReasoningContextAssembler().build(cases.get("CASE-JD202609100001"), repository.list("CASE-JD202609100001"))
    group, = snapshot.history_digest.repeated_lookup_groups
    assert group.status == "NOT_FOUND" and group.references.count == 10
    assert group.first_observed_at < group.last_observed_at
    assert len(snapshot.selected_evidence_refs) == 2
    assert not any(h.hypothesis_id == H.H5 and h.status == S.CONFIRMED for h in snapshot.active_hypotheses)


def test_historical_superseded_state_is_not_current(harness, admin):
    cases, repository, executor, sid, _ = harness(ScenarioId.S7)
    admin.add_fault(sid, ObservationFault(tool=T.GUARANTEE, kind=FaultKind.OLD_CACHE, starts_at=T0,
                                         ends_at=T0+timedelta(seconds=11), cached_revision=0))
    execute(executor, T.GUARANTEE)
    admin.advance(sid, 1)
    execute(executor, T.GUARANTEE)
    snapshot = ReasoningContextAssembler().build(cases.get("CASE-JD202609100001"), repository.list("CASE-JD202609100001"))
    assert {f.value for f in snapshot.current_facts if f.claim_type == C.GUARANTEE_STATUS} == {"SUCCESS"}
    assert any(h.last_observed_value == "PROCESSING" for h in snapshot.history_digest.state_transitions)


def test_new_timeout_invalidates_old_current_payment_in_context(harness, admin):
    _, case, evidence, executor, repository, sid = build(harness)
    admin.advance(sid, 1)
    admin.add_fault(sid, ObservationFault(tool=T.PAYMENT, kind=FaultKind.TIMEOUT, starts_at=T0))
    execute(executor, T.PAYMENT)
    snapshot = ReasoningContextAssembler().build(case, repository.list(case.case_id))
    assert snapshot.financial_identity.result == M.UNKNOWN
    assert not any(f.claim_type == C.PAYMENT_FINALITY for f in snapshot.current_facts)
    assert any(h.claim_type == C.PAYMENT_FINALITY and h.last_observed_value == "SETTLED" for h in snapshot.history_digest.state_transitions)
    assert {"PAYMENT_FINALITY", "PAYMENT_IDENTITY"} <= {g.gap_id.split(":")[-1] for g in snapshot.open_evidence_gaps}


def test_eliminated_hypothesis_is_compacted(harness):
    snapshot, _, _, _, _, _ = build(harness)
    h = next(h for h in snapshot.resolved_hypotheses_summary if h.hypothesis_id == H.H1)
    assert h.decisive_evidence_refs and h.status == S.ELIMINATED
    assert not hasattr(h, "relations") and not hasattr(h, "supporting_evidence_refs")


def test_confirmed_hypothesis_keeps_decisive_refs(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    graph = HypothesisEngine().evaluate(case, evidence)
    for h in graph.hypotheses:
        if h.status == S.CONFIRMED:
            capsule = next(c for c in snapshot.active_hypotheses if c.hypothesis_id == h.hypothesis_id)
            assert capsule.decisive_evidence_refs == h.decisive_evidence_refs
            assert set(capsule.decisive_evidence_refs) <= set(snapshot.selected_evidence_refs)


def test_context_evidence_refs_are_valid(harness):
    snapshot, _, evidence, _, _, _ = build(harness)
    assert set(snapshot.selected_evidence_refs) == set(referenced_ids(snapshot))
    assert set(snapshot.selected_evidence_refs) <= {e.evidence_id for e in evidence}
    summary = snapshot.omitted_evidence_summary
    assert summary.selected + sum(g.count for g in summary.omitted) == summary.total_evidence


def test_tool_catalog_respects_case_scope(harness):
    cases, repository, executor, _, _ = harness(tools={T.FUND})
    execute(executor, T.FUND)
    snapshot = ReasoningContextAssembler().build(cases.get("CASE-JD202609100001"), repository.list("CASE-JD202609100001"))
    assert {t.tool_name for t in snapshot.available_tools} == {T.FUND}
    assert any(g.gap_id.endswith(":PAYMENT_FINALITY") for g in snapshot.open_evidence_gaps)
    assert {entry.tool_name for entry in ToolCapabilityCatalog.entries} == set(T)


def test_gap_does_not_contain_next_tool(harness):
    snapshot, _, _, _, _, _ = build(harness)
    assert all("next_tool" not in g.model_dump_json() for g in snapshot.open_evidence_gaps)


def test_context_assembler_does_not_choose_next_action(harness):
    snapshot, _, _, _, _, _ = build(harness)
    for field in ("next_action", "next_tool", "selected_tool", "tool_ranking", "prompt"):
        assert field not in json.dumps(snapshot.model_json_schema())


def large_inputs(case, evidence):
    items = list(evidence)
    cb = next(e for e in evidence if e.claim_type == C.CALLBACK_GATEWAY_RECEIVED)
    for i in range(300):
        items.append(changed(cb, claim_type=C.SOURCE_LOOKUP_STATUS, value="NOT_FOUND", event_time=None,
                             source_as_of=None, freshness=Freshness.UNKNOWN, completeness=Completeness.UNKNOWN,
                             observed_at=cb.observed_at-timedelta(seconds=400-i)))
    trace = next(e for e in evidence if e.claim_type == C.REQUEST_SENT)
    for i in range(100):
        when = trace.observed_at-timedelta(seconds=200-i)
        items.append(changed(trace, claim_type=C.GUARANTEE_STATUS, tool=T.GUARANTEE, value="PROCESSING",
                             event_time=when, observed_at=when, source_as_of=when, freshness=Freshness.STALE))
    protocol = next(e for e in evidence if e.claim_type == C.PROTOCOL_FIELD_TYPE)
    for i in range(70):
        items.append(changed(protocol, subject=protocol.subject.model_copy(update={"field": f"unused_{i}"})))
    return tuple(items)


def test_large_evidence_set_is_bounded(harness):
    _, case, evidence, _, _, _ = build(harness)
    items = large_inputs(case, evidence)
    snapshot = ReasoningContextAssembler().build(case, items)
    assert len(items) >= 500
    assert len(snapshot.selected_evidence_refs) < len(items) // 4
    assert len(snapshot.current_facts) < len(items) // 4
    assert snapshot.financial_identity.result == M.MATCH
    assert {e.claim_type for e in evidence if e.claim_type in MandatoryContextFactPolicy.claims} <= {f.claim_type for f in snapshot.current_facts}
    assert max(g.references.count for g in snapshot.history_digest.repeated_lookup_groups) == 300
    assert max(g.references.count for g in snapshot.history_digest.state_transitions) == 100
    assert snapshot.context_budget_usage.serialized_chars <= snapshot.context_budget_usage.limits.max_serialized_chars
    assert snapshot == ReasoningContextAssembler().build(case, tuple(reversed(items)))


def test_s6_reasoning_context(harness):
    snapshot, _, _, _, _, _ = build(harness)
    states = {h.hypothesis_id: h.status for h in snapshot.active_hypotheses}
    assert states[H.H4] == states[H.H6] == states[H.H6_SCHEMA_MISMATCH] == S.CONFIRMED
    assert states[H.H6_STALE_CONSUMER_SCHEMA] == states[H.H8] == S.SUPPORTED
    assert snapshot.financial_subject.expected_principal_minor == 2000000


def test_s8_reasoning_context(harness, admin):
    cases, repository, executor, sid, _ = harness(ScenarioId.S8)
    for _ in range(3):
        execute(executor, T.FUND)
        execute(executor, T.PAYMENT)
        admin.advance(sid, 1)
    snapshot = ReasoningContextAssembler().build(cases.get("CASE-JD202609100001"), repository.list("CASE-JD202609100001"))
    assert snapshot.financial_identity.result == M.UNKNOWN and not snapshot.current_facts
    assert all(h.status == S.POSSIBLE for h in snapshot.active_hypotheses)
    assert {g.status for g in snapshot.history_digest.repeated_lookup_groups} == {"NOT_FOUND", "TIMEOUT"}
    assert {g.references.count for g in snapshot.history_digest.repeated_lookup_groups} == {3}


def test_context_static_boundaries():
    root = Path(inspect.getfile(ReasoningContextAssembler)).parent
    forbidden = ("identity.vault", "simulator", "repository", "persistence", "openai", "anthropic", "langgraph", "google.genai")
    for path in (*root.glob("*.py"), *root.parent.joinpath("hypotheses").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [node.module or ""] if isinstance(node, ast.ImportFrom) else [alias.name for alias in node.names]
                assert not any(word in module for word in forbidden for module in modules)
            if isinstance(node, ast.Name):
                assert node.id not in {"WorldState", "GroundTruth", "SimulatorAdmin", "SecretStr", "SyntheticIdentityRecord"}


@pytest.mark.parametrize("scenario", [ScenarioId.S6, ScenarioId.S8])
def test_reasoning_context_demo_uses_real_harness_routes(engine, scenario):
    from scripts.demo_reasoning_context import run_context_demo, preview
    snapshot, case, evidence = run_context_demo(engine, scenario)
    assert snapshot == ReasoningContextAssembler().build(case, evidence)
    assert snapshot.snapshot_id in preview(snapshot)
    assert snapshot.financial_identity.result == (M.MATCH if scenario == ScenarioId.S6 else M.UNKNOWN)


def test_invariant_rejects_removed_critical_fact_even_when_ref_survives(harness):
    from credit_harness.context.invariants import ReasoningContextInvariantError, ReasoningContextInvariantValidator
    from credit_harness.hypotheses.index import EvidenceIndex
    snapshot, case, evidence, _, _, _ = build(harness)
    corrupt = snapshot.model_copy(update={"current_facts": tuple(f for f in snapshot.current_facts if f.claim_type != C.PAYMENT_AMOUNT)})
    with pytest.raises(ReasoningContextInvariantError, match="critical fact"):
        ReasoningContextInvariantValidator().validate(corrupt, EvidenceIndex(case, evidence),
            HypothesisEngine().evaluate(case, evidence), set(), ContextEligibilityPolicy())


def test_char_budget_is_measured_on_complete_snapshot(harness):
    snapshot, case, evidence, _, _, _ = build(harness)
    # Enough for Tier 0, deliberately smaller than the full S6 context.
    limit = len(snapshot.model_dump_json()) - 1000
    budget = ContextBudget(max_serialized_chars=limit)
    bounded = ReasoningContextAssembler(budget=budget).build(case, evidence)
    assert bounded.context_budget_usage.serialized_chars == len(bounded.model_dump_json()) <= limit
    assert len(bounded.model_dump_json()) < len(snapshot.model_dump_json())
    assert bounded.financial_identity == snapshot.financial_identity
