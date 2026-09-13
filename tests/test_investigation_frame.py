import pytest
from fastapi.testclient import TestClient
from scripts.demo_case_evidence import run_demo
from credit_harness.api.ui import create_ui_app
from credit_harness.cases.repository import CaseRepository
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.domain.enums import ScenarioId
from credit_harness.persistence.store import token_hash
from credit_harness.investigation.service import InvestigationFrameService
from credit_harness.investigation.models import FrameStale
from credit_harness.benchmark.fixture import EvaluationFixture
from credit_harness.domain.enums import ToolName as T
from tests.test_case_evidence import harness
from tests.test_route_revisions import routed, protocol, promote
from tests.test_registry import publish
from tests.test_agent_runtime import setup_agent

CASE = "CASE-JD202609100001"


@pytest.fixture
def service(engine):
    run_demo(engine)
    return InvestigationFrameService(EvidenceRepository(CaseRepository(engine, "demo")))


def test_frame_bound_to_single_case_revision(service):
    f = service.build(CASE)
    assert f.case_revision == f.case_summary.updated_at == service.repository.cases.get(CASE).updated_at
    assert f.evidence.frame_id == f.timeline.frame_id == f.frame_id
    assert f.financial_identity.result == "MATCH"
    assert f.case_summary.internal_order_id.startswith("ORDER-")


def test_frame_retries_or_stales_on_concurrent_evidence(service, monkeypatch):
    original = service._read
    count = 0
    def changed(case_id):
        nonlocal count
        count += 1
        a, b, c, d = original(case_id)
        return a, b, c, str(count)
    monkeypatch.setattr(service, "_read", changed)
    with pytest.raises(FrameStale): service.build(CASE)
    assert count == 6


def test_unknown_payment_stays_unknown(engine):
    run_demo(engine, ScenarioId.S8)
    f = InvestigationFrameService(EvidenceRepository(CaseRepository(engine, "demo"))).build(CASE)
    assert next(t.value for t in f.financial_truth if t.claim_type == "PAYMENT_FINALITY") == "UNKNOWN"
    assert f.financial_identity.result == "UNKNOWN"
    assert not f.closure_trace.items


def test_tool_call_links_to_produced_evidence(service):
    f = service.build(CASE)
    ids = {e.evidence_id for e in service.page(CASE, f.frame_id, "evidence", limit=100).items}
    assert all(set(t.evidence_refs) <= ids for t in f.tool_trace.items)
    assert all(t.related_refs for t in f.tool_trace.items)


def test_frame_cursor_is_section_and_case_bound(service):
    f = service.build(CASE)
    p = service.page(CASE, f.frame_id, "evidence", limit=1)
    assert p.next_cursor
    q = service.page(CASE, f.frame_id, "evidence", p.next_cursor, 1)
    assert q.items[0] != p.items[0]
    with pytest.raises(FrameStale): service.page(CASE, f.frame_id, "tools", p.next_cursor)


@pytest.mark.parametrize("forbidden", ["310101199001011234", "6222021234567890123", "13800138000",
    "raw_callback", "credential_ref", "embedding_vector", "private_reasoning", "source_case_id",
    "lease_token", "ground_truth", "simulation_id", "dispatch_correlation_id"])
def test_sensitive_domains_never_in_frame(service, forbidden):
    assert forbidden not in service.build(CASE).model_dump_json()


def test_cross_tenant_case_denied(service):
    other = EvidenceRepository(CaseRepository(service.repository.engine, "other"))
    app = create_ui_app({token_hash("test"): other})
    with TestClient(app) as client:
        assert client.get(f"/ui/cases/{CASE}/frame", headers={"Authorization": "Bearer test"}).status_code == 404


@pytest.mark.parametrize("permissions", [{"REGISTRY_ADMIN"}, {"CASE_VIEW"}, {"CASE_VIEW", "CASE_TRACE_VIEW"}])
def test_independent_investigation_permissions(service, permissions):
    key = token_hash("test")
    app = create_ui_app({key: service.repository}, permissions={key: frozenset(permissions)})
    with TestClient(app) as client:
        assert client.get(f"/ui/cases/{CASE}/frame", headers={"Authorization": "Bearer test"}).status_code == 403


@pytest.fixture
def investigation(engine, monkeypatch):
    monkeypatch.setenv('CAPABILITY_SIGNING_SECRET', 'synthetic-step16-test-signing-key-only')
    x = EvaluationFixture(engine)
    yield x
    x.close()


def frame_for(x):
    return InvestigationFrameService(x.evidence).build(x.case.case_id)


def test_frame_does_not_mix_evaluation_after_new_evidence(investigation):
    x = investigation
    x.read()
    x.evaluate()
    before = frame_for(x)
    assert before.evaluation_trace.items
    x.read(T.PAYMENT)
    after = frame_for(x)
    assert before.evidence_fingerprint != after.evidence_fingerprint
    assert all(e.historical for e in after.evaluation_trace.items)


def test_evaluator_requirements_visible(investigation):
    x = investigation
    x.read()
    report = x.evaluate()
    f = frame_for(x)
    assert {next(v.value for v in e.fields if v.name == 'dimension') for e in f.evaluation_trace.items} == {
        'MONEY', 'IDENTITY', 'STATE_CONVERGENCE', 'SIDE_EFFECT', 'EVIDENCE_SUFFICIENCY', 'POLICY', 'RECOVERY', 'OUTCOME'}
    assert all(r.requirement.value in f.model_dump_json() for r in report.unresolved_requirements)


def test_closed_only_from_verified_closure(investigation):
    x = investigation
    x.read()
    x.progress()
    x.read()
    report = x.evaluate()
    assert report.overall_verdict == 'PASS'
    assert not frame_for(x).closure_trace.items
    x.closure.close(report)
    f = frame_for(x)
    assert f.case_summary.status == 'CLOSED_VERIFIED'
    assert f.closure_trace.items[0].status == 'CLOSED_VERIFIED'


def test_applied_not_rendered_as_verified(investigation):
    x = investigation
    x.read()
    x.remediate()
    f = frame_for(x)
    assert f.side_effect_trace.items[0].status == 'APPLIED'
    assert f.side_effect_trace.items[0].warning == 'APPLIED ≠ VERIFIED'
    assert not f.closure_trace.items


def test_recovery_unknown_warning(investigation):
    x = investigation
    x.read()
    x.remediate(timeout=True)
    f = frame_for(x)
    assert any(e.status == 'UNKNOWN' and '盲目重试' in e.warning for e in f.recovery_trace.items)


def test_fund_success_not_rendered_as_payment_finality(investigation):
    x = investigation
    x.read(T.FUND)
    f = frame_for(x)
    assert next(t.value for t in f.financial_truth if t.claim_type == 'FUND_BUSINESS_STATUS') == 'SUCCESS'
    assert next(t.value for t in f.financial_truth if t.claim_type == 'PAYMENT_FINALITY') == 'UNKNOWN'


def test_tool_result_not_rendered_as_business_truth(investigation):
    x = investigation
    x.read(T.TRACE)
    f = frame_for(x)
    assert all(t.value == 'UNKNOWN' for t in f.financial_truth)


def test_history_never_appears_as_current_truth(service):
    from credit_harness.investigation.trace_store import SQLInvestigationTraceStore, InvestigationTraceRow
    from credit_harness.retrieval.models import RetrievalTelemetry
    InvestigationTraceRow.__table__.create(service.repository.engine, checkfirst=True)
    SQLInvestigationTraceStore(service.repository.cases).record_retrieval(case_id=CASE, snapshot_id='a'*64,
        telemetry=RetrievalTelemetry(selected_experience_ids=('b'*64,), degradation='VECTOR_UNAVAILABLE'))
    f = service.build(CASE)
    assert f.knowledge_retrieval_trace.items[0].historical
    assert 'VECTOR_UNAVAILABLE' in f.knowledge_retrieval_trace.model_dump_json()
    assert 'EXPERIENCE-' not in str(f.current_evidence)


def test_retrieval_trace_shows_ids_not_vectors(service):
    test_history_never_appears_as_current_truth(service)
    f = service.build(CASE)
    text = f.knowledge_retrieval_trace.model_dump_json()
    assert 'EXPERIENCE-' in text and 'latency_ms' in text
    assert 'query_text' not in text and 'embedding_vector' not in text


def test_frame_http_read_only_and_detail_binding(service):
    app = create_ui_app({token_hash('test'): service.repository})
    with TestClient(app) as client:
        headers = {'Authorization':'Bearer test'}
        f = client.get(f'/ui/cases/{CASE}/frame', headers=headers).json()
        eid = f['evidence']['items'][0]['evidence_id']
        assert client.get(f'/ui/cases/{CASE}/evidence-items/{eid}', params={'frame_id':f['frame_id']}, headers=headers).status_code == 200
        assert client.get(f'/ui/cases/{CASE}/evidence-items/{eid}', params={'frame_id':'other'}, headers=headers).status_code == 409
        assert client.post(f'/ui/cases/{CASE}/frame', headers=headers).status_code == 405


def test_frame_does_not_mix_old_and_new_route(routed):
    x = routed()
    refs = protocol(x)
    s = InvestigationFrameService(x.evidence)
    old = s.build(x.case.case_id)
    changed = False
    def mutate():
        nonlocal changed
        if not changed:
            changed = True
            promote(x, refs)
    s.after_read = mutate
    new = s.build(x.case.case_id)
    assert new.route_revision != old.route_revision
    assert new.route_revision == x.repo.route_revision(x.case.case_id).route_revision_id
    assert len(new.route_trace.items) == 2


def test_frame_does_not_mix_old_and_new_registry_version(routed):
    x = routed()
    s = InvestigationFrameService(x.evidence)
    old = s.build(x.case.case_id)
    changed = False
    def mutate():
        nonlocal changed
        if not changed:
            changed = True
            publish(x, systems=[system.model_copy(update={'display_name':'Updated system'}) for system in x.definition.systems])
    s.after_read = mutate
    new = s.build(x.case.case_id)
    assert new.registry_version != old.registry_version
    assert new.registry_version == x.repo.current()[0]


def test_evidence_links_to_safe_registry_source(routed):
    x = routed()
    from credit_harness.tools.contracts import ToolQuery
    x.executor.execute(x.case.case_id, T.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))
    f = InvestigationFrameService(x.evidence).build(x.case.case_id)
    assert len(f.registry_source_trace.items) == 1
    source = f.registry_source_trace.items[0]
    assert source.evidence_refs == f.tool_trace.items[0].evidence_refs
    assert any(v.name == 'system_id' and v.value == 'sim-payment-sushang' for v in source.fields)
    assert 'credential_ref' not in f.model_dump_json()


def test_protocol_route_revision_visible(routed):
    x = routed()
    refs = protocol(x)
    promote(x, refs)
    f = InvestigationFrameService(x.evidence).build(x.case.case_id)
    assert any(v.name == 'protocol_version' and v.value == '2.3' for v in f.route_summary)
    assert set(refs) <= {r for t in f.route_trace.items for r in t.evidence_refs}


def test_default_agent_trace_is_durable_and_safe(setup_agent):
    runtime = setup_agent()
    result = runtime.run(CASE)
    f = InvestigationFrameService(runtime.evidence).build(CASE)
    assert f.latest_agent_run
    assert f.planner_trace.items
    text = f.planner_trace.model_dump_json()
    assert 'rejections' in text and 'rank_priority' in text and 'selected' in text
    assert all(word not in text for word in ('execution_precondition','lease_token','"query":','system_contract'))
    assert len(runtime.trace_store.records) == 1
    assert result.case_id == f.case_id


def test_frame_retries_real_evidence_publication(investigation):
    x = investigation
    x.read(T.TRACE)
    s = InvestigationFrameService(x.evidence)
    called = False
    def publish_once():
        nonlocal called
        if not called:
            called = True
            x.read(T.PAYMENT)
    s.after_read = publish_once
    f = s.build(CASE)
    assert f.case_revision == x.cases.get(CASE).updated_at
    assert f.financial_identity.result == 'MATCH'
    assert f.case_summary.budget.used_tool_calls == 2


def test_repeated_calls_preserve_deduplicated_evidence_origins(investigation):
    x = investigation
    x.read(T.PAYMENT)
    x.read(T.PAYMENT)
    f = frame_for(x)
    assert len(f.tool_trace.items) == 2
    assert all(t.evidence_refs for t in f.tool_trace.items)


def test_guidance_early_failure_does_not_rebind_previous_retrieval():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from credit_harness.investigation.trace_store import TracedGuidanceProvider
    provider = Mock()
    provider.build_result.return_value = SimpleNamespace(bundle=None)
    store = Mock()
    hybrid = SimpleNamespace(telemetry_revision=1, last_telemetry=object())
    TracedGuidanceProvider(provider, store, hybrid=hybrid).build_result(SimpleNamespace(case_id='CURRENT', snapshot_id='b'*64))
    store.record_retrieval.assert_not_called()


def test_recovery_change_invalidates_current_evaluation(investigation):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from credit_harness.recovery.tables import EffectRecoveryStateRow
    x = investigation
    x.read()
    x.remediate(timeout=True)
    x.evaluate()
    assert any(not e.historical for e in frame_for(x).evaluation_trace.items)
    with Session(x.engine) as session, session.begin():
        row = session.scalar(select(EffectRecoveryStateRow).where(EffectRecoveryStateRow.case_id == CASE))
        row.attempt_count += 1
    assert all(e.historical for e in frame_for(x).evaluation_trace.items)


def test_production_console_does_not_mount_legacy_diagnostics(service):
    from credit_harness.api.ui import create_production_ui_app
    with TestClient(create_production_ui_app({token_hash('test'):service.repository})) as client:
        headers = {'Authorization':'Bearer test'}
        assert client.get(f'/ui/cases/{CASE}/frame', headers=headers).status_code == 200
        for suffix in ('', '/evidence', '/hypotheses', '/reasoning-context'):
            assert client.get(f'/ui/cases/{CASE}' + suffix, headers=headers).status_code == 404
