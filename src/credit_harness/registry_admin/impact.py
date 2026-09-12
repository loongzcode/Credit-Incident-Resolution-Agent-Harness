"""Read-only, conservative impact aligned with existing global registry-version fencing."""
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from credit_harness.cases.tables import CaseRow
from credit_harness.registry.tables import CapabilitySnapshotRow, RouteContextRow
from credit_harness.orchestration.tables import WorkItemRow
from credit_harness.registry.repository import fingerprint
from .identity import Permission as P
from .models import ChangeRequest


def changed_capability_types(before, after):
    """Actual configuration delta, separate from the global runtime version fence."""
    old, new = ({c.capability_id: c for c in d.capabilities} for d in (before, after))
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    old_rules, new_rules = ({(r.capability_id, r.claim_type): r for r in d.authority_rules} for d in (before, after))
    changed.update(key[0] for key in old_rules.keys() | new_rules.keys() if old_rules.get(key) != new_rules.get(key))
    # Source operational changes also change its capabilities' applicability. Master
    # labels/bindings and display descriptions alone are not capability changes.
    source_fields = ("system_type", "source_channel", "scope", "status", "effective_from", "effective_until", "draining_since", "credential_ref")
    a, b = ({s.system_id: {f: getattr(s, f) for f in source_fields} for s in d.systems} for d in (before, after))
    sources = {key for key in a.keys() | b.keys() if a.get(key) != b.get(key)}
    changed.update(c.capability_id for d in (before, after) for c in d.capabilities if c.system_id in sources)
    return sorted({c.capability_type.value for d in (before, after) for c in d.capabilities if c.capability_id in changed})


def analyze(service, identity, cr_id, *, page=1, size=20):
    service.access.require(identity, P.REGISTRY_VIEW)
    with Session(service.engine) as s:
        cr = ChangeRequest.model_validate(service._row(s, cr_id).payload)
        changed = cr.base_registry_version != cr.proposed_registry_version
        # Any registry version change fences all registered active Cases, even where
        # capability eligibility is unchanged. Report this conservative scope explicitly.
        query = select(CaseRow).join(RouteContextRow, RouteContextRow.case_id == CaseRow.case_id).where(
            CaseRow.tenant_id == service.tenant_id, RouteContextRow.tenant_id == service.tenant_id,
            CaseRow.status.not_in(("CLOSED", "CLOSED_VERIFIED")))
        rows = s.scalars(query.order_by(CaseRow.case_id)).all() if changed else []
        ids = [r.case_id for r in rows]
        partners = set()
        for r in rows:
            route = service.repo.route(r.case_id, s)
            partners.add((route.asset_partner, route.funding_partner, route.product_code))
        snapshots = s.scalars(select(CapabilitySnapshotRow).where(CapabilitySnapshotRow.tenant_id == service.tenant_id,
            CapabilitySnapshotRow.case_id.in_(ids))).all() if ids else []
        invalidated = sum(r.payload.get("registry_version") == cr.base_registry_version for r in snapshots)
        work_count = s.scalar(select(func.count()).select_from(WorkItemRow).where(WorkItemRow.tenant_id == service.tenant_id,
            WorkItemRow.case_id.in_(ids), WorkItemRow.status.in_(("PENDING", "READY", "CLAIMED", "BLOCKED")))) if ids else 0
        definitions = (service._definition(s, cr.base_registry_version), service._definition(s, cr.proposed_registry_version))
        return dict(affected_active_case_count=len(ids), affected_case_ids_digest=fingerprint(ids),
            affected_partner_products=[dict(asset_partner=a, funding_partner=f, product_code=p)
                for a, f, p in sorted(partners, key=lambda x: str(x))],
            changed_capability_types=changed_capability_types(*definitions),
            runtime_revalidation_scope="ALL_ACTIVE_REGISTERED_CASES" if changed else "NONE",
            invalidated_snapshot_count=invalidated, affected_pending_work_count=work_count,
            case_page=dict(items=ids[(page-1)*size:page*size], total=len(ids), page=page, page_size=size),
            analyzed_at=service.clock().isoformat(),
            base_is_current=service._head(s) == cr.base_registry_version)
