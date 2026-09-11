"""Read-only external effect status. Does not inspect the business Oracle."""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.cases.tables import CaseRow
from credit_harness.context.budget import digest
from credit_harness.recovery.models import RecoveryCapability, SideEffectLookupResult, LookupStatus as L
from .synthetic_remediation import SyntheticEffectRow


class SyntheticEffectStatusResolver:
    def __init__(self, engine, tenant_id, *, clock=None):
        self.engine, self.tenant_id = engine, tenant_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.capability = RecoveryCapability(resolver_id="SYNTHETIC-EFFECT-STORE", contract_version="1",
            not_found_proves_no_effect=False)

    def lookup(self, correlation_id, command_identity):
        command = command_identity.command
        result = dict(correlation_id=correlation_id,
            command_identity_hash=digest(command_identity.model_dump(mode="json")),
            resolver_id=self.capability.resolver_id, observed_at=self.clock())
        with Session(self.engine) as session:
            case = session.scalar(select(CaseRow).where(CaseRow.case_id == command.case_id,
                                                       CaseRow.tenant_id == self.tenant_id))
            if (case is None or command.tenant_id != self.tenant_id
                    or case.payload["internal_order_id"] != command.internal_order_id):
                return SideEffectLookupResult(**result, lookup_status=L.INDETERMINATE)
            target = getattr(command, "message_ref", None) or getattr(command, "delivery_ref", None) or command.case_id
            key = digest(dict(world=case.simulation_id, action=command.action_type.value, target=target))
            records = session.scalars(select(SyntheticEffectRow).where(SyntheticEffectRow.correlation_id == correlation_id).limit(2)).all()
            if not records:
                # Absence at this instant cannot rule out a delayed original
                # sender arriving later, even in an ACID synthetic database.
                return SideEffectLookupResult(**result, lookup_status=L.NOT_FOUND)
            if len(records) != 1 or records[0].effect_key != key or records[0].payload_hash != command_identity.payload_hash:
                return SideEffectLookupResult(**result, lookup_status=L.INDETERMINATE)
            return SideEffectLookupResult(**result, lookup_status=L.FOUND_APPLIED,
                external_effect_ref=records[0].external_ref, remote_status_reference=records[0].external_ref)
