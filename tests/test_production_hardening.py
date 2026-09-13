"""Production seams: shared storage, authority, failure and shutdown, offline."""
import base64
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from sqlalchemy import select, func, update, event
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from tests.test_investigation_frame import service, CASE
from tests.test_organizational_memory import factory, context, guidance_setup
from credit_harness.investigation.cache import SQLFrameCache, InvestigationFrameRow
from credit_harness.investigation.service import InvestigationFrameService
from credit_harness.investigation.models import FrameStale, TraceTrustClass, TraceKind
from credit_harness.investigation.privacy import alias_scope, alias
from credit_harness.investigation.trace_store import SQLInvestigationTraceStore, InvestigationTraceRow
from credit_harness.production.workers import WorkerLoop
from credit_harness.production.settings import InvestigationApiSettings, StartupConfigurationError
from credit_harness.production.telemetry import SafeJSONFormatter, sanitize_log
from credit_harness.api.ui import create_production_ui_app
from credit_harness.persistence.store import token_hash, create_schema

KEY = b'synthetic-frame-test-key-000000001'
NEW = b'synthetic-frame-test-key-000000002'


def shared(service, *, key=KEY, previous=None, clock=None):
    InvestigationFrameRow.__table__.create(service.repository.engine, checkfirst=True)
    cache = SQLFrameCache(service.repository.engine, **({'clock': clock} if clock else {}))
    return InvestigationFrameService(service.repository, cache=cache, cursor_key=key,
        previous_cursor_key=previous, alias_key=KEY)


def test_multi_instance_frame_and_cursor_rotation(service):
    a = shared(service); frame = a.build(CASE)
    first = a.page(CASE, frame.frame_id, 'evidence', limit=1)
    b = shared(service, key=NEW, previous=KEY)
    second = b.page(CASE, frame.frame_id, 'evidence', first.next_cursor, 1)
    assert second.items[0] != first.items[0]
    assert second.total == first.total == frame.evidence.total
    assert second.eligibility_denied_count == first.eligibility_denied_count
    with pytest.raises(FrameStale):
        shared(service, key=NEW).page(CASE, frame.frame_id, 'evidence', first.next_cursor, 1)


def test_twenty_concurrent_frames_are_identical(service,monkeypatch):
    # All requests belong to one cache epoch; expiry is covered separately.
    monkeypatch.setattr('credit_harness.investigation.service.time',lambda:1800000000.)
    shared(service)
    def build(_):
        return shared(service).build(CASE).model_dump(mode='json')
    with ThreadPoolExecutor(max_workers=20) as pool:
        frames = list(pool.map(build, range(20)))
    assert all(frame == frames[0] for frame in frames)
    with Session(service.repository.engine) as session:
        assert session.scalar(select(func.count()).select_from(InvestigationFrameRow)) == 1


def test_shared_frame_expiry_does_not_renew_on_read(service):
    now = [100.]
    s = shared(service, clock=lambda: now[0]); f = s.build(CASE)
    now[0] = 401.
    with pytest.raises(FrameStale, match='FRAME_EXPIRED'):
        s.page(CASE, f.frame_id, 'evidence')
    assert s.cache.prune() == 1


def test_shared_cache_rejects_tamper_and_foreign_tenant(service):
    s = shared(service); f = s.build(CASE)
    with pytest.raises(FrameStale):
        s.cache.get('other', CASE, f.frame_id)
    with service.repository.engine.begin() as c:
        c.execute(update(InvestigationFrameRow).values(content_hash='0'*64))
    with pytest.raises(FrameStale, match='FRAME_CORRUPT'):
        s.page(CASE, f.frame_id, 'evidence')


def test_denied_count_uses_verified_minus_eligible_and_survives_paging(service,monkeypatch):
    from credit_harness.hypotheses.engine import HypothesisEngine
    case,read,_,_ = service._read(CASE)
    graph = HypothesisEngine().evaluate(case,read.evidence)
    required = {r for h in graph.hypotheses for r in (*h.supporting_evidence_refs,
        *h.contradicting_evidence_refs,*h.decisive_evidence_refs)} | set(graph.payment_identity.evidence_refs)
    denied = next(e.evidence_id for e in read.evidence if e.evidence_id not in required)
    original = service.assembler.eligibility.allows
    monkeypatch.setattr(type(service.assembler.eligibility),'allows',lambda self,e: e.evidence_id != denied and original(e))
    f = service.build(CASE)
    assert f.evidence.eligibility_denied_count == 1
    assert f.evidence.total == len(read.evidence)-1
    page = service.page(CASE,f.frame_id,'evidence',limit=1)
    assert page.eligibility_denied_count==1 and page.total==f.evidence.total


def test_tenant_scoped_alias_is_stable_and_private():
    with alias_scope('one', KEY):
        a = alias('MSG-13800138000', 'MESSAGE')
        assert a == alias('MSG-13800138000', 'MESSAGE')
        assert a != alias('MSG-13800138000', 'ORDER')
    with alias_scope('two', KEY):
        assert a != alias('MSG-13800138000', 'MESSAGE')
    assert '13800138000' not in a


def test_legacy_persisted_sha_alias_is_rekeyed_on_frame_read(service):
    from credit_harness.investigation.models import TraceItem,TraceField
    old = 'EXPERIENCE-'+'a'*20
    item = TraceItem(trace_id='KnowledgeRetrieval-'+'b'*20,kind=TraceKind.KNOWLEDGE,
        status='RECORDED',fields=(TraceField(name='experience_alias',value=old),))
    InvestigationTraceRow.__table__.create(service.repository.engine,checkfirst=True)
    with Session(service.repository.engine) as s,s.begin():
        s.add(InvestigationTraceRow(trace_id='legacy-row',case_id=CASE,tenant_id='demo',
            projection_version='1',payload=item.model_dump(mode='json')))
    frame = service.build(CASE)
    assert old not in frame.model_dump_json() and item.trace_id not in frame.model_dump_json()
    with Session(service.repository.engine) as s:
        assert s.get(InvestigationTraceRow,'legacy-row').payload==item.model_dump(mode='json')


def test_legacy_rekey_is_tenant_bound_and_current_projection_is_unchanged():
    from credit_harness.investigation.projection import stored_trace
    from credit_harness.investigation.models import TraceItem,TraceField
    item = TraceItem(trace_id='legacy',kind=TraceKind.KNOWLEDGE,status='RECORDED',
        fields=(TraceField(name='embedding_space',value='SPACE-'+'a'*20),))
    with alias_scope('a',KEY): first = stored_trace(item.model_dump(),'1')
    with alias_scope('b',KEY): second = stored_trace(item.model_dump(),'1')
    assert first.trace_id!=second.trace_id and first.fields!=second.fields
    assert stored_trace(item.model_dump(),'2')==item
    with pytest.raises(ValueError): stored_trace(item.model_dump(),'unrecognized')


@pytest.mark.parametrize('permission,allowed,denied', [
    ('CASE_TRACE_VIEW', 'tools', 'evidence-page'),
    ('CASE_FINANCIAL_VIEW', 'evidence-page', 'tools')])
def test_section_permissions_are_independent(service, permission, allowed, denied):
    full, limited = token_hash('full'), token_hash('limited')
    app = create_production_ui_app({full: service.repository, limited: service.repository},
        permissions={full: frozenset({'CASE_VIEW','CASE_TRACE_VIEW','CASE_FINANCIAL_VIEW'}),
                     limited: frozenset({'CASE_VIEW',permission})})
    with TestClient(app) as client:
        f = client.get(f'/ui/cases/{CASE}/frame', headers={'Authorization':'Bearer full'}).json()
        headers = {'Authorization':'Bearer limited'}
        assert client.get(f'/ui/cases/{CASE}/summary', headers=headers).status_code == 200
        assert client.get(f'/ui/cases/{CASE}/frame', headers=headers).status_code == 403
        for endpoint, code in [(allowed,200),(denied,403)]:
            assert client.get(f'/ui/cases/{CASE}/{endpoint}', params={'frame_id':f['frame_id']}, headers=headers).status_code == code


def test_skill_is_organizational_not_historical(factory):
    x = factory(); snapshot = context(x); bundle = guidance_setup(x).build(snapshot)
    store = SQLInvestigationTraceStore(x.cases, alias_key=KEY); store.create_schema()
    store.record_guidance(snapshot, bundle); store.record_guidance(snapshot, bundle)
    with Session(x.engine) as s:
        rows = list(s.scalars(select(InvestigationTraceRow)))
        assert len(rows) == 1
        assert rows[0].payload['trace_trust_class'] == 'ORGANIZATIONAL_GUIDANCE'


def test_closing_pass_has_current_closure_class(factory):
    x = factory(closed=True)
    f = InvestigationFrameService(x.evidence).build(x.case.case_id)
    assert f.closure_trace.items
    assert any(t.trace_trust_class == TraceTrustClass.CLOSURE_BOUND_CURRENT_EVALUATION
               and not t.historical for t in f.evaluation_trace.items)


@pytest.mark.parametrize('field', ['closure_id','case_id','evaluation_run_id','report_id',
    'verification_snapshot_id','evidence_fingerprint','ledger_fingerprint',
    'evaluation_policy_version','contract_version'])
def test_closure_full_binding_rejects_tamper(factory, field):
    from credit_harness.evaluation.tables import CaseClosureRow
    x = factory(closed=True)
    with Session(x.engine) as s, s.begin():
        row = s.scalar(select(CaseClosureRow))
        payload = dict(row.payload); payload[field] = 'tampered'; row.payload = payload
    with pytest.raises((ValueError, RuntimeError)):
        InvestigationFrameService(x.evidence).build(x.case.case_id)


def test_runtime_schema_bootstrap_never_executes_ddl(engine):
    statements = []
    def capture(conn, cursor, sql, *args): statements.append(sql)
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        create_schema(engine.execution_options(production_runtime=True))
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert statements == []


def test_production_config_fail_fast_and_safe(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('FRAME_CURSOR_HMAC_KEY', 'sk-secret-do-not-print')
    with pytest.raises(StartupConfigurationError) as e: InvestigationApiSettings.from_env()
    assert str(e.value) == 'PRODUCTION_CONFIGURATION_INVALID'


def test_log_sanitizer_never_formats_sensitive_body():
    record = logging.LogRecord('httpx', 40, '', 1, 'Bearer %s', ('sk-secret',), None)
    record.event = 'request_completed'; record.evidence = {'mobile':'13800138000'}
    output = SafeJSONFormatter().format(record)
    assert 'request_completed' in output
    assert all(v not in output for v in ('Bearer','sk-secret','13800138000','evidence'))
    assert sanitize_log({'error_code':'MSG-13800138000','duration':float('inf')}) == {}


def test_shutdown_stops_claim_and_bounds_inflight_wait():
    started, release = threading.Event(), threading.Event()
    calls = []
    def tick(): calls.append(1); started.set(); release.wait(5)
    loop = WorkerLoop(tick, interval=.01, grace=.05)
    worker = threading.Thread(target=loop.run); worker.start()
    assert started.wait(2)
    loop.shutdown(); worker.join(2)
    release.set()
    assert not worker.is_alive() and calls == [1]


def test_worker_survives_tick_failure_without_blind_effect_retry():
    calls = []
    def tick():
        calls.append(1); loop.shutdown(); raise ConnectionError('synthetic DB disconnect')
    loop = WorkerLoop(tick, interval=.01)
    loop.run()
    assert calls == [1]


def test_production_contract_excludes_legacy_schema():
    paths = create_production_ui_app({}).openapi()['paths']
    assert not any(p.endswith(('/evidence','/hypotheses','/reasoning-context')) for p in paths)
