"""Privileged synthetic external service. Only this adapter touches Oracle state.

No authorization/model service imports this implementation; dependency is the
typed adapter Protocol. No actual bank, payment client or network is configured.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import JSON, String, select, update, func
from sqlalchemy.orm import Mapped, mapped_column, Session
from credit_harness.persistence.store import Base, SimulationRow, SnapshotRow, snapshots
from credit_harness.cases.tables import CaseRow
from credit_harness.context.budget import digest
from credit_harness.domain.enums import ConsumeStatus, DeliveryStatus
from credit_harness.authorization.commands import (COMMAND_ADAPTER, ReplayCallbackCommand,
    RedeliverAssetNotificationCommand, CreateReconciliationTaskCommand, RequestOperatorReviewCommand)
from credit_harness.authorization.models import SideEffectReceipt, ReceiptOutcome


class SyntheticEffectRow(Base):
    __tablename__ = "synthetic_external_effects"
    effect_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(80))
    external_ref: Mapped[str] = mapped_column(String(80))


class SyntheticTaskRow(Base):
    __tablename__ = "synthetic_remediation_tasks"
    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


def create_synthetic_effect_schema(engine):
    Base.metadata.create_all(engine, tables=[SyntheticEffectRow.__table__, SyntheticTaskRow.__table__])


class SyntheticRemediationAdapter:
    def __init__(self, engine, tenant_id):
        self.engine, self.tenant_id = engine, tenant_id

    def dispatch(self, command, correlation_id):
        command = COMMAND_ADAPTER.validate_python(command.model_dump())
        def receipt(outcome, ref=None):
            return SideEffectReceipt(correlation_id=correlation_id, outcome=outcome,
                external_effect_ref=ref, observed_at=datetime.now(timezone.utc),
                no_effect_confirmed=outcome == ReceiptOutcome.FAILED_CONFIRMED)
        with Session(self.engine) as session, session.begin():
            case = session.scalar(select(CaseRow).where(CaseRow.case_id == command.case_id,
                                  CaseRow.tenant_id == self.tenant_id))
            if (case is None or command.tenant_id != self.tenant_id
                    or case.payload["internal_order_id"] != command.internal_order_id):
                return receipt(ReceiptOutcome.FAILED_CONFIRMED)
            # Serialize mutations of the external synthetic aggregate, including
            # different Cases referring to the same world. No financial mutation.
            session.execute(update(SimulationRow).where(SimulationRow.id == case.simulation_id)
                            .values(clock_version=SimulationRow.clock_version))
            simulation = session.get(SimulationRow, case.simulation_id)
            now = datetime.fromisoformat(simulation.clock)
            history = [(r, w) for r, w in snapshots(session, case.simulation_id) if w.event_time <= now]
            _, world = history[-1]
            target = getattr(command, "message_ref", None) or getattr(command, "delivery_ref", None) or command.case_id
            key = digest(dict(world=case.simulation_id, action=command.action_type.value, target=target))
            payload_hash = digest(command.model_dump(mode="json"))
            existing = session.get(SyntheticEffectRow, key)
            if existing:
                if existing.payload_hash != payload_hash:
                    return receipt(ReceiptOutcome.FAILED_CONFIRMED)
                return receipt(ReceiptOutcome.APPLIED, existing.external_ref)
            event_time = now + timedelta(microseconds=1)
            changed = None
            if isinstance(command, ReplayCallbackCommand):
                matched = [m for m in world.messages if m.message_id == command.message_ref
                           and m.message.event_id == command.callback_event_ref]
                if len(matched) != 1 or matched[0].consume_status != ConsumeStatus.FAILED:
                    return receipt(ReceiptOutcome.FAILED_CONFIRMED)
                replacement = matched[0].model_copy(update=dict(consume_status=ConsumeStatus.CONSUMED,
                    error=None, dlq=None, event_time=event_time))
                changed = world.model_copy(update=dict(event_time=event_time,
                    messages=tuple(replacement if m == matched[0] else m for m in world.messages)))
            elif isinstance(command, RedeliverAssetNotificationCommand):
                delivery = world.asset_delivery
                if delivery.event_id != command.delivery_ref or delivery.delivery_status != DeliveryStatus.FAILED:
                    return receipt(ReceiptOutcome.FAILED_CONFIRMED)
                changed = world.model_copy(update=dict(event_time=event_time, asset_delivery=delivery.model_copy(
                    update=dict(delivery_status=DeliveryStatus.DELIVERED, error_code=None, event_time=event_time))))
            elif isinstance(command, (CreateReconciliationTaskCommand, RequestOperatorReviewCommand)):
                session.add(SyntheticTaskRow(task_id="TASK-" + key, case_id=command.case_id,
                                            payload=command.model_dump(mode="json")))
            else:
                return receipt(ReceiptOutcome.FAILED_CONFIRMED)
            if changed is not None:
                revision = session.scalar(select(func.max(SnapshotRow.revision)).where(
                    SnapshotRow.simulation_id == case.simulation_id)) + 1
                session.add(SnapshotRow(simulation_id=case.simulation_id, revision=revision,
                                        state=changed.model_dump(mode="json")))
                simulation.clock = event_time.isoformat()
                simulation.clock_version += 1
            ref = "EFFECT-" + key
            session.add(SyntheticEffectRow(effect_key=key, payload_hash=payload_hash,
                correlation_id=correlation_id, external_ref=ref))
            return receipt(ReceiptOutcome.APPLIED, ref)
