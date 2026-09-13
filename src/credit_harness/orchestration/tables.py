from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class WorkItemRow(Base):
    __tablename__ = "case_work_items"
    __table_args__ = (Index("ix_case_work_due", "tenant_id", "status", "not_before"),)
    work_item_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    not_before: Mapped[float] = mapped_column(Float)
    lease_until: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)


class OrchestrationStateRow(Base):
    __tablename__ = "case_orchestration_state"
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class SignalRow(Base):
    __tablename__ = "case_resolution_signals"
    signal_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class OrchestrationAuditRow(Base):
    __tablename__ = "case_orchestration_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class ResumedRunRow(Base):
    __tablename__ = "orchestration_agent_runs"
    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    work_item_id: Mapped[str] = mapped_column(ForeignKey("case_work_items.work_item_id"), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)


class EvaluationHandoffRow(Base):
    __tablename__ = "evaluation_handoff_outbox"
    __table_args__ = (Index("ix_evaluation_handoff_pending", "tenant_id", "status", "sequence"),)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evaluation_run_id: Mapped[str] = mapped_column(String(80), unique=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"))
    status: Mapped[str] = mapped_column(String(24), default="PENDING")


def create_orchestration_schema(engine):
    from credit_harness.production.schema import runtime_managed
    if runtime_managed(engine):
        return  # schema version is validated by the production startup gate
    Base.metadata.create_all(engine, tables=[OrchestrationStateRow.__table__, WorkItemRow.__table__,
        SignalRow.__table__, OrchestrationAuditRow.__table__, ResumedRunRow.__table__, EvaluationHandoffRow.__table__])
