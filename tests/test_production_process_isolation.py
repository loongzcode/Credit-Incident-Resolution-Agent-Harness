"""Deployment isolation regressions use synthetic records, never live systems."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from credit_harness.production import settings as cfg, wiring
from credit_harness.production.workers import DeploymentTicks
from credit_harness.production.app import build_app
from credit_harness.production.tables import RetrievalMetricRow
from credit_harness.investigation.trace_store import SQLInvestigationTraceStore, InvestigationTraceRow
from credit_harness.investigation.projection import stored_trace
from credit_harness.investigation.models import TraceTrustClass
from credit_harness.retrieval.models import RetrievalTelemetry
from tests.test_production_migrations import migration_engine, upgrade
from tests.test_organizational_memory import factory

KEY = base64.b64encode(b'synthetic-process-test-key-000000').decode()
CLASSES = (cfg.InvestigationApiSettings,cfg.RegistryAdminSettings,*cfg.WORKER_SETTINGS.values())
VALUES = dict(database_url='postgresql+psycopg://test@localhost/test',tenant='demo',
    oidc_issuer='https://sso.test',oidc_jwks_url='https://sso.test/jwks',oidc_audience='investigation',
    registry_admin_audience='registry',oidc_group_roles={'operators':['SUPERVISOR']},
    registry_group_role_mapping={'operators':['REGISTRY_VIEWER']},
    frame_cursor_hmac_key=KEY,identity_alias_hmac_key=KEY,allowed_origins=['https://console.test'],
    admin_allowed_origins=['https://admin.test'],openai_api_key='synthetic-offline',
    planner_model='synthetic-planner',embedding_model='synthetic-embedding',embedding_dimension=64,
    tool_bindings_file='unused-synthetic-path')
UNRELATED = ('OPENAI_API_KEY','PLANNER_MODEL','EMBEDDING_MODEL','EMBEDDING_DIMENSION','CAPABILITY_SIGNING_SECRET',
    'TOOL_BINDINGS_FILE','FRAME_CURSOR_HMAC_KEY','FRAME_CURSOR_PREVIOUS_KEY','IDENTITY_ALIAS_HMAC_KEY',
    'OIDC_ISSUER','OIDC_JWKS_URL','OIDC_AUDIENCE','OIDC_GROUP_ROLES','REGISTRY_ADMIN_AUDIENCE',
    'REGISTRY_GROUP_ROLE_MAPPING','ALLOWED_ORIGINS','ADMIN_ALLOWED_ORIGINS')


def config(cls, **updates):
    values={k:v for k,v in VALUES.items() if k in cls.model_fields}
    values.update(updates)
    return cls.model_validate(values)


def clean_env(monkeypatch):
    for name in UNRELATED:
        monkeypatch.delenv(name,raising=False)


@pytest.mark.parametrize('cls',CLASSES,ids=lambda cls:cls.__name__)
def test_process_config_requires_only_its_own_fields(monkeypatch,cls):
    clean_env(monkeypatch)
    values={k:v for k,v in VALUES.items() if k in cls.model_fields}
    for key,value in values.items():
        monkeypatch.setenv(key.upper(),json.dumps(value) if isinstance(value,(dict,list)) else str(value))
    assert cls.from_env().tenant=='demo'
    for key,field in cls.model_fields.items():
        if field.is_required():
            with monkeypatch.context() as m:
                m.delenv(key.upper())
                with pytest.raises(cfg.StartupConfigurationError): cls.from_env()
    assert 'capability_signing_secret' not in cls.model_fields
    if cls not in (cfg.AgentWorkerSettings,cfg.EmbeddingWorkerSettings):
        assert not {'openai_api_key','planner_model','embedding_model','embedding_dimension'} & cls.model_fields.keys()
    if cls not in (cfg.AgentWorkerSettings,cfg.OrchestrationWorkerSettings):
        assert 'tool_bindings_file' not in cls.model_fields


def test_audience_isolation_is_checked_by_deployment_preflight():
    cfg.validate_audience_isolation(config(cfg.InvestigationApiSettings),config(cfg.RegistryAdminSettings))
    with pytest.raises(cfg.StartupConfigurationError):
        cfg.validate_audience_isolation(config(cfg.InvestigationApiSettings),
            config(cfg.RegistryAdminSettings,registry_admin_audience='investigation'))


def wire(monkeypatch,engine,kind,tmp_path):
    from credit_harness.production.tables import WorkerHeartbeatRow
    WorkerHeartbeatRow.__table__.create(engine,checkfirst=True)
    monkeypatch.setattr(wiring,'production_engine',lambda _:engine.execution_options(production_runtime=True))
    monkeypatch.setattr(wiring,'schema_ready',lambda _:True)
    manifest=tmp_path/'bindings.json';manifest.write_text('[]')
    cls=cfg.WORKER_SETTINGS[kind]
    setting=config(cls,**({'tool_bindings_file':str(manifest)} if 'tool_bindings_file' in cls.model_fields else {}))
    return wiring.create_components(setting,kind),setting


def forbid_ai(monkeypatch):
    import credit_harness.adapters.openai_planner as planner
    import credit_harness.adapters.openai_embedding as embedding
    import credit_harness.retrieval.service as retrieval
    import credit_harness.agent.runtime as agent
    spies=[]
    for module,name in ((planner,'OpenAIPlannerModel'),(embedding,'OpenAIEmbeddingProvider'),
            (retrieval,'HybridRetrievalService'),(agent,'InvestigationAgentRuntime')):
        spy=Mock(side_effect=AssertionError('AI construction forbidden'))
        monkeypatch.setattr(module,name,spy);spies.append(spy)
    return spies


@pytest.mark.parametrize('check',[
    'without_openai','without_embedding_config','without_planner_model',
    'does_not_construct_planner','does_not_construct_embedding_provider'])
def test_recovery_worker_starts_without_ai(migration_engine,monkeypatch,tmp_path,check):
    upgrade(migration_engine);clean_env(monkeypatch);spies=forbid_ai(monkeypatch)
    c,s=wire(monkeypatch,migration_engine,'recovery',tmp_path)
    DeploymentTicks(c,s,'synthetic-recovery').operational('recovery')
    assert all(spy.call_count==0 for spy in spies)
    assert c.orchestrator.runtime is c.orchestrator.executor is None


def test_orchestration_worker_does_not_construct_planner(migration_engine,monkeypatch,tmp_path):
    upgrade(migration_engine);clean_env(monkeypatch);spies=forbid_ai(monkeypatch)
    c,s=wire(monkeypatch,migration_engine,'orchestration',tmp_path)
    DeploymentTicks(c,s,'synthetic-verification').operational('orchestration')
    assert all(spy.call_count==0 for spy in spies)
    assert c.orchestrator.runtime is None
    from credit_harness.orchestration.models import WorkType
    assert c.orchestrator.allowed_work_types=={WorkType.VERIFICATION_REQUIRED}


def test_recovery_unknown_effect_can_be_reconciled_without_ai(factory,monkeypatch,tmp_path):
    from credit_harness.authorization.models import EffectStatus
    from credit_harness.recovery.tables import EffectRecoveryAttemptRow
    x=factory();x.read();x.remediate(timeout=True)
    assert x.effect.status==EffectStatus.UNKNOWN
    x.advance(60)
    clean_env(monkeypatch);spies=forbid_ai(monkeypatch)
    c,s=wire(monkeypatch,x.engine,'recovery',tmp_path)
    # The fixture clock is controlled; deployment uses the same clock here.
    c.work.clock=lambda:x.clock.now
    c.recovery.clock=lambda:x.clock.now
    c.recovery.lookup.clock=lambda:x.clock.now
    c.recovery.lookup.resolver.clock=lambda:x.clock.now
    lookup=Mock(wraps=c.recovery.lookup.resolver.lookup)
    monkeypatch.setattr(c.recovery.lookup.resolver,'lookup',lookup)
    DeploymentTicks(c,s,'synthetic-recovery').operational('recovery')
    assert lookup.call_count==1
    assert c.recovery.repository.authorization.get_ledger(x.effect.effect_id).status in (EffectStatus.APPLIED,EffectStatus.UNKNOWN)
    with Session(x.engine) as session:
        assert session.scalar(select(EffectRecoveryAttemptRow)) is not None
    assert all(spy.call_count==0 for spy in spies)
    assert not any(__import__('os').environ.get(k) for k in ('OPENAI_API_KEY','PLANNER_MODEL','EMBEDDING_MODEL'))


def test_api_frame_starts_without_ai_effect_or_tool_secrets(migration_engine,monkeypatch):
    from credit_harness.benchmark.fixture import EvaluationFixture
    from credit_harness.registry_admin.identity import EnterpriseIdentity
    from datetime import datetime,timezone
    upgrade(migration_engine)
    with_fixture=EvaluationFixture(migration_engine)
    try:
        with_fixture.read()
        clean_env(monkeypatch)
        identity=SimpleNamespace(authenticate=lambda _:EnterpriseIdentity(user_id='synthetic-user',
            display_name='Synthetic',groups=('operators',),authenticated_at=datetime.now(timezone.utc)))
        app=build_app(config(cfg.InvestigationApiSettings,required_workers=()),engine=migration_engine,identity=identity)
        with TestClient(app) as client:
            response=client.get('/ui/cases/'+with_fixture.case.case_id+'/frame',headers={'Authorization':'Bearer synthetic'})
        assert response.status_code==200,response.text
        assert response.json()['financial_identity']['result']=='MATCH'
    finally: with_fixture.close()


@pytest.mark.parametrize('embedding_failure',['absent','invalid','provider_failure'])
def test_agent_optional_embedding_failure_does_not_block_planner(factory,monkeypatch,tmp_path,embedding_failure):
    from credit_harness.adapters import openai_planner
    from credit_harness.planner.model import FakePlannerModel
    from credit_harness.context.assembler import ReasoningContextAssembler
    from scripts.demo_planner import scripted_draft
    from credit_harness.domain.enums import ToolName
    x=factory();x.read(ToolName.TRACE)
    clean_env(monkeypatch)
    if embedding_failure!='absent':
        monkeypatch.setenv('EMBEDDING_MODEL','synthetic')
        monkeypatch.setenv('EMBEDDING_DIMENSION','bad' if embedding_failure=='invalid' else '64')
        monkeypatch.setenv('OPENAI_API_KEY','synthetic')
    monkeypatch.setattr(wiring,'embedding_provider',Mock(side_effect=ConnectionError('synthetic outage')))
    monkeypatch.setattr(openai_planner,'OpenAIPlannerModel',lambda **_:FakePlannerModel(scripted_draft))
    c,s=wire(monkeypatch,x.engine,'agent',tmp_path)
    snapshot=ReasoningContextAssembler().build(x.cases.get(x.case.case_id),x.evidence.list(x.case.case_id))
    decision=c.orchestrator.runtime.planner.plan(snapshot)
    assert decision.guidance_build_status.value=='RETRIEVAL_FAILED'
    assert decision.selected_action is not None
    # Continue a real synthetic investigation with the same production-composed
    # Planner, using the fixture's scoped HTTP client, not a live adapter.
    from credit_harness.agent.runtime import InvestigationAgentRuntime
    from credit_harness.agent.models import AgentRunConfig
    runtime=InvestigationAgentRuntime(x.cases,x.evidence,ReasoningContextAssembler(),
        c.orchestrator.runtime.planner,x.executor,config=AgentRunConfig(max_turns=1))
    result=runtime.run(x.case.case_id)
    assert result.tool_calls_dispatched==1
    assert result.turns[0].attempts[0].decision.guidance_build_status.value=='RETRIEVAL_FAILED'


def test_legacy_evaluation_projection_preserves_original_payload():
    original={'trace_id':'EVAL-legacy','kind':'Evaluation','historical':True,'status':'PASS','fields':[]}
    before=json.dumps(original)
    projected=stored_trace(original,'1')
    assert projected.trace_trust_class==TraceTrustClass.HISTORICAL_EVALUATION
    assert json.dumps(original)==before and 'trace_trust_class' not in original


def test_migration_does_not_rewrite_legacy_payload_bytes(migration_engine):
    upgrade(migration_engine,'0016_baseline')
    raw='{ "kind" : "Evaluation", "historical": true, "trace_id": "EVAL-legacy", "status": "PASS" }'
    from credit_harness.cases.tables import CaseRow
    from credit_harness.persistence.store import SimulatorAdmin
    from credit_harness.simulator.scenarios import build_scenario
    from credit_harness.domain.enums import ScenarioId
    sid=SimulatorAdmin(migration_engine).seed(build_scenario(ScenarioId.S6))
    with migration_engine.begin() as c:
        c.execute(CaseRow.__table__.insert().values(case_id='CASE-LEGACY',simulation_id=sid,
            tenant_id='demo',status='NEW',payload={},grant_hash='synthetic',
            max_tool_calls=10,used_tool_calls=0,updated_at='2026-09-10T00:00:00+00:00'))
        # Store and compare database text, not parsed/re-serialized JSON.
        c.execute(text("INSERT INTO investigation_safe_traces(trace_id,case_id,tenant_id,payload) VALUES ('old','CASE-LEGACY','demo',:payload)"),{'payload':raw})
        before=c.scalar(text("SELECT CAST(payload AS TEXT) FROM investigation_safe_traces WHERE trace_id='old'"))
    upgrade(migration_engine)
    with migration_engine.connect() as c:
        after=c.scalar(text("SELECT CAST(payload AS TEXT) FROM investigation_safe_traces WHERE trace_id='old'"))
        version=c.scalar(text("SELECT projection_version FROM investigation_safe_traces WHERE trace_id='old'"))
    assert before==after and version=='1'
    assert stored_trace(json.loads(after),version).trace_trust_class==TraceTrustClass.HISTORICAL_EVALUATION


def test_retrieval_metrics_are_bounded_and_idempotent(factory):
    from credit_harness.production.telemetry import OperationalMetrics
    from credit_harness.production.schema import metadata
    x=factory()
    metadata().create_all(x.engine)
    trace=SQLInvestigationTraceStore(x.cases);trace.create_schema()
    for _ in range(3):
        trace.record_retrieval(case_id=x.case.case_id,snapshot_id='synthetic-snapshot',telemetry=RetrievalTelemetry(latency_ms=250))
    with Session(x.engine) as s:
        row=s.get(RetrievalMetricRow,'demo')
        assert row.latency_count==1 and row.latency_sum_seconds==.25
    statements=[]
    def capture(c,cursor,sql,*_):statements.append(sql.lower())
    event.listen(x.engine,'before_cursor_execute',capture)
    try: output=OperationalMetrics().render(x.engine,'demo')
    finally:event.remove(x.engine,'before_cursor_execute',capture)
    assert 'retrieval_latency_seconds_sum 0.25' in output
    assert not any('investigation_safe_traces' in sql for sql in statements)
    assert sum('retrieval_metrics' in sql for sql in statements)==1


def test_admin_starts_with_only_admin_configuration(migration_engine,monkeypatch):
    import scripts.serve_registry_admin as admin
    import credit_harness.production.app as application
    upgrade(migration_engine);clean_env(monkeypatch)
    for key,value in config(cfg.RegistryAdminSettings).model_dump(mode='json').items():
        if key=='database_url':value=VALUES[key]
        monkeypatch.setenv(key.upper(),json.dumps(value) if isinstance(value,(dict,list)) else str(value))
    monkeypatch.setattr(application,'production_engine',lambda _:migration_engine)
    app=admin.build()
    assert app.openapi()['paths']
    assert not {'frame_cursor_hmac_key','identity_alias_hmac_key','tool_bindings_file'} & cfg.RegistryAdminSettings.model_fields.keys()


@pytest.mark.parametrize('invalid',['none','same_audience','extra_secret'])
def test_deployment_preflight_rejects_shared_secret_files(tmp_path,invalid):
    from scripts.deploy_production import PROCESSES, validate_deployment
    paths={}
    for kind,cls in PROCESSES.items():
        values={k:v for k,v in VALUES.items() if k in cls.model_fields}
        if invalid=='same_audience' and kind=='admin':
            values['registry_admin_audience']='investigation'
        if invalid=='extra_secret' and kind=='recovery':
            values['openai_api_key']='synthetic-unnecessary'
        path=tmp_path/(kind+'.env')
        path.write_text('\n'.join(k.upper()+'='+(json.dumps(v) if isinstance(v,(list,dict)) else str(v)) for k,v in values.items()))
        paths[kind]=path
    if invalid=='none':assert validate_deployment(paths)['recovery'].tenant=='demo'
    else:
        with pytest.raises(cfg.StartupConfigurationError):validate_deployment(paths)
