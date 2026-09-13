from sqlalchemy import String, Integer, Float, JSON
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class MigrationAuditRow(Base):
    __tablename__ = "schema_migration_audit"
    revision: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[str] = mapped_column(String(40))
    phase: Mapped[str] = mapped_column(String(32))


class WorkerHeartbeatRow(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32))


class SourceGenerationRow(Base):
    __tablename__ = "retrieval_source_generations"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class IndexCompletenessRow(Base):
    __tablename__ = "retrieval_completeness"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    indexed_generation: Mapped[int] = mapped_column(Integer)
    last_reconciled_at: Mapped[float] = mapped_column(Float)


class SpaceAdminAuditRow(Base):
    __tablename__ = "retrieval_space_admin_audit"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(128))
    from_space: Mapped[str] = mapped_column(String(64))
    to_space: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float)


class RetrievalMetricRow(Base):
    __tablename__ = "retrieval_metrics"
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    latency_count: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_sum_seconds: Mapped[float] = mapped_column(Float, nullable=False)
