import base64
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import text, select, update
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from tests.test_production_migrations import migration_engine, upgrade
from credit_harness.production.settings import InvestigationApiSettings
from credit_harness.production.app import build_app, readiness
from credit_harness.production.tables import WorkerHeartbeatRow
from credit_harness.investigation.identity import OIDCInvestigationIdentityProvider
from credit_harness.registry_admin.identity import IdentityError
from credit_harness.registry.repository import RegistryRepository,RegistryAdmin
from credit_harness.registry.fixtures import synthetic_registry


def settings():
    key = base64.b64encode(b'synthetic-test-only-key-000000000').decode()
    return InvestigationApiSettings(database_url='postgresql+psycopg://synthetic:fake@localhost/test',tenant='demo',
        oidc_issuer='https://sso.test',oidc_audience='investigation',
        oidc_jwks_url='https://sso.test/jwks',oidc_group_roles={'operators':('SUPERVISOR',)},
        frame_cursor_hmac_key=key,identity_alias_hmac_key=key,
        allowed_origins=('https://console.test',),required_workers=())


@pytest.mark.parametrize('audience,scope,valid', [('investigation','case.investigate',True),
    ('registry','case.investigate',False),('investigation','registry.admin',False)])
def test_investigation_oidc_audience_and_scope_are_separate(audience,scope,valid):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537,key_size=2048)
    provider = OIDCInvestigationIdentityProvider(issuer='https://sso.test',audience='investigation',jwks_url='https://sso.test/jwks')
    provider.keys = SimpleNamespace(get_signing_key_from_jwt=lambda _:SimpleNamespace(key=key.public_key()))
    now = datetime.now(timezone.utc)
    token = jwt.encode(dict(iss='https://sso.test',aud=audience,sub='synthetic-user',iat=now,
        exp=now+timedelta(minutes=1),scope=scope,groups=['operators']),key,algorithm='RS256')
    if valid: assert provider.authenticate(token).user_id=='synthetic-user'
    else:
        with pytest.raises(IdentityError): provider.authenticate(token)


def test_readiness_checks_registry_integrity_and_workers(migration_engine):
    upgrade(migration_engine)
    config = settings()
    assert not readiness(migration_engine,config)
    admin = RegistryAdmin(RegistryRepository(migration_engine,'demo'))
    v = admin.register(synthetic_registry('demo'),actor='synthetic-admin')
    admin.activate(v,expected_version=None,actor='synthetic-admin')
    assert readiness(migration_engine,config)
    assert not readiness(migration_engine,config.model_copy(update={'required_workers':('agent',)}))
    from credit_harness.registry.tables import RegistryVersionRow
    with migration_engine.begin() as c: c.execute(update(RegistryVersionRow).values(payload={}))
    assert not readiness(migration_engine,config)


def test_db_disconnect_does_not_leak_secrets_or_fabricate_health(migration_engine, monkeypatch):
    upgrade(migration_engine)
    app = build_app(settings(),engine=migration_engine,identity=SimpleNamespace())
    with TestClient(app) as client:
        def fail(*args,**kwargs): raise ConnectionError('synthetic-password-never-expose')
        monkeypatch.setattr(migration_engine,'connect',fail)
        assert client.get('/health/live').status_code==200
        response = client.get('/health/ready')
        assert response.status_code==503 and 'synthetic-password' not in response.text


def test_metrics_are_aggregate_only(migration_engine):
    upgrade(migration_engine)
    from credit_harness.production.telemetry import OperationalMetrics
    output = OperationalMetrics().render(migration_engine,'demo')
    for field in ('agent_runs_total','tool_calls_total','frame_stale_total','retrieval_latency_seconds_sum','index_job_failed'):
        assert field in output
    assert not any(field in output for field in ('case_id','customer','transaction','password','demo','{'))


@pytest.mark.postgres
@pytest.mark.parametrize('kind',['agent','orchestration','recovery','embedding'])
def test_production_worker_wiring_starts_without_ddl_or_network(migration_engine,monkeypatch,tmp_path,kind):
    if migration_engine.dialect.name!='postgresql': pytest.skip('production requires PostgreSQL')
    upgrade(migration_engine)
    from credit_harness.production import wiring
    from credit_harness.production.workers import DeploymentTicks
    from sqlalchemy import event
    engine = migration_engine.execution_options(production_runtime=True)
    monkeypatch.setattr(wiring,'production_engine',lambda _:engine)
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-offline-no-network')
    monkeypatch.setenv('PLANNER_MODEL','synthetic-planner')
    manifest = tmp_path/'bindings.json'; manifest.write_text('[]')
    monkeypatch.setenv('TOOL_BINDINGS_FILE',str(manifest))
    sql = []
    def capture(c,cursor,statement,*args): sql.append(statement)
    event.listen(engine,'before_cursor_execute',capture)
    try:
        from credit_harness.production.settings import WORKER_SETTINGS
        values = settings().model_dump()
        values.update(openai_api_key='synthetic-offline-no-network', planner_model='synthetic-planner',
            embedding_model='synthetic', embedding_dimension=64, tool_bindings_file=str(manifest))
        config = WORKER_SETTINGS[kind].model_validate({k:v for k,v in values.items() if k in WORKER_SETTINGS[kind].model_fields})
        components = wiring.create_components(config,kind)
        ticks = DeploymentTicks(components,config,'synthetic-worker')
        ticks.heartbeat(kind,'IDLE')
        ticks.embedding() if kind=='embedding' else ticks.operational(kind)
    finally: event.remove(engine,'before_cursor_execute',capture)
    assert not any(s.lstrip().upper().startswith(('CREATE','ALTER','DROP')) for s in sql)
