from sqlalchemy import update, select
from sqlalchemy.orm import Session
from credit_harness.cases.preconditions import AgentPreconditionFailed
from .models import DispatchSource, RegistryError, ResolutionCode
from .tables import RegistryHeadRow, DispatchSourceRow


class RegistryDispatchGuard:
    def __init__(self, resolver, adapters):
        self.resolver, self.adapters = resolver, adapters

    def prepare(self, session, case, tool, precondition):
        repo = self.resolver.repository
        session.execute(update(RegistryHeadRow).where(RegistryHeadRow.tenant_id == case.tenant_id)
                        .values(version=RegistryHeadRow.version))
        try:
            route = repo.route(case.case_id, session)
            resolved = self.resolver.resolve_tool(case, tool, route, session=session)
            if precondition is not None and (precondition.registry_version != resolved.registry_version
                    or precondition.routing_fingerprint != resolved.routing_fingerprint):
                raise RegistryError(ResolutionCode.STALE_CAPABILITY)
            self.adapters.resolve(resolved)  # fail before spending budget if binding is absent
            return resolved
        except RegistryError as exc:
            raise AgentPreconditionFailed(exc.code.value) from None

    def record(self, session, call_id, case, resolved):
        source = DispatchSource(call_id=call_id, case_id=case.case_id, resolved=resolved)
        session.add(DispatchSourceRow(call_id=call_id, case_id=case.case_id, tenant_id=case.tenant_id,
                                      payload=source.model_dump(mode="json")))

    def client(self, case_id, call_id):
        repo = self.resolver.repository
        with Session(repo.engine) as s:
            row = s.get(DispatchSourceRow, call_id)
            if row is None or row.case_id != case_id or row.tenant_id != repo.tenant_id:
                raise RegistryError(ResolutionCode.STALE_CAPABILITY)
            source = DispatchSource.model_validate(row.payload)
        return self.adapters.resolve(source.resolved)

    def evidence_sources(self, cases, evidence_id):
        """Private inspection: Evidence -> origins -> CaseCall -> resolved registry source."""
        from credit_harness.evidence.tables import EvidenceRow, EvidenceOriginRow
        repo = self.resolver.repository
        with Session(repo.engine) as s:
            evidence = s.get(EvidenceRow, evidence_id)
            if evidence is None:
                raise RegistryError(ResolutionCode.NO_REGISTERED_SOURCE)
            cases._row(s, evidence.case_id)
            rows = s.scalars(select(DispatchSourceRow).join(EvidenceOriginRow,
                EvidenceOriginRow.call_id == DispatchSourceRow.call_id).where(
                EvidenceOriginRow.evidence_id == evidence_id, DispatchSourceRow.tenant_id == repo.tenant_id,
                DispatchSourceRow.case_id == evidence.case_id)).all()
            return tuple(DispatchSource.model_validate(r.payload) for r in rows)
