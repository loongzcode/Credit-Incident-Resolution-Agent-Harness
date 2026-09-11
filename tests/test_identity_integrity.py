import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from credit_harness.api.harness import HarnessBinding, create_harness_app
from credit_harness.cases.evidence_view import CaseEvidenceService
from credit_harness.domain.enums import ScenarioId, ToolName as T
from credit_harness.evidence.models import ClaimType as C, Evidence, SubjectKind
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.invariants import HypothesisGraphInvariantValidator, HypothesisInvariantError
from credit_harness.hypotheses.models import HypothesisId as H, HypothesisStatus as S, GapStatus
from credit_harness.identity.models import IdentityMatch as M, IdentityDimension as D
from credit_harness.identity.service import ReferenceIdentityVerificationService
from credit_harness.identity.vault import SyntheticIdentityVault, synthetic_identity_fixture
from credit_harness.persistence.store import token_hash
from credit_harness.simulator.scenarios import build_scenario
from tests.test_case_evidence import harness, execute
from tests.test_hypotheses import investigate, status, gap, changed


def test_child_cannot_be_confirmed_when_parent_not_confirmed(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.MESSAGES,))
    assert status(graph, H.H6) != S.CONFIRMED
    assert status(graph, H.H6_SCHEMA_MISMATCH) == S.SUPPORTED


def test_h6_schema_confirmation_contains_parent_witness(harness):
    _, evidence, graph, _, _, _ = investigate(harness, tools=(T.CALLBACK, T.MESSAGES))
    parent, child = (next(s for s in graph.hypotheses if s.hypothesis_id == h)
                     for h in (H.H6, H.H6_SCHEMA_MISMATCH))
    assert parent.status == child.status == S.CONFIRMED
    assert set(parent.decisive_evidence_refs) <= set(child.decisive_evidence_refs)
    assert {e.claim_type for e in evidence if e.evidence_id in child.decisive_evidence_refs} == {
        C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS, C.MESSAGE_ERROR_CODE,
        C.MESSAGE_ERROR_FIELD, C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE,
    }


def test_graph_parent_child_invariant(harness):
    _, _, graph, _, _, _ = investigate(harness)
    validator = HypothesisGraphInvariantValidator()
    validator.validate(graph)
    invalid = graph.model_copy(update={"hypotheses": tuple(
        s.model_copy(update={"status": S.POSSIBLE}) if s.hypothesis_id == H.H6 else s
        for s in graph.hypotheses)})
    before = invalid.model_dump_json()
    with pytest.raises(HypothesisInvariantError):
        validator.validate(invalid)
    assert invalid.model_dump_json() == before
    # Eliminated specialization says nothing about its parent.
    valid = graph.model_copy(update={"hypotheses": tuple(
        s.model_copy(update={"status": S.ELIMINATED}) if s.hypothesis_id == H.H6_SCHEMA_MISMATCH else s
        for s in graph.hypotheses)})
    validator.validate(valid)
    assert status(valid, H.H6) == S.CONFIRMED


@pytest.mark.parametrize("broken", ["event", "observation", "message"])
def test_schema_error_cannot_join_other_event(harness, broken):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.CALLBACK, T.MESSAGES))
    target = next(e for e in evidence if e.claim_type == C.MESSAGE_ACTUAL_FIELD_TYPE)
    oid = str(uuid4())
    updates = {"event": {"metadata": target.metadata.model_copy(update={"callback_event_id": "OTHER"})},
               "observation": {"observation_id": oid, "raw_ref": f"observation://{oid}"},
               "message": {"subject": target.subject.model_copy(update={"identifier": "OTHER"})}}[broken]
    graph = HypothesisEngine().evaluate(case, tuple(changed(e, **updates) if e is target else e for e in evidence))
    assert status(graph, H.H6) == S.CONFIRMED
    assert status(graph, H.H6_SCHEMA_MISMATCH) != S.CONFIRMED


def test_payment_identity_match(harness):
    case, evidence, graph, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    result = graph.payment_identity
    assert result.result == M.MATCH and not result.mismatch_dimensions and not result.unknown_dimensions
    witness, = result.witnesses
    payment = [e for e in evidence if e.tool == T.PAYMENT]
    assert len(payment) == 8
    assert {e.subject for e in payment} == {payment[0].subject}
    assert payment[0].subject.kind == SubjectKind.TRANSACTION
    assert set(witness.evidence_refs) == {e.evidence_id for e in evidence}
    assert status(graph, H.H4) == S.CONFIRMED
    assert status(graph, H.H2) == status(graph, H.H3) == S.ELIMINATED
    assert gap(graph, "PAYMENT_IDENTITY").status == GapStatus.SATISFIED
    assert case.financial_subject.expected_principal_minor == 2_000_000


def negative_payment(harness, admin, field, value):
    # Real synthetic World -> persisted Tool Observation -> persisted Evidence -> Engine.
    scenario = build_scenario(ScenarioId.S6)
    frames = []
    for frame in scenario.frames:
        t = frame.fund.disbursement_transaction
        if t:
            t = type(t).model_validate({**t.model_dump(), field: value})
            fund_updates = {"disbursement_transaction": t}
            if field in ("amount", "currency", "fund_request_id"):
                fund_updates[field] = value
            fund = type(frame.fund).model_validate({**frame.fund.model_dump(), **fund_updates})
            frame = type(frame).model_validate({**frame.model_dump(), "fund": fund})
        frames.append(frame)
    sid = admin.seed(scenario.model_copy(update={"frames": tuple(frames)}))
    token = admin.grant(sid, set(T))
    cases, repository, executor, _, _ = harness(existing=(sid, token))
    for tool in (T.TRACE, T.PAYMENT):
        execute(executor, tool)
    return HypothesisEngine().evaluate(cases.get("CASE-JD202609100001"), repository.list("CASE-JD202609100001"))


def assert_mismatch(graph, dimension):
    assert graph.payment_identity.result == M.MISMATCH
    assert graph.payment_identity.mismatch_dimensions == (dimension,)
    assert status(graph, H.H4) != S.CONFIRMED
    assert status(graph, H.H2) != S.ELIMINATED and status(graph, H.H3) != S.ELIMINATED
    assert gap(graph, "PAYMENT_FINALITY").status == GapStatus.SATISFIED
    assert gap(graph, "PAYMENT_IDENTITY").status == GapStatus.OPEN


def test_wrong_amount_blocks_h4_confirmation(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "amount", 1_000_000), D.AMOUNT)


def test_wrong_currency_blocks_h4_confirmation(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "currency", "USD"), D.CURRENCY)


def test_wrong_beneficiary_blocks_h4_confirmation(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "beneficiary_ref", "BEN-OTHER"), D.BENEFICIARY)


def test_wrong_account_blocks_h4_confirmation(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "account_ref", "ACC-OTHER"), D.ACCOUNT)


def test_wrong_customer_blocks_h4_confirmation(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "customer_ref", "CUS-OTHER"), D.CUSTOMER)


def test_wrong_request_blocks_h4_confirmation(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "fund_request_id", "FREQ-OTHER"), D.REQUEST)


def test_cross_transaction_identity_cannot_be_joined(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    parts = tuple(changed(e, subject=e.subject.model_copy(update={"identifier": "PAY-OTHER"}))
                  if e.claim_type in (C.PAYMENT_BENEFICIARY_REF, C.PAYMENT_ACCOUNT_REF) else e for e in evidence)
    graph = HypothesisEngine().evaluate(case, parts)
    assert graph.payment_identity.result == M.UNKNOWN
    assert len(graph.payment_identity.witnesses) == 2
    assert status(graph, H.H4) != S.CONFIRMED


def test_cross_observation_identity_cannot_be_joined(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    oid = str(uuid4())
    parts = tuple(changed(e, observation_id=oid, raw_ref=f"observation://{oid}")
                  if e.claim_type == C.PAYMENT_ACCOUNT_REF else e for e in evidence)
    graph = HypothesisEngine().evaluate(case, parts)
    assert graph.payment_identity.result == M.UNKNOWN


def test_missing_identity_field_returns_unknown(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    graph = HypothesisEngine().evaluate(case, tuple(e for e in evidence if e.claim_type != C.PAYMENT_ACCOUNT_REF))
    assert graph.payment_identity.result == M.UNKNOWN
    assert graph.payment_identity.unknown_dimensions == (D.ACCOUNT,)
    assert status(graph, H.H4) != S.CONFIRMED


def test_payment_finality_can_be_known_while_identity_unknown(harness):
    _, _, graph, _, _, _ = investigate(harness, tools=(T.PAYMENT,))
    assert gap(graph, "PAYMENT_FINALITY").status == GapStatus.SATISFIED
    assert graph.payment_identity.result == M.UNKNOWN
    assert D.REQUEST in graph.payment_identity.unknown_dimensions
    assert gap(graph, "PAYMENT_IDENTITY").status == GapStatus.OPEN
    assert status(graph, H.H2) != S.ELIMINATED and status(graph, H.H3) != S.ELIMINATED


def test_payment_finality_can_be_settled_while_identity_mismatch(harness, admin):
    assert_mismatch(negative_payment(harness, admin, "beneficiary_ref", "BEN-OTHER"), D.BENEFICIARY)


def test_identity_mismatch_does_not_become_probability(harness, admin):
    graph = negative_payment(harness, admin, "account_ref", "ACC-OTHER")
    assert graph.payment_identity.result == M.MISMATCH
    for forbidden in ("probability", "confidence", "identity_score"):
        assert forbidden not in graph.model_dump_json().lower()


def test_legacy_case_missing_financial_subject_returns_unknown(harness):
    case, evidence, _, _, _, _ = investigate(harness)
    graph = HypothesisEngine().evaluate(case.model_copy(update={"financial_subject": None}), evidence)
    assert graph.payment_identity.result == M.UNKNOWN
    assert status(graph, H.H4) != S.CONFIRMED


def test_identity_mismatch_does_not_hide_independent_fund_evidence(harness):
    case, evidence, _, _, _, _ = investigate(harness)
    altered = tuple(changed(e, value="ACC-OTHER") if e.claim_type == C.PAYMENT_ACCOUNT_REF else e for e in evidence)
    graph = HypothesisEngine().evaluate(case, altered)
    assert graph.payment_identity.result == M.MISMATCH
    assert status(graph, H.H2) == S.ELIMINATED  # independent Fund SUCCESS proves acceptance
    assert all(r.evidence_id in {e.evidence_id for e in evidence if e.tool == T.FUND}
               for r in graph.relations if r.hypothesis_id == H.H2 and r.relation == "DECISIVE_CONTRADICTION")
    assert status(graph, H.H3) != S.ELIMINATED


RAW_VALUES = ("TEST-PERSON-0001", "TEST-ID-0001", "TEST-CARD-0001", "TEST-MOBILE-0001",
              "310101199001011234", "6222021234567890123", "13800138000")
RAW_FIELDS = ("full_name", "id_card", "bank_card", "mobile", "SyntheticIdentityRecord")


def assert_no_raw(text):
    for forbidden in (*RAW_VALUES, *RAW_FIELDS):
        assert forbidden not in text


def test_raw_pii_never_enters_evidence(harness):
    record = synthetic_identity_fixture()
    vault = SyntheticIdentityVault((record,))
    assert vault.contains("CUS-JD-001")
    assert not any(value in repr(record) + record.model_dump_json() for value in RAW_VALUES)
    _, evidence, _, _, _, _ = investigate(harness)
    assert_no_raw(json.dumps([e.model_dump(mode="json") for e in evidence]))
    for claim, raw in ((C.PAYMENT_CUSTOMER_REF, RAW_VALUES[4]), (C.PAYMENT_ACCOUNT_REF, RAW_VALUES[5]),
                       (C.PAYMENT_BENEFICIARY_REF, RAW_VALUES[6])):
        e = next(e for e in evidence if e.claim_type == claim)
        with pytest.raises(ValidationError):
            changed(e, value=raw)


def test_raw_pii_never_enters_hypothesis_graph(harness):
    case, evidence, graph, _, _, _ = investigate(harness)
    assert_no_raw(graph.model_dump_json())
    assert_no_raw(json.dumps(graph.model_json_schema()))
    e = next(e for e in evidence if e.claim_type == C.PAYMENT_CUSTOMER_REF)
    bypassed = e.model_copy(update={"value": RAW_VALUES[4]})
    with pytest.raises(ValidationError):
        HypothesisEngine().evaluate(case, tuple(bypassed if item is e else item for item in evidence))


def test_raw_pii_not_exposed_by_harness_api(harness):
    SyntheticIdentityVault((synthetic_identity_fixture(),))
    cases, repository, executor, _, _ = harness()
    app = create_harness_app({token_hash("test-harness-only"): HarnessBinding(executor, CaseEvidenceService(repository))})
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-harness-only"}
        prefix = "/cases/CASE-JD202609100001"
        reply = client.post(prefix + "/tools/" + T.PAYMENT.value, headers=headers,
                            json={"internal_order_id": "JD202609100001"})
        assert reply.status_code == 200
        assert_no_raw(reply.text)
        for url in (prefix, prefix + "/evidence", prefix + "/evidence/" + reply.json()["evidence_refs"][0] + "/raw"):
            result = client.get(url, headers=headers)
            assert result.status_code == 200
            assert_no_raw(result.text)
        assert_no_raw(json.dumps(app.openapi()))
        assert not any("identity" in route.path or "vault" in route.path for route in app.routes)


def test_synthetic_vault_rejects_non_test_values():
    record = synthetic_identity_fixture()
    with pytest.raises(ValidationError):
        type(record).model_validate({**record.model_dump(), "id_card": RAW_VALUES[4]})


def test_reference_service_is_exact_and_unknown_is_not_match():
    service = ReferenceIdentityVerificationService()
    assert service.verify_account("ACC-A", "ACC-A") == M.MATCH
    assert service.verify_account("ACC-A", "acc-a") == M.MISMATCH
    assert service.verify_customer("CUS-A", None) == M.UNKNOWN
    assert service.verify_beneficiary(None, "BEN-A") == M.UNKNOWN


def test_current_conflicting_identity_never_selects_matching_value(harness):
    case, evidence, _, _, _, _ = investigate(harness, tools=(T.TRACE, T.PAYMENT))
    account = next(e for e in evidence if e.claim_type == C.PAYMENT_ACCOUNT_REF)
    graph = HypothesisEngine().evaluate(case, (*evidence, changed(account, value="ACC-OTHER")))
    assert graph.payment_identity.result == M.UNKNOWN
    assert D.ACCOUNT in graph.payment_identity.unknown_dimensions
    assert status(graph, H.H4) != S.CONFIRMED


def test_engine_invokes_graph_invariant_validator(harness, monkeypatch):
    import credit_harness.hypotheses.engine as module
    from dataclasses import replace
    case, evidence, _, _, _, _ = investigate(harness)
    original = module.evaluate_rules
    def broken_rules(index):
        return tuple(replace(r, status=S.POSSIBLE) if r.hypothesis_id == H.H6 else r
                     for r in original(index))
    monkeypatch.setattr(module, "evaluate_rules", broken_rules)
    with pytest.raises(HypothesisInvariantError):
        HypothesisEngine().evaluate(case, evidence)


def test_pii_access_intent_is_design_only_and_frozen():
    from credit_harness.identity.models import PIIAccessIntent
    from credit_harness.simulator.scenarios import T0
    intent = PIIAccessIntent(case_id="CASE-TEST", purpose="MANUAL_INCIDENT_INVESTIGATION",
                             subject_ref="CUS-JD-001", allowed_fields={"id_card"}, expires_at=T0,
                             actor="HUMAN-TEST", audit_id="AUDIT-TEST")
    with pytest.raises(ValidationError):
        intent.actor = "OTHER"
    with pytest.raises(ValidationError):
        PIIAccessIntent.model_validate({**intent.model_dump(), "authorized": True})
    assert not hasattr(intent, "authorize") and not hasattr(intent, "reveal")


def test_payment_projection_does_not_serialize_future_private_fields():
    from credit_harness.domain.models import DisbursementTransaction
    from credit_harness.simulator.projections import project
    from credit_harness.tools.contracts import ToolQuery
    class PrivateTransaction(DisbursementTransaction):
        id_card: str
        bank_card: str
        mobile: str
    scenario = build_scenario(ScenarioId.S6)
    world = scenario.frames[-1]
    private = PrivateTransaction(**world.fund.disbursement_transaction.model_dump(),
                                 id_card=RAW_VALUES[1], bank_card=RAW_VALUES[2], mobile=RAW_VALUES[3])
    world = world.model_copy(update={"fund": world.fund.model_copy(update={"disbursement_transaction": private})})
    dto, _ = project(world, T.PAYMENT, ToolQuery(internal_order_id="JD202609100001"), scenario.initial_time)
    assert_no_raw(dto.model_dump_json())
    with pytest.raises(ValidationError):
        type(dto.record.transaction).model_validate(private.model_dump())
