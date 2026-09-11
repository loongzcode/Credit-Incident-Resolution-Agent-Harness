import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from credit_harness.api.harness import HarnessBinding, create_harness_app
from credit_harness.cases.evidence_view import CaseEvidenceService, CaseEvidenceView
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.cases.fixtures import investigation_case
from credit_harness.cases.models import Case, CaseAccessError, CasePolicyError, CaseStatus, CaseToolResult
from credit_harness.cases.repository import CaseRepository, CallState
from credit_harness.cases.schema import create_harness_schema
from credit_harness.cases.service import CaseService
from credit_harness.cases.tables import CaseCallRow
from credit_harness.domain.enums import (
    Completeness, FaultKind, Freshness, KnowledgeStatus, ScenarioId, SourceKind, ToolName,
)
from credit_harness.evidence.extractor import EvidenceExtractor
from credit_harness.evidence.models import ClaimType as C, Evidence, RawObservation, json_hash
from credit_harness.evidence.repository import EvidenceRepository, ProvenanceError
from credit_harness.evidence.services import EvidenceConflictDetector, EvidenceFreshnessService
from credit_harness.persistence.store import ObservationRow, token_hash
from credit_harness.simulator.faults import ObservationFault
from credit_harness.simulator.scenarios import ORDER_ID, T0
from credit_harness.tools.contracts import DISPATCH_CORRELATION_HEADER, Observation, ToolQuery

Q = ToolQuery(internal_order_id=ORDER_ID)
FORBIDDEN_KEYS = (
    "scenario_id", "root_cause", "ground_truth", "fund_accepted",
    "funds_must_remain_unknown", "expected_entry", "faults", "cached_revision",
    "disbursement_intent_count",
)


class HTTPTestToolClient:
    """Uses the real Observation HTTP route, never simulator admin methods."""
    def __init__(self, client, token):
        self.client, self.token = client, token

    def observe(self, tool, query, *, dispatch_correlation_id):
        response = self.client.post(f"/tools/{tool.value}",
                                    headers={"Authorization": f"Bearer {self.token}",
                                             DISPATCH_CORRELATION_HEADER: dispatch_correlation_id},
                                    json=query.model_dump(mode="json"))
        response.raise_for_status()
        return Observation.model_validate(response.json())


@pytest.fixture
def harness(engine, seeded, client):
    create_harness_schema(engine)
    def make(scenario=ScenarioId.S6, *, case_id="CASE-JD202609100001", budget=20,
             tools=None, tenant="demo", existing=None):
        sid, token = existing or seeded(scenario)
        cases = CaseRepository(engine, tenant)
        case = investigation_case(sid, tenant_id=tenant, case_id=case_id,
                                  max_tool_calls=budget, allowed_tools=tools)
        CaseService(cases).create(case, tool_credential=token)
        evidence = EvidenceRepository(cases)
        tool_client = HTTPTestToolClient(client, token)
        executor = CaseToolExecutor(cases, evidence, lambda _: tool_client)
        return cases, evidence, executor, sid, token
    return make


def execute(executor, tool, **kwargs):
    return executor.execute("CASE-JD202609100001", tool, ToolQuery(internal_order_id=ORDER_ID, **kwargs))


def values(repository, claim, case_id="CASE-JD202609100001"):
    return [e.value for e in repository.list(case_id) if e.claim_type == claim]


def test_create_case_for_simulation(harness):
    cases, evidence, executor, sid, token = harness()
    case = cases.get("CASE-JD202609100001")
    assert case.simulation_id == sid and case.case_id != sid
    assert case.status == CaseStatus.NEW and case.budget.used_tool_calls == 0
    assert case.task_contract.goal == case.goal
    second, other, second_executor, _, _ = harness(case_id="CASE-002", budget=3, existing=(sid, token))
    execute(executor, ToolName.TRACE)
    assert second.get("CASE-002").budget.used_tool_calls == 0
    assert other.list("CASE-002") == ()
    assert len(evidence.list(case.case_id)) == 2


def test_case_tool_scope_enforced(harness):
    cases, evidence, executor, _, _ = harness(tools={ToolName.FUND})
    with pytest.raises(CasePolicyError):
        execute(executor, ToolName.PAYMENT)
    with pytest.raises(CasePolicyError):
        executor.execute("CASE-JD202609100001", ToolName.FUND, ToolQuery(internal_order_id="OTHER"))
    assert cases.get("CASE-JD202609100001").budget.used_tool_calls == 0
    assert not evidence.list("CASE-JD202609100001")


def test_case_cannot_expand_upstream_grant(harness, seeded):
    sid, token = seeded(tools={ToolName.FUND})
    with pytest.raises(CaseAccessError):
        harness(existing=(sid, token), tools={ToolName.PAYMENT})


def test_case_budget_enforced(harness):
    cases, _, executor, _, _ = harness(budget=1)
    execute(executor, ToolName.FUND)
    with pytest.raises(CasePolicyError):
        execute(executor, ToolName.PAYMENT)
    assert cases.get("CASE-JD202609100001").budget.used_tool_calls == 1


def test_concurrent_budget_is_atomic(harness):
    cases, _, executor, _, _ = harness(budget=1)
    def run(_):
        try:
            execute(executor, ToolName.FUND)
            return True
        except CasePolicyError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(run, range(4))) == 1
    assert cases.get("CASE-JD202609100001").budget.used_tool_calls == 1


@pytest.mark.parametrize("status", [CaseStatus.WAITING, CaseStatus.ESCALATED])
def test_paused_case_cannot_dispatch_or_close(harness, status):
    cases, _, executor, _, _ = harness()
    cases.pause("CASE-JD202609100001", status)
    with pytest.raises(CasePolicyError):
        execute(executor, ToolName.FUND)
    with pytest.raises(CasePolicyError):
        cases.pause("CASE-JD202609100001", CaseStatus.CLOSED)


def test_observation_is_persisted_before_evidence(harness, engine):
    cases, repository, executor, _, _ = harness()
    class CheckingExtractor(EvidenceExtractor):
        def extract(self, case, observation, **kwargs):
            # Separate connection can already see committed simulator observation.
            with Session(engine) as session:
                assert session.get(ObservationRow, observation.observation_id) is not None
            return super().extract(case, observation, **kwargs)
    repository.extractor = CheckingExtractor()
    result = execute(executor, ToolName.FUND)
    _, call_id = cases.reserve_call(result.case_id, ToolName.FUND, Q)
    fake = result.observation.model_copy(update={"observation_id": str(uuid4())})
    with pytest.raises(ProvenanceError, match="persisted"):
        repository.record_call(result.case_id, call_id, fake)


def test_evidence_has_observation_provenance(harness):
    _, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.PAYMENT)
    for e in repository.list(result.case_id):
        raw = repository.get_raw_observation(e.evidence_id, case_id=result.case_id)
        assert raw.observation == result.observation
        assert raw.request == Q and raw.tool == ToolName.PAYMENT
        assert e.raw_ref == f"observation://{raw.observation_id}"
        assert e.content_hash == raw.content_hash == json_hash(raw.observation.model_dump(mode="json"))
        assert e.observed_at == raw.observation.observed_at
        assert e.source_as_of == raw.observation.source_as_of


def test_fund_success_does_not_generate_payment_settled_evidence(harness):
    _, repository, executor, _, _ = harness()
    execute(executor, ToolName.FUND)
    assert values(repository, C.FUND_BUSINESS_STATUS) == ["SUCCESS"]
    assert values(repository, C.PAYMENT_FINALITY) == []
    assert values(repository, C.PAYMENT_AMOUNT) == []


def test_payment_timeout_does_not_generate_payment_failed_evidence(harness):
    cases, repository, executor, _, _ = harness(ScenarioId.S8)
    execute(executor, ToolName.PAYMENT)
    assert values(repository, C.SOURCE_LOOKUP_STATUS) == ["TIMEOUT"]
    assert values(repository, C.PAYMENT_FINALITY) == []
    assert cases.get("CASE-JD202609100001").budget.used_tool_calls == 1


def test_callback_not_found_does_not_mean_never_sent(harness):
    _, repository, executor, _, _ = harness(ScenarioId.S5)
    execute(executor, ToolName.CALLBACK)
    assert values(repository, C.SOURCE_LOOKUP_STATUS) == ["NOT_FOUND"]
    assert values(repository, C.CALLBACK_GATEWAY_RECEIVED) == []
    lookup = repository.list("CASE-JD202609100001")[0]
    assert lookup.metadata.scope == Q and lookup.event_time is None


def test_s6_evidence_chain(harness):
    _, repository, executor, _, _ = harness()
    for tool in (ToolName.TRACE, ToolName.FUND, ToolName.PAYMENT, ToolName.CALLBACK, ToolName.MESSAGES):
        execute(executor, tool)
    execute(executor, ToolName.PROTOCOL, protocol_version="2.3")
    execute(executor, ToolName.PROTOCOL, protocol_version="2.2")
    view = CaseEvidenceService(repository).get_case_evidence("CASE-JD202609100001")
    expected = {
        C.REQUEST_SENT: True, C.HTTP_RESPONSE_STATUS: "TIMEOUT", C.FUND_BUSINESS_STATUS: "SUCCESS",
        C.PAYMENT_FINALITY: "SETTLED", C.PAYMENT_AMOUNT: 2_000_000, C.PAYMENT_CURRENCY: "CNY",
        C.CALLBACK_GATEWAY_RECEIVED: True, C.CALLBACK_SIGNATURE_VERIFIED: True,
        C.MESSAGE_CONSUME_STATUS: "FAILED", C.MESSAGE_DLQ: "loan.callback.dlq",
        C.MESSAGE_ERROR_CODE: "CALLBACK_SCHEMA_MISMATCH", C.MESSAGE_ERROR_FIELD: "loanNo",
        C.MESSAGE_EXPECTED_FIELD_TYPE: "integer", C.MESSAGE_ACTUAL_FIELD_TYPE: "string",
    }
    for claim, value in expected.items():
        assert [e.value for e in view.evidence_by_claim_type[claim]] == [value]
    assert {(e.protocol_version, e.value) for e in view.evidence_by_claim_type[C.PROTOCOL_FIELD_TYPE]} == {
        ("2.3", "string"), ("2.2", "integer"),
    }
    forbidden = {"ROOT_CAUSE", "RETRY_LOAN", "PAYMENT_FAILED", "CALLBACK_NEVER_SENT", "GROUND_TRUTH"}
    assert not forbidden.intersection(view.evidence_by_claim_type)
    assert view.evidence_count == 30
    assert view.case.budget.used_tool_calls == 7
    assert view.case.status == CaseStatus.INVESTIGATING
    assert len(view.latest_observations) == 7 and not view.conflicts
    assert view.payment_finality_evidence.knowledge == KnowledgeStatus.OBSERVED
    for items in view.evidence_by_claim_type.values():
        for e in items:
            assert repository.get_raw_observation(e.evidence_id, case_id=view.case.case_id)


def test_s8_payment_remains_unknown(harness, admin):
    _, repository, executor, sid, _ = harness(ScenarioId.S8)
    for _ in range(3):
        execute(executor, ToolName.FUND)
        execute(executor, ToolName.PAYMENT)
        admin.advance(sid, 15)
    view = CaseEvidenceService(repository).get_case_evidence("CASE-JD202609100001")
    assert view.payment_finality_evidence.knowledge == KnowledgeStatus.UNKNOWN
    assert view.payment_finality_evidence.evidence_refs == ()
    assert C.PAYMENT_FINALITY not in view.evidence_by_claim_type
    assert len(view.unknown_lookups) == 6
    assert {e.value for e in view.unknown_lookups} == {"NOT_FOUND", "TIMEOUT"}
    assert not any(e.value in ("NOT_EXECUTED", "FAILED", "SETTLED") for e in view.unknown_lookups)
    assert not view.conflicts


def test_protocol_version_evidence_is_separate(harness):
    _, repository, executor, _, _ = harness()
    for version in ("2.3", "2.2"):
        execute(executor, ToolName.PROTOCOL, protocol_version=version)
    evidence = [e for e in repository.list("CASE-JD202609100001") if e.claim_type == C.PROTOCOL_FIELD_TYPE]
    assert len(evidence) == 2 and evidence[0].subject != evidence[1].subject
    assert {e.metadata.scope.protocol_version for e in evidence} == {"2.2", "2.3"}


def test_duplicate_identical_evidence_is_deduplicated(harness, admin):
    _, repository, executor, sid, _ = harness()
    first = execute(executor, ToolName.FUND)
    second = execute(executor, ToolName.FUND)
    assert first.observation.observation_id != second.observation.observation_id
    assert first.evidence_refs == second.evidence_refs
    assert len(repository.list(first.case_id)) == 3
    assert repository.origin_observation_ids(first.case_id, first.evidence_refs[0]) == (
        first.observation.observation_id, second.observation.observation_id,
    )
    admin.advance(sid, 5)
    third = execute(executor, ToolName.FUND)
    assert third.evidence_refs != first.evidence_refs
    assert len(repository.list(first.case_id)) == 6


def test_state_change_over_time_is_not_conflict(harness, admin):
    _, repository, executor, sid, _ = harness(ScenarioId.S4)
    execute(executor, ToolName.GUARANTEE)
    admin.advance(sid, 10)
    execute(executor, ToolName.GUARANTEE)
    evidence = repository.list("CASE-JD202609100001")
    assert values(repository, C.GUARANTEE_STATUS) == ["PROCESSING", "SUCCESS"]
    assert not EvidenceConflictDetector().detect("CASE-JD202609100001", evidence)
    old = next(e for e in evidence if e.value == "PROCESSING")
    assert EvidenceFreshnessService().is_stale(old)
    assert EvidenceFreshnessService().age(old, T0 + timedelta(seconds=20)) == timedelta(seconds=18)


@pytest.mark.parametrize("claim,new_value", [(C.PAYMENT_AMOUNT, 1), (C.PAYMENT_CURRENCY, "USD"),
                                            (C.TRANSACTION_FUND_REQUEST_ID, "OTHER-REQUEST")])
def test_true_amount_conflict_is_detected(harness, claim, new_value):
    # Unit-level detector fixture: synthetic alternate source, never inserted into store.
    _, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.PAYMENT)
    first = next(e for e in repository.list(result.case_id) if e.claim_type == claim)
    second = Evidence.model_validate({**first.model_dump(), "evidence_id": "E-other",
                                      "observation_id": str(uuid4()), "value": new_value,
                                      "source_kind": SourceKind.REPLICA})
    conflicts = EvidenceConflictDetector().detect(result.case_id, (first, second))
    assert len(conflicts) == 1 and set(conflicts[0].evidence_refs) == {first.evidence_id, second.evidence_id}
    later = second.model_copy(update={"event_time": second.event_time + timedelta(seconds=1)})
    assert not EvidenceConflictDetector().detect(result.case_id, (first, later))
    stale = second.model_copy(update={"freshness": Freshness.STALE})
    assert not EvidenceConflictDetector().detect(result.case_id, (first, stale))
    same_source = second.model_copy(update={"source_kind": first.source_kind})
    assert not EvidenceConflictDetector().detect(result.case_id, (first, same_source))


@pytest.mark.parametrize("scenario", list(ScenarioId))
def test_ground_truth_never_leaks_into_evidence(harness, scenario):
    _, repository, executor, _, _ = harness(scenario)
    for tool in ToolName:
        execute(executor, tool)
    view = CaseEvidenceService(repository).get_case_evidence("CASE-JD202609100001")
    serialized = view.model_dump_json()
    for key in FORBIDDEN_KEYS:
        assert key not in serialized


def test_public_schemas_have_no_oracle_or_admin_route(harness):
    _, repository, executor, _, _ = harness()
    binding = HarnessBinding(executor, CaseEvidenceService(repository))
    app = create_harness_app({token_hash("harness-only"): binding})
    with TestClient(app) as client:
        schema = json.dumps(client.get("/openapi.json").json())
        for forbidden in (*FORBIDDEN_KEYS, "WorldState", "GroundTruth", "ScenarioId", "SimulatorAdmin"):
            assert forbidden not in schema
        for model in (Case, Evidence, CaseEvidenceView, CaseToolResult, RawObservation):
            serialized = json.dumps(model.model_json_schema())
            assert "WorldState" not in serialized and "GroundTruth" not in serialized
        assert client.get("/cases/CASE-JD202609100001").status_code == 401
        headers = {"Authorization": "Bearer harness-only"}
        assert client.get("/cases/CASE-JD202609100001", headers=headers).status_code == 200
        assert client.post("/cases/CASE-JD202609100001/tools/get_fund_order", headers=headers,
                           json=Q.model_dump(mode="json")).status_code == 200
        assert client.get("/cases/CASE-JD202609100001/evidence", headers=headers).status_code == 200
        for path in ("/admin", "/world", "/ground_truth", "/tools/get_fund_order"):
            assert client.get(path, headers=headers).status_code == 404
        assert client.post("/cases/CASE-JD202609100001/close", headers=headers).status_code == 404


def test_cross_tenant_and_case_provenance_isolation(harness, engine):
    _, repository, executor, sid, token = harness()
    result = execute(executor, ToolName.FUND)
    harness(existing=(sid, token), case_id="CASE-002")
    with pytest.raises(CaseAccessError):
        repository.get_raw_observation(result.evidence_refs[0], case_id="CASE-002")
    alien = CaseRepository(engine, "another-tenant")
    with pytest.raises(CaseAccessError):
        alien.get(result.case_id)
    with pytest.raises(CaseAccessError):
        EvidenceRepository(alien).get_raw_observation(result.evidence_refs[0], case_id=result.case_id)


def test_wrong_simulation_response_and_observation_replay_rejected(harness, query, seeded):
    cases, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.FUND)
    _, call = cases.reserve_call(result.case_id, ToolName.FUND, Q)
    with pytest.raises(ProvenanceError, match="already assigned"):
        repository.record_call(result.case_id, call, result.observation)
    _, other_token = seeded()
    wrong = query(other_token, ToolName.FUND)
    with pytest.raises(ProvenanceError, match="does not match"):
        repository.record_call(result.case_id, call, wrong)


def test_tampered_observation_rejected(harness, engine):
    _, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.PAYMENT)
    with Session(engine) as session, session.begin():
        row = session.get(ObservationRow, result.observation.observation_id)
        changed = json.loads(json.dumps(row.observation))
        changed["data"]["record"]["transaction"]["amount"] = 1
        row.observation = changed
    with pytest.raises(ProvenanceError, match="hash mismatch"):
        repository.get_raw_observation(result.evidence_refs[0], case_id=result.case_id)


def test_transport_exception_counts_attempt_without_fabricating_evidence(harness, engine):
    cases, repository, _, _, _ = harness(budget=1)
    class Broken:
        def observe(self, tool, query, *, dispatch_correlation_id):
            raise httpx.ReadTimeout("local HTTP failure")
    executor = CaseToolExecutor(cases, repository, lambda _: Broken())
    with pytest.raises(httpx.ReadTimeout):
        execute(executor, ToolName.PAYMENT)
    assert cases.get("CASE-JD202609100001").budget.used_tool_calls == 1
    assert not repository.list("CASE-JD202609100001")
    with Session(engine) as session:
        assert session.scalar(select(CaseCallRow)).state == CallState.ERROR.value


def test_models_forbid_extras_and_are_frozen(harness):
    cases, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.FUND)
    for model in (cases.get(result.case_id), repository.list(result.case_id)[0],
                  CaseEvidenceService(repository).get_case_evidence(result.case_id)):
        with pytest.raises(ValidationError):
            type(model).model_validate({**model.model_dump(), "ground_truth": {}})
        with pytest.raises(ValidationError):
            setattr(model, next(iter(type(model).model_fields)), "changed")


def test_new_timeout_does_not_turn_historical_payment_into_current_proof(harness, admin):
    _, repository, executor, sid, _ = harness()
    execute(executor, ToolName.PAYMENT)
    admin.add_fault(sid, ObservationFault(tool=ToolName.PAYMENT, kind=FaultKind.TIMEOUT, starts_at=T0))
    execute(executor, ToolName.PAYMENT)
    view = CaseEvidenceService(repository).get_case_evidence("CASE-JD202609100001")
    assert values(repository, C.PAYMENT_FINALITY) == ["SETTLED"]  # history is not deleted
    assert view.payment_finality_evidence.knowledge == KnowledgeStatus.UNKNOWN


def test_concurrent_identical_claims_keep_all_origins(harness):
    cases, repository, executor, _, _ = harness(budget=4)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(executor, ToolName.FUND), range(4)))
    assert len({r.evidence_refs for r in results}) == 1
    assert len(repository.list(results[0].case_id)) == 3
    assert len(repository.origin_observation_ids(results[0].case_id, results[0].evidence_refs[0])) == 4
    assert cases.get(results[0].case_id).budget.used_tool_calls == 4


def test_extraction_is_deterministic_and_has_no_business_conclusion(harness):
    cases, _, executor, _, _ = harness()
    result = execute(executor, ToolName.FUND)
    case = cases.get(result.case_id)
    extractor = EvidenceExtractor()
    first = extractor.extract(case, result.observation)
    second = extractor.extract(case, result.observation)
    assert first == second
    assert {e.claim_type for e in first} == {C.FUND_BUSINESS_STATUS, C.LOAN_NO_PRESENT, C.LOAN_NOTE_REFERENCE}
    for e in first:
        assert not set(FORBIDDEN_KEYS).intersection(e.model_dump())


def test_freshness_unknown_has_no_invented_age(harness):
    _, repository, executor, _, _ = harness(ScenarioId.S8)
    execute(executor, ToolName.PAYMENT)
    e = repository.list("CASE-JD202609100001")[0]
    service = EvidenceFreshnessService()
    assert e.freshness == Freshness.UNKNOWN
    assert not service.is_stale(e)
    assert service.age(e, T0) is None
    with pytest.raises(ValueError, match="timezone"):
        service.age(e, T0.replace(tzinfo=None))


def test_harness_repositories_reopen_persisted_case(harness, engine):
    _, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.FUND)
    # Fresh repository/session objects use no in-memory Case or Evidence cache.
    reopened_cases = CaseRepository(engine, "demo")
    reopened_evidence = EvidenceRepository(reopened_cases)
    assert reopened_cases.get(result.case_id).budget.used_tool_calls == 1
    assert reopened_evidence.list(result.case_id) == repository.list(result.case_id)
    assert reopened_evidence.get_raw_observation(result.evidence_refs[0], case_id=result.case_id).observation == result.observation


def test_observation_is_bound_to_dispatch_correlation(harness, engine):
    _, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.FUND)
    with Session(engine) as session:
        call = session.get(CaseCallRow, result.call_id)
        row = session.get(ObservationRow, result.observation.observation_id)
        assert call.dispatch_correlation_id == result.call_id == row.dispatch_correlation_id
        assert call.observation_id == row.id and call.state == CallState.OBSERVED.value
    assert result.evidence_refs and repository.list(result.case_id)


def test_precreated_unassigned_observation_cannot_be_attached(harness, query, engine):
    cases, repository, _, _, token = harness()
    # O1 is valid, committed and unassigned, but it predates the new dispatch C2.
    old = query(token, ToolName.FUND)
    case, call_id = cases.reserve_call("CASE-JD202609100001", ToolName.FUND, Q)
    with Session(engine) as session:
        call = session.get(CaseCallRow, call_id)
        row = session.get(ObservationRow, old.observation_id)
        assert row.simulation_id == case.simulation_id
        assert row.grant_hash == token_hash(token)
        assert row.tool == call.tool and row.request == call.request
        assert session.scalar(select(CaseCallRow.call_id).where(CaseCallRow.observation_id == row.id)) is None
    with pytest.raises(ProvenanceError, match="dispatch correlation"):
        repository.record_call(case.case_id, call_id, old)
    assert not repository.list(case.case_id)
    with Session(engine) as session:
        assert session.get(CaseCallRow, call_id).observation_id is None


def test_wrong_dispatch_correlation_rejected(harness, client, engine):
    cases, repository, _, _, token = harness()
    case, call_id = cases.reserve_call("CASE-JD202609100001", ToolName.FUND, Q)
    other = str(uuid4())
    observation = HTTPTestToolClient(client, token).observe(ToolName.FUND, Q, dispatch_correlation_id=other)
    with Session(engine) as session:
        assert session.get(CaseCallRow, call_id).dispatch_correlation_id != other
        assert session.get(ObservationRow, observation.observation_id).dispatch_correlation_id == other
    with pytest.raises(ProvenanceError, match="dispatch correlation"):
        repository.record_call(case.case_id, call_id, observation)
    assert not repository.list(case.case_id)


@pytest.mark.parametrize("missing", ["call", "observation", "both"])
def test_missing_dispatch_correlation_rejected(harness, client, engine, missing):
    cases, repository, _, _, token = harness()
    case, call_id = cases.reserve_call("CASE-JD202609100001", ToolName.FUND, Q)
    observation = HTTPTestToolClient(client, token).observe(ToolName.FUND, Q, dispatch_correlation_id=call_id)
    with Session(engine) as session, session.begin():
        if missing in ("call", "both"):
            session.get(CaseCallRow, call_id).dispatch_correlation_id = None
        if missing in ("observation", "both"):
            session.get(ObservationRow, observation.observation_id).dispatch_correlation_id = None
    with pytest.raises(ProvenanceError, match="dispatch correlation"):
        repository.record_call(case.case_id, call_id, observation)


@pytest.mark.parametrize("scenario", [ScenarioId.S6, ScenarioId.S8])
def test_correlation_does_not_leak_ground_truth(harness, client, query, engine, scenario):
    from tests.support.inspector import world_state

    _, repository, executor, sid, token = harness(scenario)
    before = world_state(engine, sid)
    for tool in ToolName:
        uncorrelated = query(token, tool)
        correlated = execute(executor, tool).observation
        assert uncorrelated.model_dump(exclude={"observation_id"}) == correlated.model_dump(exclude={"observation_id"})
        assert "dispatch_correlation_id" not in correlated.model_dump()
    assert world_state(engine, sid) == before
    view = CaseEvidenceService(repository).get_case_evidence("CASE-JD202609100001")
    serialized = view.model_dump_json()
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in serialized
    schema = json.dumps(client.get("/openapi.json").json())
    assert "GroundTruth" not in schema and "WorldState" not in schema


def test_s6_schema_type_evidence(harness):
    _, repository, executor, _, _ = harness()
    result = execute(executor, ToolName.MESSAGES)
    expected = {
        C.MESSAGE_ERROR_CODE: ("CALLBACK_SCHEMA_MISMATCH", "code"),
        C.MESSAGE_ERROR_FIELD: ("loanNo", "field"),
        C.MESSAGE_EXPECTED_FIELD_TYPE: ("integer", "expected_type"),
        C.MESSAGE_ACTUAL_FIELD_TYPE: ("string", "actual_type"),
    }
    evidence = repository.list(result.case_id)
    assert len(evidence) == 6
    for claim, (value, source_field) in expected.items():
        e = next(e for e in evidence if e.claim_type == claim)
        assert e.value == value and e.subject.identifier == "MSG-001"
        assert e.observation_id == result.observation.observation_id
        assert e.metadata.source_path == f"/data/records/0/error/{source_field}"
        raw = repository.get_raw_observation(e.evidence_id, case_id=result.case_id)
        assert getattr(raw.observation.data.records[0].error, source_field) == value
    assert not {"ROOT_CAUSE", "CONSUMER_USES_V2_2", "H6"}.intersection(C)
    assert "OLD_PARSER" not in " ".join(e.model_dump_json() for e in evidence)


def test_agent_cannot_choose_dispatch_correlation(harness, engine):
    _, repository, executor, _, _ = harness()
    binding = HarnessBinding(executor, CaseEvidenceService(repository))
    chosen = str(uuid4())
    headers = {"Authorization": "Bearer harness-only", DISPATCH_CORRELATION_HEADER: chosen}
    with TestClient(create_harness_app({token_hash("harness-only"): binding})) as client:
        response = client.post("/cases/CASE-JD202609100001/tools/get_fund_order",
                               headers=headers, json=Q.model_dump(mode="json"))
        assert response.status_code == 200
        result = CaseToolResult.model_validate(response.json())
        assert result.call_id != chosen
        with Session(engine) as session:
            row = session.get(ObservationRow, result.observation.observation_id)
            assert row.dispatch_correlation_id == result.call_id
        response = client.post("/cases/CASE-JD202609100001/tools/get_fund_order", headers=headers,
                               json={**Q.model_dump(mode="json"), "dispatch_correlation_id": chosen})
        assert response.status_code == 422


@pytest.mark.parametrize("bad", ["", "arbitrary", "00000000-0000-0000-0000-000000000000", "x" * 500])
def test_tool_api_validates_correlation_header(client, seeded, bad):
    _, token = seeded()
    response = client.post(f"/tools/{ToolName.FUND.value}", json=Q.model_dump(mode="json"),
                           headers={"Authorization": f"Bearer {token}", DISPATCH_CORRELATION_HEADER: bad})
    assert response.status_code == 422


def test_correlation_does_not_replace_tool_authorization(client, seeded):
    _, token = seeded(tools={ToolName.FUND})
    headers = {DISPATCH_CORRELATION_HEADER: str(uuid4())}
    assert client.post(f"/tools/{ToolName.FUND.value}", json=Q.model_dump(mode="json"), headers=headers).status_code == 401
    headers["Authorization"] = f"Bearer {token}"
    assert client.post(f"/tools/{ToolName.PAYMENT.value}", json=Q.model_dump(mode="json"), headers=headers).status_code == 403


def test_tool_client_injects_per_request_correlation(seeded, query, monkeypatch):
    from credit_harness.tools.client import ToolClient

    _, token = seeded()
    observation = query(token, ToolName.FUND)
    sent = []
    def post(url, **kwargs):
        sent.append(kwargs)
        return httpx.Response(200, json=observation.model_dump(mode="json"),
                              request=httpx.Request("POST", "http://simulator" + url))
    with ToolClient("http://simulator", token) as client:
        monkeypatch.setattr(client._http, "post", post)
        for correlation in (str(uuid4()), str(uuid4()), None):
            assert client.observe(ToolName.FUND, Q, dispatch_correlation_id=correlation) == observation
            assert sent[-1]["headers"] == ({DISPATCH_CORRELATION_HEADER: correlation} if correlation else {})
            assert sent[-1]["json"] == Q.model_dump(mode="json")
        assert DISPATCH_CORRELATION_HEADER not in client._http.headers
        with pytest.raises(ValidationError):
            client.observe(ToolName.FUND, Q, dispatch_correlation_id="invalid")
        assert len(sent) == 3


def test_additive_correlation_upgrade_preserves_legacy_rows(harness, query, engine):
    from credit_harness.persistence.store import create_schema

    cases, repository, _, _, token = harness()
    old = query(token, ToolName.FUND)
    case, legacy_call_id = cases.reserve_call("CASE-JD202609100001", ToolName.FUND, Q)
    # This fixture database is isolated per test. Remove only the newly introduced
    # columns to reproduce an existing Step 2 DB; keep all original tables/data.
    with engine.begin() as connection:
        schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
        quote = connection.dialect.identifier_preparer.quote
        for table in ("tool_observations", "case_tool_calls"):
            qualified = f"{quote(schema)}.{quote(table)}" if schema else quote(table)
            # Step 9 adds correlation lookup indexes. SQLite requires removing
            # the dependent index before reproducing the pre-correlation schema.
            from sqlalchemy import inspect
            for index in inspect(connection).get_indexes(table, schema=schema):
                if index["column_names"] == ["dispatch_correlation_id"]:
                    index_name = f"{quote(schema)}.{quote(index['name'])}" if schema else quote(index["name"])
                    connection.exec_driver_sql(f"DROP INDEX {index_name}")
            connection.exec_driver_sql(f"ALTER TABLE {qualified} DROP COLUMN dispatch_correlation_id")
    create_schema(engine)
    create_harness_schema(engine)
    create_schema(engine)  # idempotent upgrade
    with Session(engine) as session:
        row = session.get(ObservationRow, old.observation_id)
        assert row.observation == old.model_dump(mode="json")
        assert row.content_hash == json_hash(row.observation)
        assert row.dispatch_correlation_id is None
        assert session.get(CaseCallRow, legacy_call_id).dispatch_correlation_id is None
    with pytest.raises(ProvenanceError, match="dispatch correlation"):
        repository.record_call(case.case_id, legacy_call_id, old)
    _, new_call = cases.reserve_call(case.case_id, ToolName.FUND, Q)
    with Session(engine) as session:
        assert session.get(CaseCallRow, new_call).dispatch_correlation_id == new_call
