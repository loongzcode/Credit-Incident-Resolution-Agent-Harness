from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from credit_harness.persistence.store import Base


class EvidenceRow(Base):
    __tablename__ = "case_evidence"
    evidence_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey("tool_observations.id"))
    payload: Mapped[dict] = mapped_column(JSON)


class EvidenceOriginRow(Base):
    """Deduplicated claims still retain every contributing tool-call receipt."""
    __tablename__ = "evidence_origins"
    evidence_id: Mapped[str] = mapped_column(ForeignKey("case_evidence.evidence_id"), primary_key=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("case_tool_calls.call_id"), primary_key=True)
