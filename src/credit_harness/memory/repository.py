from datetime import datetime, timezone
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from credit_harness.context.budget import digest
from .models import VerifiedIncidentExperience, ExperienceStatus, MemoryError
from .tables import ExperienceRow, GuidanceAuditRow


def experience_identity(experience):
    return digest(experience.model_dump(mode="json", exclude={"experience_id"}))


def checked_experience(row, tenant_id):
    e = VerifiedIncidentExperience.model_validate(row.payload)
    if (row.tenant_id != tenant_id or e.tenant_id != tenant_id or row.experience_id != e.experience_id
            or experience_identity(e) != e.experience_id or row.closure_id != e.closure_id
            or row.source_case_id != e.source_case_id or row.report_id != e.report_id
            or row.schema_version != e.experience_schema_version
            or row.signature_hash != digest(e.incident_signature.model_dump(mode="json"))
            or row.partner != e.partner_context.funding_partner or row.protocol != e.incident_signature.protocol_version):
        raise MemoryError("experience integrity invalid")
    ExperienceStatus(row.status)
    return e


class SQLExperienceRepository:
    """No public upsert. Only Publisher inserts; trusted administration revokes."""
    def __init__(self, cases, *, clock=lambda: datetime.now(timezone.utc)):
        self.cases, self.engine, self.tenant_id, self.clock = cases, cases.engine, cases.tenant_id, clock

    def get(self, experience_id):
        with Session(self.engine) as session:
            row = session.scalar(select(ExperienceRow).where(ExperienceRow.experience_id == experience_id,
                ExperienceRow.tenant_id == self.tenant_id))
            if row is None:
                raise MemoryError("experience unavailable")
            return checked_experience(row, self.tenant_id)

    def active(self):
        with Session(self.engine) as session:
            return tuple(checked_experience(r, self.tenant_id) for r in session.scalars(select(ExperienceRow)
                .where(ExperienceRow.tenant_id == self.tenant_id, ExperienceRow.status == ExperienceStatus.ACTIVE.value)
                .order_by(ExperienceRow.experience_id)))

    def revoke(self, experience_id):
        with Session(self.engine) as session, session.begin():
            row = session.scalar(select(ExperienceRow).where(ExperienceRow.experience_id == experience_id,
                ExperienceRow.tenant_id == self.tenant_id))
            if row is None:
                raise MemoryError("experience unavailable")
            checked_experience(row, self.tenant_id)
            result = session.execute(update(ExperienceRow).where(ExperienceRow.experience_id == experience_id,
                ExperienceRow.tenant_id == self.tenant_id, ExperienceRow.status == ExperienceStatus.ACTIVE.value)
                .values(status=ExperienceStatus.REVOKED.value))
            if result.rowcount:
                session.add(GuidanceAuditRow(tenant_id=self.tenant_id, kind="EXPERIENCE", object_id=experience_id,
                    version=row.schema_version, from_status=ExperienceStatus.ACTIVE.value,
                    to_status=ExperienceStatus.REVOKED.value, recorded_at=self.clock().isoformat()))

    def _insert(self, session, e):
        if e.tenant_id != self.tenant_id or experience_identity(e) != e.experience_id:
            raise MemoryError("experience integrity invalid")
        session.add(ExperienceRow(experience_id=e.experience_id, tenant_id=e.tenant_id,
            source_case_id=e.source_case_id, closure_id=e.closure_id, report_id=e.report_id,
            schema_version=e.experience_schema_version, signature_hash=digest(e.incident_signature.model_dump(mode="json")),
            partner=e.partner_context.funding_partner, protocol=e.incident_signature.protocol_version,
            status=ExperienceStatus.ACTIVE.value, created_at=e.created_at.isoformat(), payload=e.model_dump(mode="json")))
        session.add(GuidanceAuditRow(tenant_id=self.tenant_id, kind="EXPERIENCE", object_id=e.experience_id,
            version=e.experience_schema_version, from_status=None, to_status=ExperienceStatus.ACTIVE.value,
            recorded_at=self.clock().isoformat()))
