"""Separately composed human Admin API. No simulator / tool / agent routes."""
from uuid import uuid4
from fastapi import FastAPI, Depends, Request, UploadFile, File, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer
from pydantic import ValidationError
from .identity import IdentityError, LocalIdentityProvider
from .models import (AdminError, CreateDraft, EditDraft, Transition, RollbackRequest, ImportConfirm, AgentConfigurationStatus)
from .impact import analyze
from credit_harness.registry.models import RegistryError


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PATCH"):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            body.extend(event.get("body", b""))
            if len(body) > 6_000_000:
                return await JSONResponse({"detail": "REQUEST_TOO_LARGE"}, 413)(scope, receive, send)
            if not event.get("more_body", False):
                break
        sent = False
        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


def create_admin_app(service, identity_provider, *, allowed_origins=(), local_mode=False):
    if isinstance(identity_provider, LocalIdentityProvider) and not local_mode:
        raise ValueError("Local identity requires explicit local_mode; production uses OIDC")
    app = FastAPI(title="Registry Administration", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimit)
    bearer = HTTPBearer(auto_error=False)

    async def actor(request: Request, credentials=Depends(bearer)):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AdminError("UNAUTHENTICATED", 401)
        if request.method in ("POST", "PATCH"):
            # No ambient cookie auth. Custom header and same-origin allowlist also
            # reject form posts and cross-site CSRF attempts; CORS is not enabled.
            if request.headers.get("x-registry-request") != "1":
                raise AdminError("CSRF_REJECTED", 403)
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                raise AdminError("CSRF_REJECTED", 403)
        request.state.request_id = uuid4().hex
        return identity_provider.authenticate(credentials.credentials)

    @app.exception_handler(AdminError)
    async def admin_error(request, exc):
        return JSONResponse({"detail": exc.code}, status_code=exc.status)

    @app.exception_handler(IdentityError)
    async def identity_error(request, exc):
        return JSONResponse({"detail": "UNAUTHENTICATED"}, status_code=401)

    @app.exception_handler(RegistryError)
    async def registry_error(request, exc):
        return JSONResponse({"detail": exc.code.value}, status_code=409)

    async def validation_error(request, exc):
        # FastAPI's default error includes rejected input (which could be a secret).
        return JSONResponse({"detail": "INVALID_ADMIN_REQUEST"}, status_code=422)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(ValidationError, validation_error)
    app.add_exception_handler(ValueError, validation_error)

    @app.middleware("http")
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    prefix = "/admin/registry"

    @app.get(prefix)
    def dashboard(user=Depends(actor)):
        return service.read(user, "dashboard")

    @app.get(prefix + "/catalog")
    def catalog(user=Depends(actor)):
        from .identity import Permission
        from credit_harness.registry.models import SystemType, SourceChannel, CapabilityType, PartnerRole, AuthorityLevel
        from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
        service.access.require(user, Permission.REGISTRY_VIEW)
        return dict(system_types=list(SystemType), source_channels=list(SourceChannel), capability_types=list(CapabilityType),
            partner_roles=list(PartnerRole), authority_levels=list(AuthorityLevel),
            tools=[dict(tool_name=t.tool_name, claims=t.produces_claim_types, requirements=t.contributes_requirements)
                   for t in ToolCapabilityCatalog.entries])

    @app.get(prefix + "/systems")
    def systems(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                system_code: str | None = Query(None, max_length=100), system_name: str | None = Query(None, max_length=300),
                system_type_label: str | None = Query(None, max_length=300), inventory_group: str | None = Query(None, max_length=300),
                configuration_status: AgentConfigurationStatus | None = None, has_capability: bool | None = None, user=Depends(actor)):
        return service.company_systems(user, page=page, size=size, system_code=system_code, system_name=system_name,
            system_type_label=system_type_label, inventory_group=inventory_group,
            configuration_status=configuration_status, has_capability=has_capability)

    @app.get(prefix + "/systems/{system_id}")
    def system(system_id: str, user=Depends(actor)):
        return service.company_systems(user, code=system_id)

    @app.get(prefix + "/versions")
    def versions(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(actor)):
        return service.read(user, "versions", page=page, size=size)

    @app.get(prefix + "/versions/{version}")
    def version(version: str, user=Depends(actor)):
        return service.read(user, "definition", version=version)

    @app.get(prefix + "/versions/{before}/diff/{after}")
    def diff(before: str, after: str, user=Depends(actor)):
        return service.diff(user, before, after)

    @app.get(prefix + "/change-requests")
    def changes(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), status: str | None = None, user=Depends(actor)):
        return service.read(user, "changes", page=page, size=size, status=status)

    @app.post(prefix + "/change-requests")
    def create(body: CreateDraft, request: Request, user=Depends(actor)):
        return service.create(user, body, request.state.request_id)

    @app.post(prefix + "/rollback")
    def rollback(body: RollbackRequest, request: Request, user=Depends(actor)):
        return service.create(user, body, request.state.request_id, rollback=True)

    @app.get(prefix + "/change-requests/{cr_id}")
    def change(cr_id: str, user=Depends(actor)):
        return service.get(user, cr_id)

    @app.patch(prefix + "/change-requests/{cr_id}")
    def edit(cr_id: str, body: EditDraft, request: Request, user=Depends(actor)):
        return service.edit(user, cr_id, body, request.state.request_id)

    @app.get(prefix + "/change-requests/{cr_id}/impact")
    def impact(cr_id: str, page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(actor)):
        return analyze(service, user, cr_id, page=page, size=size)

    @app.post(prefix + "/change-requests/{cr_id}/{action}")
    def transition(cr_id: str, action: str, body: Transition, request: Request, user=Depends(actor)):
        return service.transition(user, cr_id, action, body.expected_revision, request.state.request_id,
                                  decision_comment=body.decision_comment)

    @app.get(prefix + "/inventory")
    def inventory(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(actor)):
        return service.read(user, "inventory", page=page, size=size)

    @app.post(prefix + "/import/preview")
    async def preview(request: Request, file: UploadFile = File(...), user=Depends(actor)):
        content = await file.read(5_000_001)
        return service.preview(user, content, file.filename, request.state.request_id)

    @app.post(prefix + "/import/confirm")
    def confirm(body: ImportConfirm, request: Request, user=Depends(actor)):
        return service.confirm_import(user, body, request.state.request_id)

    @app.get(prefix + "/audit")
    def audit(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user=Depends(actor)):
        return service.read(user, "audit", page=page, size=size)

    return app
