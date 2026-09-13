from time import perf_counter, time
from uuid import uuid4
import logging
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import create_engine, text, select
from sqlalchemy.orm import Session
from credit_harness.api.ui import create_production_ui_app
from credit_harness.cases.repository import CaseRepository
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.investigation.cache import SQLFrameCache
from credit_harness.investigation.identity import OIDCInvestigationIdentityProvider, InvestigationAccess
from credit_harness.registry.tables import RegistryHeadRow, RegistryVersionRow
from .settings import ProductionSettings, StartupConfigurationError
from .schema import HEAD
from .tables import WorkerHeartbeatRow
from .telemetry import configure_logging, OperationalMetrics


def production_engine(settings):
    return create_engine(settings.database_url.get_secret_value(), hide_parameters=True,
        pool_pre_ping=True, pool_size=settings.db_pool_size, max_overflow=0, pool_timeout=5,
        pool_recycle=settings.db_pool_recycle, connect_args={"connect_timeout": 5,
            "options": f"-c statement_timeout={settings.statement_timeout_ms} -c lock_timeout=5000 -c idle_in_transaction_session_timeout=10000"},
        execution_options={"production_runtime": True})


def schema_ready(engine):
    with engine.connect() as connection:
        return connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD


def readiness(engine, settings):
    try:
        if not schema_ready(engine):
            return False
        with Session(engine) as session:
            from credit_harness.registry.repository import RegistryRepository
            RegistryRepository(engine, settings.tenant).current(session)
            workers = set(session.scalars(select(WorkerHeartbeatRow.kind).where(
                WorkerHeartbeatRow.tenant_id == settings.tenant,
                WorkerHeartbeatRow.updated_at > time()-settings.worker_lease_seconds*2,
                WorkerHeartbeatRow.status.in_(["IDLE", "WORKING"])) ))
            return set(settings.required_workers) <= workers
    except Exception:
        return False


def build_app(settings, *, engine=None, identity=None):
    engine = engine or production_engine(settings)
    try:
        if not schema_ready(engine):
            raise ValueError()
    except Exception:
        raise StartupConfigurationError("DATABASE_SCHEMA_NOT_READY") from None
    evidence = EvidenceRepository(CaseRepository(engine, settings.tenant))
    app = create_production_ui_app({}, identity_repository=evidence,
        identity_provider=identity or OIDCInvestigationIdentityProvider(issuer=settings.oidc_issuer,
            audience=settings.oidc_audience, jwks_url=settings.oidc_jwks_url),
        identity_access=InvestigationAccess(settings.oidc_group_roles), frame_options=dict(
            cache=SQLFrameCache(engine), ttl=settings.frame_ttl_seconds,
            cursor_key=settings.key_bytes("frame_cursor_hmac_key"),
            previous_cursor_key=settings.key_bytes("frame_cursor_previous_key"),
            alias_key=settings.key_bytes("identity_alias_hmac_key")))
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def lifespan(app):
        yield
        engine.dispose()
    app.router.lifespan_context = lifespan
    metrics = OperationalMetrics()
    app.state.metrics = metrics
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.allowed_origins),
        allow_methods=["GET"], allow_headers=["Authorization"], allow_credentials=False)

    @app.middleware("http")
    async def operational_boundary(request, call_next):
        start, request_id = perf_counter(), uuid4().hex
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse(status_code=503, content={"detail": {"code": "SERVICE_UNAVAILABLE"}})
        duration = perf_counter()-start
        if request.url.path.endswith("/frame"):
            metrics.observe("frame_build_latency_seconds_sum", duration)
            metrics.observe("frame_build_latency_seconds_count")
        if response.status_code == 409:
            metrics.observe("frame_stale_total")
        logging.getLogger("harness").info("", extra=dict(event="request_completed", component="api",
            request_id=request_id, duration=duration))
        response.headers.update({"X-Request-ID": request_id, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'"})
        return response

    @app.get("/health/live", include_in_schema=False)
    def live():
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        healthy = readiness(engine, settings)
        return JSONResponse(status_code=200 if healthy else 503,
            content={"status": "ready" if healthy else "not_ready", "guidance": "optional"})

    @app.get("/metrics", include_in_schema=False)
    def aggregate_metrics():
        return PlainTextResponse(metrics.render(engine, settings.tenant), media_type="text/plain; version=0.0.4")
    return app


def create_app():
    configure_logging()
    return build_app(ProductionSettings.from_env())
