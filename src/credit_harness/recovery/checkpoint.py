from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import AwareDatetime
from credit_harness.agent.models import AgentRunStatus
from credit_harness.context.models import Hash
from credit_harness.context.structured_values import OpaqueSubjectRef
from credit_harness.domain.models import Model
from credit_harness.cases.repository import utc_now
from .tables import AgentCheckpointRow


class DurableAgentCheckpoint(Model):
    run_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    last_completed_turn: int
    last_snapshot_id: Hash
    last_decision_id: Hash | None
    last_call_id: OpaqueSubjectRef | None
    stop_status: AgentRunStatus | None
    updated_at: AwareDatetime


class AgentCheckpointStore:
    """Audit pointer only. Never an execution ticket or planner input."""
    def __init__(self, cases):
        self.cases = cases

    def save(self, run_id, case_id, turn, snapshot_id, decision_id=None, call_id=None, stop_status=None):
        checkpoint = DurableAgentCheckpoint(run_id=run_id, case_id=case_id, last_completed_turn=turn,
            last_snapshot_id=snapshot_id, last_decision_id=decision_id, last_call_id=call_id,
            stop_status=stop_status, updated_at=utc_now())
        values = checkpoint.model_dump(mode="json")
        values["updated_at"] = checkpoint.updated_at.timestamp()
        with Session(self.cases.engine) as session, session.begin():
            self.cases._row(session, case_id)
            row = session.get(AgentCheckpointRow, run_id)
            if row is None:
                session.add(AgentCheckpointRow(**values))
            elif row.case_id != case_id or row.last_completed_turn > turn:
                raise ValueError("checkpoint cannot move backwards or change Case")
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def list(self, case_id):
        with Session(self.cases.engine) as session:
            self.cases._row(session, case_id)
            return tuple(DurableAgentCheckpoint(run_id=r.run_id, case_id=r.case_id,
                last_completed_turn=r.last_completed_turn, last_snapshot_id=r.last_snapshot_id,
                last_decision_id=r.last_decision_id, last_call_id=r.last_call_id, stop_status=r.stop_status,
                updated_at=datetime.fromtimestamp(r.updated_at, timezone.utc)) for r in session.scalars(
                    select(AgentCheckpointRow).where(AgentCheckpointRow.case_id == case_id).order_by(AgentCheckpointRow.updated_at)))
