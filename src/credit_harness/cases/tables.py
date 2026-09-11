from sqlalchemy import JSON, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from credit_harness.persistence.store import Base


class CaseRow(Base):
    __tablename__ = "investigation_cases"
    case_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80), index=True)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"))
    grant_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))
    max_tool_calls: Mapped[int] = mapped_column(Integer)
    used_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[str] = mapped_column(String(40))


class CaseCallRow(Base):
    """Small dispatch receipt, not a retry queue or durable workflow."""
    __tablename__ = "case_tool_calls"
    __table_args__ = (UniqueConstraint("case_id", "sequence"),)
    call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    tool: Mapped[str] = mapped_column(String(50))
    request: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20))
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("tool_observations.id"), unique=True)
