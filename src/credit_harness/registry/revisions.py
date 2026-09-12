"""Append-only route bodies; only a head pointer advances under the Case lock."""
from sqlalchemy import update, select
from sqlalchemy.orm import Session
from credit_harness.cases.repository import hydrate, utc_now
from .models import (CaseRouteContext, CaseRouteRevision, RouteChangeReason as Reason,
    RouteRevisionStatus as Status, RegistryError, ResolutionCode as Code)
from .repository import fingerprint
from .tables import RouteRevisionRow, RouteHeadRow, RouteContextRow, RegistryHeadRow


def read_revision(session, revision_id, case_id, tenant_id):
    row = session.get(RouteRevisionRow, revision_id)
    if row is None or row.case_id != case_id or row.tenant_id != tenant_id:
        raise RegistryError(Code.INVALID_ROUTE_CONTEXT)
    revision = CaseRouteRevision.model_validate(row.payload)
    if (revision.route_revision_id != revision_id or revision.case_id != case_id or revision.tenant_id != tenant_id
            or revision.parent_revision_id != row.parent_revision_id
            or fingerprint(revision.model_dump(mode="json", exclude={"route_revision_id"})) != revision_id
            or fingerprint(revision.route_context) != revision.routing_fingerprint):
        raise RegistryError(Code.INVALID_ROUTE_CONTEXT)
    return revision


def revision_chain(session, head):
    result, seen = [], set()
    while head is not None:
        if head.route_revision_id in seen:
            raise RegistryError(Code.INVALID_ROUTE_CONTEXT)
        seen.add(head.route_revision_id)
        result.append(head)
        head = (read_revision(session, head.parent_revision_id, head.case_id, head.tenant_id)
                if head.parent_revision_id else None)
    return tuple(reversed(result))


def append_revision(session, route, *, parent, reason, refs, now, actor):
    draft = CaseRouteRevision(route_revision_id="0" * 64, case_id=route.case_id, tenant_id=route.tenant_id,
        parent_revision_id=parent, route_context=route, routing_fingerprint=fingerprint(route),
        change_reason=reason, supporting_evidence_refs=tuple(sorted(set(refs))), created_at=now,
        created_by=actor, status=Status.VERIFIED if parent else Status.TRUSTED_INITIAL)
    revision = draft.model_copy(update=dict(route_revision_id=fingerprint(draft.model_dump(
        mode="json", exclude={"route_revision_id"}))))
    session.add(RouteRevisionRow(route_revision_id=revision.route_revision_id, case_id=route.case_id,
        tenant_id=route.tenant_id, parent_revision_id=parent, payload=revision.model_dump(mode="json")))
    session.flush()
    return revision


def ensure_initial_revision(session, case, legacy, now, *, imported=True):
    head = session.get(RouteHeadRow, case.case_id)
    if head is not None:
        return read_revision(session, head.route_revision_id, case.case_id, case.tenant_id)
    if session.scalar(select(RouteRevisionRow.route_revision_id).where(RouteRevisionRow.case_id == case.case_id)):
        raise RegistryError(Code.INVALID_ROUTE_CONTEXT)  # missing head is corruption, never a reset to UNKNOWN
    if (legacy is None or legacy.case_id != case.case_id or legacy.tenant_id != case.tenant_id
            or fingerprint(legacy.payload) != legacy.fingerprint):
        raise RegistryError(Code.INVALID_ROUTE_CONTEXT)
    route = CaseRouteContext.model_validate(legacy.payload)
    if route.case_id != case.case_id or route.tenant_id != case.tenant_id:
        raise RegistryError(Code.INVALID_ROUTE_CONTEXT)
    revision = append_revision(session, route, parent=None,
        reason=Reason.LEGACY_IMPORT if imported else Reason.INITIAL_BINDING, refs=(), now=now, actor=legacy.actor)
    session.add(RouteHeadRow(case_id=case.case_id, tenant_id=case.tenant_id, route_revision_id=revision.route_revision_id))
    session.flush()
    return revision


class RouteUpdater:
    """Trusted deterministic service. Not a Planner action, Tool, Skill or HTTP endpoint."""
    def __init__(self, cases, resolver, *, actor="ROUTE-UPDATER", clock=utc_now):
        repo = resolver.repository
        if cases.engine is not repo.engine or cases.tenant_id != repo.tenant_id:
            raise ValueError("routing must share the Case repository boundary")
        self.cases, self.resolver, self.actor, self.clock = cases, resolver, actor, clock

    def initialize_legacy(self, case_id):
        """Trusted, idempotent import. The Step 14 row remains byte-for-byte unchanged."""
        with Session(self.cases.engine) as s, s.begin():
            case = self._lock_case(s, case_id)
            return ensure_initial_revision(s, case, s.get(RouteContextRow, case_id), self.clock())

    def _lock_case(self, session, case_id):
        from credit_harness.cases.tables import CaseRow
        session.execute(update(CaseRow).where(CaseRow.case_id == case_id,
            CaseRow.tenant_id == self.cases.tenant_id).values(updated_at=CaseRow.updated_at))
        case = hydrate(self.cases._row(session, case_id))
        if case.status.is_terminal:
            raise RegistryError(Code.INVALID_ROUTE_CONTEXT)
        return case

    def promote_protocol(self, case_id, *, expected_revision_id, evidence_refs):
        # Accept references only, never a caller-supplied version or arbitrary context.
        import re
        if (type(evidence_refs) is not tuple or not 1 <= len(evidence_refs) <= 32
                or any(type(r) is not str or re.fullmatch(r"E-[0-9a-f]{64}", r) is None for r in evidence_refs)
                or type(expected_revision_id) is not str):
            raise RegistryError(Code.INVALID_ROUTING_PROOF)
        with Session(self.cases.engine) as s, s.begin():
            case = self._lock_case(s, case_id)
            current = self.resolver.repository.route_revision(case_id, s)
            if current.route_revision_id != expected_revision_id:
                raise RegistryError(Code.STALE_ROUTE_REVISION)
            # Case -> Registry ordering also protects source validity against activation.
            s.execute(update(RegistryHeadRow).where(RegistryHeadRow.tenant_id == case.tenant_id)
                      .values(version=RegistryHeadRow.version))
            from .routing_proof import verified_protocol
            version = verified_protocol(s, case, current, evidence_refs, self.resolver, self.clock())
            known = current.route_context.protocol_version
            if known is not None:
                if version != known:
                    raise RegistryError(Code.ROUTING_FACT_CONFLICT)
                return current  # same verified fact is an idempotent no-op
            route = current.route_context.model_copy(update=dict(protocol_version=version))
            revision = append_revision(s, route, parent=current.route_revision_id, reason=Reason.PROTOCOL_VERIFIED,
                refs=evidence_refs, now=self.clock(), actor=self.actor)
            result = s.execute(update(RouteHeadRow).where(RouteHeadRow.case_id == case_id,
                RouteHeadRow.tenant_id == case.tenant_id, RouteHeadRow.route_revision_id == expected_revision_id)
                .values(route_revision_id=revision.route_revision_id))
            if result.rowcount != 1:
                raise RegistryError(Code.STALE_ROUTE_REVISION)
            return revision
