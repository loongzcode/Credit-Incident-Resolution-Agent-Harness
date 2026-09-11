from sqlalchemy import JSON, ForeignKey, String, Integer
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class AuthorizedIntentRow(Base):
    __tablename__ = "authorized_intents"
    intent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"))
    payload: Mapped[dict] = mapped_column(JSON)


class ApprovalRow(Base):
    __tablename__ = "remediation_approvals"
    approval_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("authorized_intents.intent_id"))
    status: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)


class CapabilityRow(Base):
    __tablename__ = "execution_capabilities"
    capability_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    intent_id: Mapped[str] = mapped_column(ForeignKey("authorized_intents.intent_id"))
    payload: Mapped[dict] = mapped_column(JSON)
    used_effect_id: Mapped[str | None] = mapped_column(String(64))


class EffectRow(Base):
    __tablename__ = "side_effect_ledger"
    effect_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    capability_id: Mapped[str] = mapped_column(ForeignKey("execution_capabilities.capability_id"), unique=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    status: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)


class CapabilitySignatureRow(Base):
    """Private issuance envelope for PREPARED recovery. Never model/audit data."""
    __tablename__ = "capability_signatures"
    capability_id: Mapped[str] = mapped_column(ForeignKey("execution_capabilities.capability_id"), primary_key=True)
    signature: Mapped[str] = mapped_column(String(64))


class AuthorizationAuditRow(Base):
    __tablename__ = "authorization_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    audit_id: Mapped[str] = mapped_column(String(80), unique=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


def create_authorization_schema(engine):
    Base.metadata.create_all(engine, tables=[AuthorizedIntentRow.__table__, ApprovalRow.__table__,
        CapabilityRow.__table__, CapabilitySignatureRow.__table__, EffectRow.__table__, AuthorizationAuditRow.__table__])
    from credit_harness.recovery.tables import create_recovery_schema
    create_recovery_schema(engine)
