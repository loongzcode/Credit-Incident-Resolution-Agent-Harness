from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class ExperienceRow(Base):
    __tablename__ = "verified_incident_experiences"
    __table_args__ = (UniqueConstraint("tenant_id", "closure_id", "schema_version"),
                     Index("ix_experience_scope", "tenant_id", "partner", "protocol", "signature_hash"))
    experience_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    source_case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"))
    closure_id: Mapped[str] = mapped_column(String(64))
    report_id: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))
    signature_hash: Mapped[str] = mapped_column(String(64))
    partner: Mapped[str | None] = mapped_column(String(128))
    protocol: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)


class SkillRow(Base):
    __tablename__ = "organizational_skills"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    skill_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)


class GuidanceAuditRow(Base):
    __tablename__ = "organizational_guidance_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    object_id: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    recorded_at: Mapped[str] = mapped_column(String(40))


def create_memory_schema(engine):
    from credit_harness.production.schema import runtime_managed
    if runtime_managed(engine):
        return  # schema version is validated by the production startup gate
    Base.metadata.create_all(engine, tables=[ExperienceRow.__table__, SkillRow.__table__, GuidanceAuditRow.__table__])
