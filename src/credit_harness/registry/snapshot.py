from sqlalchemy import update
from sqlalchemy.orm import Session
from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
from .models import (AvailableCapability, VisibleAuthority, CaseCapabilitySnapshot, RegistryError,
                     ResolutionCode)
from .repository import fingerprint
from .tables import CapabilitySnapshotRow, RegistryHeadRow


class RegistryBackedCatalog:
    """Trusted composition injection; configured registry failures never fall back to static tools."""
    def __init__(self, resolver):
        self.resolver = resolver

    def project(self, case):
        repo = self.resolver.repository
        with Session(repo.engine) as s, s.begin():
            # A single version is used for all entries and durable snapshot publication.
            s.execute(update(RegistryHeadRow).where(RegistryHeadRow.tenant_id == repo.tenant_id)
                      .values(version=RegistryHeadRow.version))
            route = repo.route(case.case_id, s)
            version, definition, _ = self.resolver.candidates(case, route, session=s)
            caps = {c.capability_id: c for c in definition.capabilities}
            tools, visible = [], []
            for legacy in ToolCapabilityCatalog().for_case(case):
                try:
                    r = self.resolver.resolve_tool(case, legacy.tool_name, route, session=s)
                except RegistryError:
                    continue
                c = caps[r.capability_id]
                # Registration cannot invent executable Tool DTO claims/requirements.
                claims = tuple(v for v in r.claim_types if v in legacy.produces_claim_types)
                requirements = tuple(v for v in c.contributes_requirements if v in legacy.contributes_requirements)
                if not claims:
                    continue
                tools.append(legacy.model_copy(update=dict(produces_claim_types=claims,
                    contributes_requirements=requirements)))
                visible.append(AvailableCapability(capability_type=c.capability_type, tool_name=c.tool_name,
                    claim_types=claims, authority=tuple(VisibleAuthority(**rule.model_dump(exclude={"capability_id"}))
                        for rule in r.authority if rule.claim_type in claims),
                    cost_class=legacy.estimated_cost_class.value, latency_class=legacy.estimated_latency_class.value))
            payload = dict(case_id=case.case_id, registry_version=version, routing_context_fingerprint=fingerprint(route),
                           available_capabilities=[v.model_dump(mode="json") for v in visible],
                           assembled_at=route.effective_at.isoformat())
            draft = CaseCapabilitySnapshot(snapshot_id="0" * 64, **payload)
            snapshot = draft.model_copy(update=dict(snapshot_id=fingerprint(draft.model_dump(mode="json", exclude={"snapshot_id"}))))
            if s.get(CapabilitySnapshotRow, snapshot.snapshot_id) is None:
                s.add(CapabilitySnapshotRow(snapshot_id=snapshot.snapshot_id, case_id=case.case_id,
                    tenant_id=case.tenant_id, payload=snapshot.model_dump(mode="json")))
            return tuple(tools), snapshot

    def for_case(self, case):
        return self.project(case)[0]

    def revalidate_snapshot(self, case, snapshot):
        current = self.project(case)[1]
        if current != snapshot:
            raise RegistryError(ResolutionCode.STALE_CAPABILITY)
        return current
