"""Immutable publications and a CAS active pointer, only for trusted deployment code."""
import hashlib
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from .models import RegistryDefinition, CaseRouteContext, RegistryError, ResolutionCode, RegistryEvent, RegistryStatus
from .tables import (RegistryVersionRow, RegistryHeadRow, SystemRow, CapabilityRow, AuthorityRow,
                     RegistryAuditRow, RouteContextRow, create_registry_schema)


def fingerprint(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class RegistryRepository:
    def __init__(self, engine, tenant_id):
        self.engine, self.tenant_id = engine, tenant_id

    def current(self, session=None):
        if session is None:
            with Session(self.engine) as s:
                return self.current(s)
        head = session.get(RegistryHeadRow, self.tenant_id)
        if head is None or head.version is None:
            raise RegistryError(ResolutionCode.NO_REGISTERED_SOURCE)
        row = session.get(RegistryVersionRow, head.version)
        if row is None or row.tenant_id != self.tenant_id or fingerprint(row.payload) != row.version:
            raise RegistryError(ResolutionCode.STALE_CAPABILITY)
        from .compatibility import read_definition
        return row.version, read_definition(row.payload)

    def route(self, case_id, session=None):
        if session is None:
            with Session(self.engine) as s:
                return self.route(case_id, s)
        from .tables import RouteHeadRow, RouteRevisionRow
        if session.get(RouteHeadRow, case_id) is not None:
            return self.route_revision(case_id, session).route_context
        if session.scalar(select(RouteRevisionRow.route_revision_id).where(RouteRevisionRow.case_id == case_id)):
            raise RegistryError(ResolutionCode.INVALID_ROUTE_CONTEXT)
        row = session.get(RouteContextRow, case_id)
        if row is None or row.tenant_id != self.tenant_id or fingerprint(row.payload) != row.fingerprint:
            raise RegistryError(ResolutionCode.INVALID_ROUTE_CONTEXT)
        return CaseRouteContext.model_validate(row.payload)

    def route_revision(self, case_id, session=None):
        if session is None:
            with Session(self.engine) as s:
                return self.route_revision(case_id, s)
        from .revisions import read_revision
        from .tables import RouteHeadRow
        head = session.get(RouteHeadRow, case_id)
        if head is None or head.tenant_id != self.tenant_id:
            raise RegistryError(ResolutionCode.INVALID_ROUTE_CONTEXT)
        return read_revision(session, head.route_revision_id, case_id, self.tenant_id)

    def route_history(self, case_id):
        with Session(self.engine) as s:
            from .revisions import revision_chain
            return revision_chain(s, self.route_revision(case_id, s))


class RegistryAdmin:
    """Never mounted as a Harness/Agent API. No secrets are accepted."""
    def __init__(self, repository, *, clock=lambda: datetime.now(timezone.utc)):
        self.repository, self.clock = repository, clock
        create_registry_schema(repository.engine)
        # Bootstrap only: normal activation uses the pre-existing singleton row.
        with Session(repository.engine) as s, s.begin():
            if s.get(RegistryHeadRow, repository.tenant_id) is None:
                s.add(RegistryHeadRow(tenant_id=repository.tenant_id, version=None))

    def _audit(self, s, version, event, actor):
        s.add(RegistryAuditRow(tenant_id=self.repository.tenant_id, version=version,
            event=event.value, actor=actor, occurred_at=self.clock().isoformat()))

    @contextmanager
    def _transaction(self, session=None):
        if session is not None:
            if not session.in_transaction():
                raise ValueError("caller transaction required")
            yield session
        else:
            with Session(self.repository.engine) as s, s.begin():
                yield s

    def register(self, definition, *, actor, session=None):
        definition = RegistryDefinition.model_validate(definition.model_dump())
        if definition.tenant_id != self.repository.tenant_id or not actor:
            raise ValueError("invalid registry tenant/actor")
        from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
        from .models import AccessMode
        contracts = {c.tool_name: c for c in ToolCapabilityCatalog.entries}
        for c in definition.capabilities:
            actual = contracts.get(c.tool_name)
            if c.read_or_write == AccessMode.READ and (actual is None
                    or not set(c.produces_claim_types) <= set(actual.produces_claim_types)
                    or not set(c.contributes_requirements) <= set(actual.contributes_requirements)):
                raise ValueError("registered claims exceed executable Tool contract")
        payload = definition.model_dump(mode="json")
        for key, sortkey in (("systems", "system_id"), ("capabilities", "capability_id")):
            payload[key].sort(key=lambda d: d[sortkey])
        payload["authority_rules"].sort(key=lambda d: (d["capability_id"], d["claim_type"]))
        version = fingerprint(payload)
        with self._transaction(session) as s:
            # Serialize publication with activation and dispatch admission.
            s.execute(update(RegistryHeadRow).where(RegistryHeadRow.tenant_id == definition.tenant_id)
                      .values(version=RegistryHeadRow.version))
            if s.get(RegistryVersionRow, version) is not None:
                return version
            s.add(RegistryVersionRow(version=version, tenant_id=definition.tenant_id, payload=payload)); s.flush()
            for item in payload["systems"]:
                s.add(SystemRow(version=version, system_id=item["system_id"], payload=item))
            for item in payload["capabilities"]:
                s.add(CapabilityRow(version=version, capability_id=item["capability_id"], payload=item))
            for item in payload["authority_rules"]:
                s.add(AuthorityRow(version=version, capability_id=item["capability_id"], claim_type=item["claim_type"], payload=item))
            self._audit(s, version, RegistryEvent.REGISTERED, actor)
        return version

    def activate(self, version, *, expected_version, actor, session=None):
        if not actor:
            raise ValueError("actor required")
        with self._transaction(session) as s:
            row = s.get(RegistryVersionRow, version)
            if row is None or row.tenant_id != self.repository.tenant_id:
                raise RegistryError(ResolutionCode.STALE_CAPABILITY)
            result = s.execute(update(RegistryHeadRow).where(
                RegistryHeadRow.tenant_id == self.repository.tenant_id,
                RegistryHeadRow.version == expected_version).values(version=version))
            if result.rowcount != 1:
                raise RegistryError(ResolutionCode.STALE_CAPABILITY)
            self._audit(s, version, RegistryEvent.ACTIVATED, actor)
            for status, event in ((RegistryStatus.DRAINING, RegistryEvent.DRAINING),
                                  (RegistryStatus.DISABLED, RegistryEvent.DISABLED)):
                if any(d["status"] == status for d in (*row.payload["systems"], *row.payload["capabilities"])):
                    self._audit(s, version, event, actor)
            if expected_version and expected_version != version:
                self._audit(s, expected_version, RegistryEvent.RETIRED, actor)

    def bind_case_route(self, cases, route, *, actor):
        route = CaseRouteContext.model_validate(route.model_dump())
        case = cases.get(route.case_id)
        if (cases.engine is not self.repository.engine or case.tenant_id != self.repository.tenant_id
                or route.tenant_id != case.tenant_id or not actor):
            raise RegistryError(ResolutionCode.INVALID_ROUTE_CONTEXT)
        with Session(self.repository.engine) as s, s.begin():
            # Case -> Registry lock order matches tool reservation.
            from credit_harness.cases.tables import CaseRow
            s.execute(update(CaseRow).where(CaseRow.case_id == case.case_id).values(updated_at=CaseRow.updated_at))
            existing = s.get(RouteContextRow, case.case_id)
            if existing:
                if existing.fingerprint != fingerprint(route):
                    raise ValueError("initial binding cannot be overwritten; use evidence-verified RouteUpdater")
                from .revisions import ensure_initial_revision
                ensure_initial_revision(s, case, existing, self.clock())
                return
            initial = RouteContextRow(case_id=case.case_id, tenant_id=case.tenant_id, fingerprint=fingerprint(route),
                                      payload=route.model_dump(mode="json"), actor=actor)
            s.add(initial)
            from .revisions import ensure_initial_revision
            ensure_initial_revision(s, case, initial, self.clock(), imported=False)
