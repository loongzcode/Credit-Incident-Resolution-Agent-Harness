from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base
from credit_harness.authorization import tables as authorization_tables  # noqa: F401 - resolve ledger FK even in standalone admin/test bootstrap


class ReadDispatchRecoveryRow(Base):
    __tablename__ = "read_dispatch_recovery"
    call_id: Mapped[str] = mapped_column(ForeignKey("case_tool_calls.call_id"), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    dispatch_correlation_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(40))
    recovered_observation_id: Mapped[str | None] = mapped_column(String(36))
    recovered_evidence_refs: Mapped[list] = mapped_column(JSON)
    attempted_at: Mapped[float] = mapped_column(Float)


class AgentCheckpointRow(Base):
    __tablename__ = "agent_checkpoints"
    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    last_completed_turn: Mapped[int] = mapped_column(Integer)
    last_snapshot_id: Mapped[str] = mapped_column(String(64))
    last_decision_id: Mapped[str | None] = mapped_column(String(64))
    last_call_id: Mapped[str | None] = mapped_column(String(80))
    stop_status: Mapped[str | None] = mapped_column(String(48))
    updated_at: Mapped[float] = mapped_column(Float)


class EffectRecoveryStateRow(Base):
    __tablename__ = "effect_recovery_state"
    __table_args__ = (Index("ix_recovery_due", "tenant_id", "status", "next_eligible_at"),)
    effect_id: Mapped[str] = mapped_column(ForeignKey("side_effect_ledger.effect_id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(80))
    case_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24))
    ledger_updated_at: Mapped[float] = mapped_column(Float)
    next_eligible_at: Mapped[float] = mapped_column(Float)
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[float | None] = mapped_column(Float)
    lease_token: Mapped[str | None] = mapped_column(String(80))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    requires_escalation: Mapped[bool] = mapped_column(Boolean, default=False)


class EffectRecoveryAttemptRow(Base):
    __tablename__ = "effect_recovery_attempts"
    __table_args__ = (UniqueConstraint("effect_id", "sequence"),)
    recovery_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    worker_id: Mapped[str] = mapped_column(String(80))
    effect_id: Mapped[str] = mapped_column(ForeignKey("side_effect_ledger.effect_id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[float] = mapped_column(Float)
    completed_at: Mapped[float | None] = mapped_column(Float)
    source_status: Mapped[str] = mapped_column(String(24))
    resolver_id: Mapped[str] = mapped_column(String(80))
    result_status: Mapped[str | None] = mapped_column(String(24))
    lookup_result: Mapped[dict | None] = mapped_column(JSON)
    proof: Mapped[dict | None] = mapped_column(JSON)
    proof_hash: Mapped[str | None] = mapped_column(String(64))
    failure_code: Mapped[str | None] = mapped_column(String(48))


class RecoveryAuditRow(Base):
    __tablename__ = "effect_recovery_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effect_id: Mapped[str] = mapped_column(ForeignKey("side_effect_ledger.effect_id"), index=True)
    recovery_id: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


def synchronize_effect(session, ledger):
    """Same transaction as Ledger: indexed discovery never lags its source."""
    row = session.get(EffectRecoveryStateRow, ledger.effect_id)
    from credit_harness.orchestration.handoff import effect_handoff
    effect_handoff(session, ledger)
    timestamp = ledger.updated_at.timestamp()
    if row is None:
        session.flush()  # Ledger FK must exist first.
        session.add(EffectRecoveryStateRow(effect_id=ledger.effect_id, tenant_id=ledger.tenant_id,
            case_id=ledger.case_id, status=ledger.status.value, ledger_updated_at=timestamp,
            next_eligible_at=timestamp, attempt_count=0, requires_escalation=False))
    else:
        row.status, row.ledger_updated_at = ledger.status.value, timestamp
        if ledger.status.value not in ("PREPARED", "DISPATCHED", "ACCEPTED", "UNKNOWN"):
            row.requires_escalation = False


def create_recovery_schema(engine):
    from sqlalchemy import select, update
    from sqlalchemy.orm import Session
    from credit_harness.authorization.tables import EffectRow
    from credit_harness.authorization.models import SideEffectLedger
    from credit_harness.cases.tables import CaseRow
    from credit_harness.orchestration.tables import create_orchestration_schema
    create_orchestration_schema(engine)
    Base.metadata.create_all(engine, tables=[ReadDispatchRecoveryRow.__table__, AgentCheckpointRow.__table__, EffectRecoveryStateRow.__table__,
        EffectRecoveryAttemptRow.__table__, RecoveryAuditRow.__table__])
    # Additive bootstrap for existing Step 8 Ledgers. Re-running preserves leases,
    # attempts and backoff. No synthesized capability signature for legacy rows.
    with Session(engine) as session, session.begin():
        for row in session.scalars(select(EffectRow)):
            # Also backfill Step 13 handoffs for pre-existing Step 8/9 ledgers.
            # synchronize_effect preserves live leases, attempts and backoff.
            session.execute(update(CaseRow).where(CaseRow.case_id == row.case_id)
                .values(updated_at=CaseRow.updated_at))
            session.refresh(row)
            synchronize_effect(session, SideEffectLedger.model_validate(row.payload))
