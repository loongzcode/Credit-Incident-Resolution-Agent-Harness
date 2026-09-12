"""Human CR lifecycle; registry publication, activation and audit share one transaction."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session
from credit_harness.registry.repository import RegistryRepository, RegistryAdmin, fingerprint
from credit_harness.registry.compatibility import read_definition
from credit_harness.registry.models import RegistryDefinition, RegistryError
from credit_harness.registry.tables import RegistryHeadRow, RegistryVersionRow, RegistryAuditRow
from .identity import Permission as P
from .models import AdminError, ChangeRequest, ChangeStatus as S, ChangeKind as K
from .tables import (ChangeRow, AdminAuditRow, InventoryHeadRow, InventoryVersionRow, ImportPreviewRow, create_admin_schema)
from .diff import version_diff
from .inventory import read_records, merge_records, content_identity, join_record


def masked(definition):
    data = definition.model_dump(mode="json")
    for system in data["systems"]:
        system["credential_ref"] = "[MASKED]" if system.get("credential_ref") else None
    return data


class RegistryAdministration:
    def __init__(self, engine, tenant_id, access, *, clock=lambda: datetime.now(timezone.utc)):
        self.engine, self.tenant_id, self.access, self.clock = engine, tenant_id, access, clock
        self.repo = RegistryRepository(engine, tenant_id)
        self.admin = RegistryAdmin(self.repo, clock=clock)
        create_admin_schema(engine)
        with Session(engine) as s, s.begin():
            self._lock(s)
            if s.get(InventoryHeadRow, tenant_id) is None:
                s.add(InventoryHeadRow(tenant_id=tenant_id, revision=0, payload=[]))

    def _lock(self, s):
        s.execute(update(RegistryHeadRow).where(RegistryHeadRow.tenant_id == self.tenant_id)
                  .values(version=RegistryHeadRow.version))

    def _head(self, s):
        return s.get(RegistryHeadRow, self.tenant_id).version

    def _definition(self, s, version):
        if version is None:
            return RegistryDefinition(tenant_id=self.tenant_id, systems=(), capabilities=(), authority_rules=())
        row = s.get(RegistryVersionRow, version)
        if row is None or row.tenant_id != self.tenant_id or fingerprint(row.payload) != version:
            raise AdminError("VERSION_NOT_FOUND", 404)
        return read_definition(row.payload)

    def _row(self, s, cr_id):
        row = s.get(ChangeRow, cr_id)
        if row is None or row.tenant_id != self.tenant_id:
            raise AdminError("CHANGE_REQUEST_NOT_FOUND", 404)
        return row

    def _audit(self, s, event, identity, request_id, cr=None, before=None, after=None, **metadata):
        s.add(AdminAuditRow(tenant_id=self.tenant_id, payload=dict(event=event, actor=identity.user_id,
            time=self.clock().isoformat(), request_id=request_id, before_version=before,
            after_version=after, change_request_id=cr, **metadata)))

    def _fresh(self, s, cr):
        if self._head(s) != cr.base_registry_version:
            raise AdminError("STALE_CHANGE_REQUEST")
        if cr.kind == K.INVENTORY and s.get(InventoryHeadRow, self.tenant_id).revision != cr.base_inventory_revision:
            raise AdminError("STALE_CHANGE_REQUEST")

    def _save(self, s, row, cr, changes):
        value = ChangeRequest.model_validate({**cr.model_dump(), **changes, "revision": cr.revision + 1})
        result = s.execute(update(ChangeRow).where(ChangeRow.change_request_id == cr.change_request_id,
            ChangeRow.tenant_id == self.tenant_id, ChangeRow.revision == cr.revision, ChangeRow.status == cr.status.value)
            .values(revision=value.revision, status=value.status.value, payload=value.model_dump(mode="json")))
        if result.rowcount != 1:
            raise AdminError("CHANGE_REQUEST_CONFLICT")
        return value

    def _new(self, s, identity, body, request_id, *, kind=K.CONFIGURATION, target=None, inventory=()):
        if body.base_registry_version != self._head(s):
            raise AdminError("STALE_CHANGE_REQUEST")
        if kind == K.ROLLBACK and not s.scalar(select(RegistryAuditRow.id).where(
                RegistryAuditRow.tenant_id == self.tenant_id, RegistryAuditRow.version == target,
                RegistryAuditRow.event == "ACTIVATED").limit(1)):
            raise AdminError("ROLLBACK_TARGET_NEVER_ACTIVATED", 422)
        definition = self._definition(s, target if kind == K.ROLLBACK else body.base_registry_version)
        # Inventory must not reserialize or republish a historical Registry body.
        if kind == K.INVENTORY and body.base_registry_version is None:
            raise AdminError("REGISTRY_NOT_INITIALIZED", 422)
        version = (target if kind == K.ROLLBACK else body.base_registry_version)
        if version is None:
            version = self.admin.register(definition, actor=identity.user_id, session=s)
        cr = ChangeRequest(change_request_id="CR-" + uuid4().hex, tenant_id=self.tenant_id,
            base_registry_version=body.base_registry_version, proposed_registry_version=version,
            base_inventory_revision=s.get(InventoryHeadRow, self.tenant_id).revision, kind=kind, revision=1,
            status=S.DRAFT, title=body.title, reason=body.reason, created_by=identity.user_id, created_at=self.clock())
        s.add(ChangeRow(change_request_id=cr.change_request_id, tenant_id=self.tenant_id, revision=1,
            status=S.DRAFT.value, payload=cr.model_dump(mode="json"), inventory=list(inventory)))
        self._audit(s, "ROLLBACK_REQUESTED" if kind == K.ROLLBACK else "CREATE_DRAFT", identity, request_id,
                    cr.change_request_id, cr.base_registry_version, version)
        return cr

    def create(self, identity, body, request_id, *, rollback=False):
        self.access.require(identity, P.REGISTRY_ROLLBACK if rollback else P.REGISTRY_EDIT)
        with Session(self.engine) as s, s.begin():
            self._lock(s)
            return self._new(s, identity, body, request_id, kind=K.ROLLBACK if rollback else K.CONFIGURATION,
                             target=body.target_version if rollback else None)

    def edit(self, identity, cr_id, body, request_id):
        self.access.require(identity, P.REGISTRY_EDIT)
        with Session(self.engine) as s, s.begin():
            self._lock(s)
            row = self._row(s, cr_id); cr = ChangeRequest.model_validate(row.payload)
            if cr.revision != body.expected_revision or cr.status != S.DRAFT or cr.kind != K.CONFIGURATION:
                raise AdminError("CHANGE_REQUEST_CONFLICT")
            self._fresh(s, cr)
            old = self._definition(s, cr.proposed_registry_version)
            proposed = body.definition.model_dump(mode="json")
            refs = {v.system_id: v.credential_ref for v in old.systems}
            known_codes = {r["system_code"] for r in read_records(s.get(InventoryHeadRow, self.tenant_id).payload)}
            for system in proposed["systems"]:
                if system.get("company_system_code") and system["company_system_code"] not in known_codes:
                    raise AdminError("UNKNOWN_COMPANY_SYSTEM", 422)
                if system["credential_ref"] == "[MASKED]" and not refs.get(system["system_id"]):
                    raise AdminError("INVALID_CREDENTIAL_REFERENCE", 422)
                # Browser cannot remove, replace or reveal a server-owned credential reference.
                system["credential_ref"] = refs.get(system["system_id"])
            version = self.admin.register(RegistryDefinition.model_validate(proposed), actor=identity.user_id, session=s)
            updated = self._save(s, row, cr, dict(title=body.title, reason=body.reason, proposed_registry_version=version))
            self._audit(s, "EDIT_DRAFT", identity, request_id, cr_id, cr.proposed_registry_version, version)
            return updated

    def transition(self, identity, cr_id, action, expected_revision, request_id, *, decision_comment=None):
        permissions = {"submit": P.REGISTRY_SUBMIT, "approve": P.REGISTRY_APPROVE,
            "reject": P.REGISTRY_APPROVE, "activate": P.REGISTRY_ACTIVATE, "cancel": P.REGISTRY_EDIT,
            "supersede": P.REGISTRY_EDIT}
        if action not in permissions:
            raise AdminError("ACTION_NOT_ALLOWED", 422)
        self.access.require(identity, permissions[action])
        from .models import Transition
        comment = Transition(expected_revision=expected_revision, decision_comment=decision_comment).decision_comment
        if action == "reject" and not comment:
            raise AdminError("REJECT_REASON_REQUIRED", 422)
        if action not in ("approve", "reject") and comment:
            raise AdminError("DECISION_COMMENT_NOT_ALLOWED", 422)
        with Session(self.engine) as s, s.begin():
            self._lock(s)
            row = self._row(s, cr_id); cr = ChangeRequest.model_validate(row.payload)
            if cr.revision != expected_revision:
                raise AdminError("CHANGE_REQUEST_CONFLICT")
            allowed = {"submit": (S.DRAFT,), "approve": (S.SUBMITTED,), "reject": (S.SUBMITTED,),
                "activate": (S.APPROVED,), "cancel": (S.DRAFT, S.SUBMITTED), "supersede": (S.DRAFT, S.SUBMITTED, S.APPROVED)}
            if cr.status not in allowed[action]:
                raise AdminError("INVALID_CHANGE_STATE")
            if action in ("approve", "reject") and identity.user_id in (cr.created_by, cr.submitted_by):
                raise AdminError("SELF_APPROVAL_REJECTED", 403)
            if action not in ("cancel", "supersede"):
                self._fresh(s, cr)
            statuses = dict(submit=S.SUBMITTED, approve=S.APPROVED, reject=S.REJECTED,
                activate=S.ACTIVATED, cancel=S.CANCELED, supersede=S.SUPERSEDED)
            changes = dict(status=statuses[action])
            if action in ("approve", "reject"):
                changes["approval_comment" if action == "approve" else "rejection_reason"] = comment
            if action in ("submit", "approve", "reject", "activate"):
                prefix = {"submit": "submitted", "approve": "approved", "reject": "rejected", "activate": "activated"}[action]
                changes.update({prefix + "_by": identity.user_id, prefix + "_at": self.clock()})
            if action == "activate":
                if cr.kind == K.INVENTORY:
                    if cr.proposed_registry_version != cr.base_registry_version:
                        raise AdminError("INVALID_INVENTORY_REGISTRY_CHANGE", 422)
                    inv = s.get(InventoryHeadRow, self.tenant_id)
                    payload = merge_records(inv.payload, row.inventory)
                    s.add(InventoryVersionRow(tenant_id=self.tenant_id, revision=inv.revision + 1,
                        change_request_id=cr_id, payload=payload))
                    cas = s.execute(update(InventoryHeadRow).where(InventoryHeadRow.tenant_id == self.tenant_id,
                        InventoryHeadRow.revision == cr.base_inventory_revision)
                        .values(revision=cr.base_inventory_revision + 1, payload=payload))
                    if cas.rowcount != 1:
                        raise AdminError("STALE_CHANGE_REQUEST")
                else:
                    # Existing RegistryAdmin owns Registry head CAS, in the same transaction.
                    self.admin.activate(cr.proposed_registry_version, expected_version=cr.base_registry_version,
                                        actor=identity.user_id, session=s)
            value = self._save(s, row, cr, changes)
            event = "ROLLED_BACK" if action == "activate" and cr.kind == K.ROLLBACK else statuses[action].value
            if action == "activate" and cr.kind == K.INVENTORY:
                event = "INVENTORY_ACTIVATED"
            metadata = {"decision_comment": comment} if action in ("approve", "reject") else {}
            if event == "INVENTORY_ACTIVATED":
                metadata.update(before_inventory_revision=cr.base_inventory_revision, after_inventory_revision=cr.base_inventory_revision + 1)
            self._audit(s, event, identity, request_id, cr_id, cr.base_registry_version, cr.proposed_registry_version, **metadata)
            return value

    def get(self, identity, cr_id):
        self.access.require(identity, P.REGISTRY_VIEW)
        with Session(self.engine) as s:
            cr = ChangeRequest.model_validate(self._row(s, cr_id).payload)
            before, after = self._definition(s, cr.base_registry_version), self._definition(s, cr.proposed_registry_version)
            return dict(change_request=cr.model_dump(mode="json"), definition=masked(after),
                        diff=version_diff(before, after).model_dump(mode="json"),
                        inventory_changes=read_records(self._row(s, cr_id).inventory))

    def read(self, identity, resource, *, version=None, page=1, size=20, status=None):
        self.access.require(identity, P.REGISTRY_AUDIT if resource == "audit" else P.REGISTRY_VIEW)
        with Session(self.engine) as s:
            head = self._head(s)
            if resource == "dashboard":
                return dict(active_version=head, inventory_revision=s.get(InventoryHeadRow, self.tenant_id).revision,
                    system_count=len(s.get(InventoryHeadRow, self.tenant_id).payload),
                    registry_source_count=len(self._definition(s, head).systems), permissions=self.access.permissions(identity),
                    roles=self.access.roles(identity), identity=identity.model_dump(mode="json"))
            if resource == "definition":
                return masked(self._definition(s, version if version else head))
            if resource == "inventory":
                data = read_records(s.get(InventoryHeadRow, self.tenant_id).payload)
                return dict(items=data[(page-1)*size:page*size], total=len(data))
            if resource == "versions":
                history = select(RegistryAuditRow.version.label("version"), func.min(RegistryAuditRow.id).label("sequence"),
                    func.min(RegistryAuditRow.occurred_at).label("registered_at")).where(
                        RegistryAuditRow.tenant_id == self.tenant_id, RegistryAuditRow.event == "REGISTERED").group_by(RegistryAuditRow.version).subquery()
                count = s.scalar(select(func.count()).select_from(RegistryVersionRow).where(RegistryVersionRow.tenant_id == self.tenant_id))
                rows = s.execute(select(RegistryVersionRow.version, history.c.registered_at).outerjoin(history,
                    history.c.version == RegistryVersionRow.version).where(RegistryVersionRow.tenant_id == self.tenant_id)
                    .order_by(history.c.sequence.desc().nulls_last(), RegistryVersionRow.version).offset((page-1)*size).limit(size)).all()
                activated = set(s.scalars(select(RegistryAuditRow.version).where(RegistryAuditRow.tenant_id == self.tenant_id,
                    RegistryAuditRow.event == "ACTIVATED")).all())
                return dict(items=[dict(version=v, active=v == head, registered_at=time,
                    activated_before=v in activated) for v, time in rows], total=count)
            cls = {"changes": ChangeRow, "versions": RegistryVersionRow, "audit": AdminAuditRow}[resource]
            query = select(cls).where(cls.tenant_id == self.tenant_id)
            if resource == "changes" and status:
                query = query.where(ChangeRow.status == S(status).value)
            order = {"changes": ChangeRow.change_request_id, "versions": RegistryVersionRow.version,
                     "audit": AdminAuditRow.sequence}[resource]
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            rows = s.scalars(query.order_by(order.desc()).offset((page-1)*size).limit(size)).all()
            items = ([dict(version=r.version, active=r.version == head) for r in rows] if resource == "versions"
                     else [r.payload for r in rows])
            return dict(items=items, total=total)

    def diff(self, identity, before, after):
        self.access.require(identity, P.REGISTRY_VIEW)
        with Session(self.engine) as s:
            return version_diff(self._definition(s, before), self._definition(s, after))

    def preview(self, identity, content, filename, request_id):
        self.access.require(identity, P.REGISTRY_EDIT)
        from .importer import parse_inventory
        parsed = parse_inventory(content, filename, identity.user_id, self.clock())
        rows = parsed["rows"]
        with Session(self.engine) as s, s.begin():
            self._lock(s)
            inv = s.get(InventoryHeadRow, self.tenant_id)
            old = {r["system_code"]: r for r in read_records(inv.payload)}
            merged = {r["system_code"]: r for r in merge_records(inv.payload, rows)}
            preview_id = "IMP-" + uuid4().hex
            added, changed, unchanged = [], [], []
            for row in rows:
                code = row["system_code"]
                group = added if code not in old else (changed if content_identity(old[code]) != content_identity(merged[code]) else unchanged)
                group.append(code)
            data = dict(preview_id=preview_id, base_registry_version=self._head(s), base_inventory_revision=inv.revision,
                        **parsed, added=added, changed=changed, unchanged=unchanged)
            s.add(ImportPreviewRow(preview_id=preview_id, tenant_id=self.tenant_id, created_by=identity.user_id, payload=data))
            self._audit(s, "IMPORT_PREVIEW", identity, request_id, before=self._head(s))
            return data

    def confirm_import(self, identity, body, request_id):
        self.access.require(identity, P.REGISTRY_EDIT)
        from .models import CreateDraft
        with Session(self.engine) as s, s.begin():
            self._lock(s)
            p = s.get(ImportPreviewRow, body.preview_id)
            if p is None or p.tenant_id != self.tenant_id or p.created_by != identity.user_id:
                raise AdminError("PREVIEW_NOT_FOUND", 404)
            if p.confirmed_cr:
                return ChangeRequest.model_validate(self._row(s, p.confirmed_cr).payload)
            if p.payload["invalid"] or p.payload.get("conflicts") or not p.payload["rows"]:
                raise AdminError("INVALID_IMPORT_ROWS", 422)
            if p.payload["base_inventory_revision"] != s.get(InventoryHeadRow, self.tenant_id).revision:
                raise AdminError("STALE_CHANGE_REQUEST")
            draft = CreateDraft(title=body.title, reason=body.reason, base_registry_version=p.payload["base_registry_version"])
            cr = self._new(s, identity, draft, request_id, kind=K.INVENTORY, inventory=p.payload["rows"])
            p.confirmed_cr = cr.change_request_id
            self._audit(s, "IMPORT_CONFIRMED", identity, request_id, cr.change_request_id,
                        cr.base_registry_version, cr.proposed_registry_version)
            return cr

    def company_systems(self, identity, *, code=None, page=1, size=20, system_code=None, system_name=None,
                        system_type_label=None, inventory_group=None, configuration_status=None, has_capability=None):
        self.access.require(identity, P.REGISTRY_VIEW)
        with Session(self.engine) as s:
            definition = masked(self._definition(s, self._head(s)))
            records = read_records(s.get(InventoryHeadRow, self.tenant_id).payload)
            joined = [join_record(r, definition) for r in records]
            if code is not None:
                found = next((r for r in joined if r["company_system"]["system_code"] == code), None)
                if found is None:
                    raise AdminError("SYSTEM_NOT_FOUND", 404)
                return found
            results = []
            for detail in joined:
                row = detail["company_system"]
                if any(value and value.casefold() not in row[field].casefold() for field, value in
                       (("system_code", system_code), ("system_name", system_name), ("system_type_label", system_type_label))):
                    continue
                if inventory_group and inventory_group not in detail["inventory_groups"]:
                    continue
                if configuration_status and configuration_status != detail["agent_configuration_status"]:
                    continue
                if has_capability is not None and has_capability != bool(detail["capability_count"]):
                    continue
                results.append({**{k: row[k] for k in ("system_code", "system_name", "system_type_label")},
                    **{k: detail[k] for k in ("inventory_groups", "agent_configuration_status", "agent_source_count", "capability_count")}})
            results.sort(key=lambda r: r["system_code"])
            return dict(items=results[(page-1)*size:page*size], total=len(results))
