import ast
import inspect
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from credit_harness.cases.models import CaseStatus
from credit_harness.domain.enums import Freshness, ScenarioId, ToolName as T
from credit_harness.evidence.models import ClaimType as C, Evidence
from credit_harness.hypotheses.catalog import CATALOG, HYPOTHESIS_RULESET_VERSION
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.hypotheses.models import GapStatus, HypothesisGraphView, HypothesisId as H, HypothesisStatus as S
from tests.test_case_evidence import harness, execute, FORBIDDEN_KEYS  # shared real HTTP fixtures


def investigate(harness, scenario=ScenarioId.S6, *, tools=None):
    cases, repository, executor, sid, _ = harness(scenario, budget=32)
    tools = tools or (T.TRACE, T.FUND, T.PAYMENT, T.CALLBACK, T.MESSAGES)
    for tool in tools:
        execute(executor, tool)
    if tools == (T.TRACE, T.FUND, T.PAYMENT, T.CALLBACK, T.MESSAGES):
        execute(executor, T.PROTOCOL, protocol_version="2.3")
        execute(executor, T.PROTOCOL, protocol_version="2.2")
    case = cases.get("CASE-JD202609100001")
    evidence = repository.list(case.case_id)
    graph = HypothesisEngine().evaluate(case, evidence)
    return case, evidence, graph, executor, repository, sid


def status(graph, h):
    return next(s.status for s in graph.hypotheses if s.hypothesis_id == h)


def gap(graph, name):
    return next(g for g in graph.gaps if g.gap_id.endswith(":" + name))


def changed(e, **updates):
    return Evidence.model_validate({**e.model_dump(), "evidence_id": "TEST-" + uuid4().hex, **updates})


def test_hypothesis_engine_uses_only_evidence(harness, monkeypatch):
    from credit_harness.simulator.service import ObservationService
    from credit_harness.evidence.repository import EvidenceRepository
    case, evidence, _, executor, _, _ = investigate(harness)
    observation = execute(executor, T.FUND).observation
    def forbidden(*args, **kwargs):
        raise AssertionError("engine crossed the Evidence boundary")
    monkeypatch.setattr(ObservationService, "observe", forbidden)
    monkeypatch.setattr(EvidenceRepository, "get_raw_observation", forbidden)
    assert HypothesisEngine().evaluate(case, evidence)
    with pytest.raises(TypeError):
        HypothesisEngine(observation)
    with pytest.raises(TypeError):
        HypothesisEngine().evaluate(case, [observation])
    with pytest.raises(TypeError):
        HypothesisEngine().evaluate(observation, evidence)
    with pytest.raises(TypeError):
        HypothesisEngine().evaluate(case, evidence, relations=[])
    assert tuple(inspect.signature(HypothesisEngine.evaluate).parameters) == ("self", "case", "evidence")
    root = Path(inspect.getfile(HypothesisEngine)).parent
    for file in root.glob("*.py"):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(part in (node.module or "") for part in ("simulator", "persistence", "tools", "repository"))
            if isinstance(node, ast.Name):
                assert node.id not in {"Observation", "WorldState", "GroundTruth", "SimulatorAdmin"}


def test_engine_rejects_oracle_types(harness, engine):
    from tests.support.inspector import ground_truth, world_state
    cases, _, _, sid, _ = harness()
    case = cases.get("CASE-JD202609100001")
    for forbidden in (world_state(engine, sid), ground_truth(engine, sid)):
        with pytest.raises(TypeError):
            HypothesisEngine(forbidden)
        with pytest.raises(TypeError):
            HypothesisEngine().evaluate(case, [forbidden])


def test_h1_request_sent_eliminates_request_not_sent(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.TRACE,))
    assert status(graph, H.H1) == S.ELIMINATED


def test_h1_complete_false_trace_confirms_but_stale_does_not(harness):
    case, evidence, graph, _, _, _ = investigate(harness, ScenarioId.S1, tools=(T.TRACE,))
    assert status(graph, H.H1) == S.CONFIRMED
    old = tuple(changed(e, freshness=Freshness.STALE) for e in evidence)
    assert status(HypothesisEngine().evaluate(case, old), H.H1) != S.CONFIRMED


def test_not_found_does_not_confirm_fund_not_accepted(harness):
    _, _, graph, _, _, _ = investigate(harness, ScenarioId.S2, tools=(T.FUND,))
    assert status(graph, H.H2) in (S.UNKNOWN, S.POSSIBLE)


def test_payment_settled_eliminates_no_disbursement_hypotheses(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    assert status(graph, H.H2) == status(graph, H.H3) == S.ELIMINATED


def test_h4_timeout_plus_settled_is_confirmed_without_claiming_packet_loss(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    assert status(graph, H.H4) == S.CONFIRMED
    payload = graph.model_dump_json().lower()
    assert "packet loss" not in payload and "response lost" not in payload
    assert "网络某层" not in payload


def test_callback_not_found_does_not_confirm_never_sent(harness):
    _, _, graph, _, _, _ = investigate(harness, ScenarioId.S5, tools=(T.CALLBACK, T.PAYMENT))
    assert status(graph, H.H5) == S.SUPPORTED
    assert gap(graph, "CALLBACK_GATEWAY_OBSERVATION").status == GapStatus.OPEN


def test_ten_callback_absences_never_become_confirmation(harness, admin):
    cases, repository, executor, sid, _ = harness(ScenarioId.S5)
    for _ in range(10):
        execute(executor, T.CALLBACK)
        admin.advance(sid, 1)
    case = cases.get("CASE-JD202609100001")
    evidence = repository.list(case.case_id)
    assert len(evidence) == 10
    assert status(HypothesisEngine().evaluate(case, evidence), H.H5) == S.SUPPORTED


def test_callback_received_eliminates_h5(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.CALLBACK,))
    assert status(graph, H.H5) == S.ELIMINATED


def test_h6_callback_received_and_consume_failed_confirmed(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.CALLBACK, T.MESSAGES))
    assert status(graph, H.H6) == S.CONFIRMED


def test_h6_schema_mismatch_confirmed(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.CALLBACK, T.MESSAGES))
    assert status(graph, H.H6_SCHEMA_MISMATCH) == S.CONFIRMED


def test_stale_consumer_schema_not_overconfirmed(harness):
    case, evidence, graph, _, _, _ = investigate(harness)
    assert status(graph, H.H6_STALE_CONSUMER_SCHEMA) == S.SUPPORTED
    subset = tuple(e for e in evidence if e.claim_type in (
        C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE, C.PROTOCOL_FIELD_TYPE,
    ))
    assert {e.value for e in subset} == {"integer", "string"}
    assert status(HypothesisEngine().evaluate(case, subset), H.H6_STALE_CONSUMER_SCHEMA) != S.CONFIRMED


def test_s6_has_open_deployed_schema_gap(harness):
    _, _, graph, _, _, _ = investigate(harness)
    assert gap(graph, "DEPLOYED_CONSUMER_SCHEMA_VERSION").status == GapStatus.OPEN
    assert gap(graph, "DEPLOYED_CONSUMER_SCHEMA_VERSION").required_claim_types == ("DEPLOYED_CONSUMER_SCHEMA_VERSION",)


def test_s8_repeated_unknown_remains_unknown(harness, admin):
    cases, repository, executor, sid, _ = harness(ScenarioId.S8)
    for _ in range(3):
        execute(executor, T.FUND)
        execute(executor, T.PAYMENT)
        admin.advance(sid, 10)
    case = cases.get("CASE-JD202609100001")
    graph = HypothesisEngine().evaluate(case, repository.list(case.case_id))
    for h in (H.H1, H.H2, H.H3, H.H4):
        assert status(graph, h) in (S.UNKNOWN, S.POSSIBLE)
    assert gap(graph, "PAYMENT_FINALITY").status == GapStatus.OPEN
    assert gap(graph, "PAYMENT_FINALITY").priority_class == "SAFETY_CRITICAL"
    assert not graph.confirmed and not graph.eliminated


def test_multiple_hypotheses_can_be_confirmed(harness):
    _, _, graph, _, _, _ = investigate(harness)
    assert {H.H4, H.H6, H.H6_SCHEMA_MISMATCH} <= set(graph.confirmed)


def test_old_stale_evidence_does_not_override_current_evidence(harness, admin):
    cases, repository, executor, sid, _ = harness(ScenarioId.S7)
    from credit_harness.simulator.faults import ObservationFault
    from credit_harness.domain.enums import FaultKind
    from credit_harness.simulator.scenarios import T0
    admin.add_fault(sid, ObservationFault(tool=T.GUARANTEE, kind=FaultKind.OLD_CACHE,
                                         starts_at=T0, ends_at=T0 + timedelta(seconds=11), cached_revision=0))
    execute(executor, T.GUARANTEE)
    admin.advance(sid, 1)
    for tool in (T.GUARANTEE, T.ASSET_DELIVERY, T.ASSET):
        execute(executor, tool)
    case = cases.get("CASE-JD202609100001")
    evidence = repository.list(case.case_id)
    old = next(e for e in evidence if e.claim_type == C.GUARANTEE_STATUS and e.value == "PROCESSING")
    graph = HypothesisEngine().evaluate(case, evidence)
    assert status(graph, H.H7) == S.CONFIRMED
    assert any(r.evidence_id == old.evidence_id and r.hypothesis_id == H.H7 and r.relation == "CONTEXT_ONLY"
               for r in graph.relations)


def test_hypothesis_recompute_is_deterministic(harness):
    case, evidence, graph, _, _, _ = investigate(harness)
    assert HypothesisEngine().evaluate(case, tuple(reversed(evidence))) == graph
    assert HypothesisEngine().evaluate(case, (*evidence, *evidence)) == graph
    engine = HypothesisEngine()
    partial = tuple(e for e in evidence if e.claim_type != C.PAYMENT_FINALITY)
    assert status(engine.evaluate(case, partial), H.H4) != S.CONFIRMED
    assert engine.evaluate(case, evidence).model_dump_json() == graph.model_dump_json()
    assert status(engine.evaluate(case, partial), H.H4) != S.CONFIRMED


def test_ruleset_version_is_present(harness):
    _, _, graph, _, _, _ = investigate(harness)
    assert graph.rule_version == HYPOTHESIS_RULESET_VERSION == "3"
    assert all(s.rule_version == "3" for s in graph.hypotheses)
    assert all(r.rule_id.endswith(".v3") for r in graph.relations)


def test_hypothesis_graph_has_no_ground_truth(harness):
    _, _, graph, _, _, _ = investigate(harness)
    for forbidden in (*FORBIDDEN_KEYS, "GroundTruth", "WorldState", "ScenarioId", "RootCause"):
        assert forbidden not in graph.model_dump_json()
        assert forbidden not in json.dumps(HypothesisGraphView.model_json_schema())


def test_no_root_cause_field(harness):
    case, evidence, _, _, repository, _ = investigate(harness)
    before = case.model_dump_json(), evidence
    HypothesisEngine().evaluate(case, evidence)
    assert repository.cases.get(case.case_id).model_dump_json() == before[0]
    assert repository.list(case.case_id) == before[1]
    assert "root_cause" not in type(case).model_fields


def test_callback_protocol_does_not_bind_fund_protocol(harness):
    _, _, graph, _, _, _ = investigate(harness)
    assert status(graph, H.H8) == S.SUPPORTED
    assert gap(graph, "FUND_PROTOCOL_APPLICABILITY").status == GapStatus.OPEN


def test_explicit_guarantee_protocol_evidence_can_confirm_semantic_guard(harness):
    case, _, _, executor, repository, _ = investigate(harness)
    execute(executor, T.GUARANTEE)
    graph = HypothesisEngine().evaluate(repository.cases.get(case.case_id), repository.list(case.case_id))
    assert status(graph, H.H8) == S.CONFIRMED
    assert status(graph, H.H4) == S.CONFIRMED
    assert gap(graph, "FUND_PROTOCOL_APPLICABILITY").status == GapStatus.SATISFIED


def test_gap_progression_is_fact_based(harness):
    cases, repository, executor, _, _ = harness()
    def graph():
        case = cases.get("CASE-JD202609100001")
        return HypothesisEngine().evaluate(case, repository.list(case.case_id))
    execute(executor, T.TRACE)
    assert gap(graph(), "PAYMENT_FINALITY").status == GapStatus.OPEN
    execute(executor, T.FUND)
    assert gap(graph(), "PAYMENT_FINALITY").status == GapStatus.OPEN
    execute(executor, T.PAYMENT)
    assert gap(graph(), "PAYMENT_FINALITY").status == GapStatus.SATISFIED
    execute(executor, T.CALLBACK)
    assert gap(graph(), "CALLBACK_CONSUMPTION").status == GapStatus.OPEN
    execute(executor, T.MESSAGES)
    assert gap(graph(), "CALLBACK_CONSUMPTION").status == GapStatus.SATISFIED
    assert gap(graph(), "CONSUMER_FAILURE_DETAILS").status == GapStatus.SATISFIED
    assert gap(graph(), "DEPLOYED_CONSUMER_SCHEMA_VERSION").status == GapStatus.OPEN


def test_no_cross_request_confirmation(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    modified = tuple(changed(e, metadata=e.metadata.model_copy(update={"fund_request_id": "OTHER"}))
                     if e.tool == T.TRACE else e for e in evidence)
    assert status(HypothesisEngine().evaluate(case, modified), H.H4) != S.CONFIRMED


def test_no_cross_callback_confirmation(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.CALLBACK, T.MESSAGES))
    modified = tuple(changed(e, metadata=e.metadata.model_copy(update={"callback_event_id": "OTHER"}))
                     if e.tool == T.MESSAGES else e for e in evidence)
    assert status(HypothesisEngine().evaluate(case, modified), H.H6) != S.CONFIRMED


def test_no_cross_message_schema_confirmation(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.MESSAGES,))
    modified = tuple(changed(e, observation_id=str(uuid4()), raw_ref="observation://different")
                     if e.claim_type == C.MESSAGE_ACTUAL_FIELD_TYPE else e for e in evidence)
    # Invalid provenance is refused at the input boundary.
    with pytest.raises(ValueError):
        HypothesisEngine().evaluate(case, modified)
    actual = next(e for e in evidence if e.claim_type == C.MESSAGE_ACTUAL_FIELD_TYPE)
    oid = str(uuid4())
    replacement = changed(actual, observation_id=oid, raw_ref=f"observation://{oid}")
    valid_but_unrelated = tuple(replacement if e is actual else e for e in evidence)
    assert status(HypothesisEngine().evaluate(case, valid_but_unrelated), H.H6_SCHEMA_MISMATCH) != S.CONFIRMED


def test_conflicting_current_trace_does_not_arbitrarily_win(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    http = next(e for e in evidence if e.claim_type == C.HTTP_RESPONSE_STATUS)
    conflicting = changed(http, value="OK")
    graph = HypothesisEngine().evaluate(case, (*evidence, conflicting))
    assert status(graph, H.H4) not in (S.CONFIRMED, S.ELIMINATED)


def test_legacy_missing_links_are_not_invented(harness):
    case, evidence, _, _, _, _ = investigate(harness)
    legacy = tuple(changed(e, metadata=e.metadata.model_copy(update={
        "fund_request_id": None, "callback_event_id": None, "extractor_version": "1",
    })) for e in evidence)
    graph = HypothesisEngine().evaluate(case, legacy)
    assert status(graph, H.H1) == S.ELIMINATED
    assert status(graph, H.H4) != S.CONFIRMED
    assert status(graph, H.H6) != S.CONFIRMED


def test_relations_reference_only_input_evidence(harness):
    _, evidence, graph, _, _, _ = investigate(harness)
    ids = {e.evidence_id for e in evidence}
    assert all(r.evidence_id in ids for r in graph.relations)
    assert all(set(s.decisive_evidence_refs) <= ids for s in graph.hypotheses)
    assert all(set(g.evidence_refs) <= ids for g in graph.gaps)
    assert all(s.decisive_evidence_refs for s in graph.hypotheses if s.status in (S.CONFIRMED, S.ELIMINATED))


def test_no_confidence_or_tool_selection_in_graph(harness):
    _, _, graph, _, _, _ = investigate(harness)
    schema = json.dumps(graph.model_json_schema()).lower()
    for name in ("confidence", "probability", "next_tool", "next_action", "prompt"):
        assert name not in schema
    assert all("get_" not in g.model_dump_json() for g in graph.gaps)


def test_index_preserves_history_and_is_read_only(harness):
    case, evidence, _, _, _, _ = investigate(harness)
    index = EvidenceIndex(case, evidence)
    assert index.all(C.PAYMENT_FINALITY)[0] in evidence
    assert index.current(C.PAYMENT_FINALITY)[0].subject.identifier
    with pytest.raises((AttributeError, TypeError)):
        index.evidence = ()
    with pytest.raises(TypeError):
        index._by_claim[C.PAYMENT_FINALITY] = ()


def test_cross_case_evidence_rejected(harness):
    case, evidence, _, _, _, _ = investigate(harness)
    foreign = changed(evidence[0], case_id="CASE-OTHER")
    with pytest.raises(ValueError, match="scope"):
        HypothesisEngine().evaluate(case, (*evidence, foreign))


def test_new_case_with_no_evidence_is_unknown(harness):
    cases, _, _, _, _ = harness()
    graph = HypothesisEngine().evaluate(cases.get("CASE-JD202609100001"), ())
    assert {s.status for s in graph.hypotheses} == {S.UNKNOWN}
    assert not graph.relations


def test_business_failure_is_support_not_http_failure_confirmation(harness):
    _, _, graph, _, _, _ = investigate(harness, ScenarioId.S3, tools=(T.TRACE, T.FUND, T.PAYMENT))
    assert status(graph, H.H3) == S.SUPPORTED


def test_s6_h7_not_confirmed_without_asset_investigation(harness):
    _, _, graph, _, _, _ = investigate(harness)
    assert status(graph, H.H7) != S.CONFIRMED


def test_ambiguous_partner_cannot_confirm_protocol_guard(harness):
    case, _, _, executor, repository, _ = investigate(harness)
    execute(executor, T.GUARANTEE)
    evidence = repository.list(case.case_id)
    original = next(e for e in evidence if e.claim_type == C.PROTOCOL_BUSINESS_SEMANTICS
                    and e.subject.field == "SUCCESS" and e.protocol_version == "2.3")
    another_partner = changed(original, value="REQUEST_PROCESSING", subject=original.subject.model_copy(
        update={"identifier": "OTHER-FUND@2.3"},
    ))
    graph = HypothesisEngine().evaluate(case, (*evidence, another_partner))
    assert status(graph, H.H8) == S.SUPPORTED
    assert gap(graph, "FUND_PROTOCOL_APPLICABILITY").status == GapStatus.OPEN


def test_later_payment_timeout_blocks_old_current_payment(harness, admin):
    from credit_harness.simulator.faults import ObservationFault
    from credit_harness.domain.enums import FaultKind
    from credit_harness.simulator.scenarios import T0

    case, _, graph, executor, repository, sid = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    assert status(graph, H.H4) == S.CONFIRMED
    admin.advance(sid, 1)
    admin.add_fault(sid, ObservationFault(tool=T.PAYMENT, kind=FaultKind.TIMEOUT, starts_at=T0))
    execute(executor, T.PAYMENT)
    evidence = repository.list(case.case_id)
    graph = HypothesisEngine().evaluate(case, evidence)
    assert status(graph, H.H4) != S.CONFIRMED
    assert gap(graph, "PAYMENT_FINALITY").status == GapStatus.OPEN
    assert any(e.claim_type == C.PAYMENT_FINALITY and e.value == "SETTLED" for e in evidence)


def test_partial_authoritative_evidence_is_not_decisive(harness):
    from credit_harness.domain.enums import Completeness
    from credit_harness.evidence.models import EvidenceStrength

    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE,))
    partial = tuple(changed(e, completeness=Completeness.PARTIAL, strength=EvidenceStrength.AUTHORITATIVE)
                    for e in evidence)
    graph = HypothesisEngine().evaluate(case, partial)
    assert status(graph, H.H1) not in (S.CONFIRMED, S.ELIMINATED)


@pytest.mark.parametrize("scenario", [ScenarioId.S6, ScenarioId.S8])
def test_graph_demo_runs_real_investigation(engine, scenario):
    from scripts.demo_hypothesis_graph import run_graph_demo

    graph, diagnostic = run_graph_demo(engine, scenario)
    assert graph.case_id == diagnostic.case.case_id
    input_ids = {e.evidence_id for items in diagnostic.evidence_by_claim_type.values() for e in items}
    assert all(r.evidence_id in input_ids for r in graph.relations)
    if scenario == ScenarioId.S6:
        assert {H.H4, H.H6, H.H6_SCHEMA_MISMATCH} <= set(graph.confirmed)
        assert status(graph, H.H6_STALE_CONSUMER_SCHEMA) == S.SUPPORTED
    else:
        assert not graph.confirmed and not graph.eliminated
