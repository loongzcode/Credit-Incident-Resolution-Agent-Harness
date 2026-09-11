from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from credit_harness.persistence.store import GrantRow, SimulationRow, token_hash
from credit_harness.tools.contracts import ToolQuery
from credit_harness.domain.enums import ToolName
from .models import Case, CaseAccessError, CasePolicyError, CaseStatus
from .tables import CaseCallRow, CaseRow


class CallState(StrEnum):
    DISPATCHED = "DISPATCHED"
    OBSERVED = "OBSERVED"
    ERROR = "ERROR"


def utc_now():
    return datetime.now(timezone.utc)


def hydrate(row: CaseRow) -> Case:
    return Case.model_validate({
        **row.payload, "status": row.status, "updated_at": row.updated_at,
        "budget": {"max_tool_calls": row.max_tool_calls, "used_tool_calls": row.used_tool_calls},
    })


class CaseRepository:
    """Trusted server repository. Tenant comes from deployment/auth, never query JSON."""

    def __init__(self, engine, tenant_id: str):
        self.engine, self.tenant_id = engine, tenant_id

    def _row(self, session, case_id):
        row = session.scalar(select(CaseRow).where(
            CaseRow.case_id == case_id, CaseRow.tenant_id == self.tenant_id,
        ))
        if row is None:
            raise CaseAccessError("case unavailable")
        return row

    def get(self, case_id: str) -> Case:
        with Session(self.engine) as session:
            return hydrate(self._row(session, case_id))

    def create(self, case: Case, tool_credential: str) -> Case:
        if case.tenant_id != self.tenant_id:
            raise CaseAccessError("tenant mismatch")
        if case.status != CaseStatus.NEW or case.budget.used_tool_calls != 0:
            raise CasePolicyError("new cases must start NEW with zero usage")
        with Session(self.engine) as session, session.begin():
            grant = session.get(GrantRow, token_hash(tool_credential))
            simulation = session.get(SimulationRow, case.simulation_id)
            if (grant is None or simulation is None or grant.simulation_id != case.simulation_id
                    or datetime.fromisoformat(grant.expires_at) <= datetime.fromisoformat(simulation.clock)
                    or not case.scope.allowed_order_ids <= {grant.order_id}
                    or not {t.value for t in case.scope.allowed_tools} <= set(grant.allowed_tools)):
                raise CaseAccessError("case scope must be contained in an active tool grant")
            session.add(CaseRow(
                case_id=case.case_id, tenant_id=case.tenant_id, simulation_id=case.simulation_id,
                grant_hash=grant.token_hash, payload=case.model_dump(mode="json"),
                status=case.status.value, max_tool_calls=case.budget.max_tool_calls,
                used_tool_calls=0, updated_at=case.updated_at.isoformat(),
            ))
        return case

    def reserve_call(self, case_id: str, tool: ToolName, query: ToolQuery) -> tuple[Case, str]:
        with Session(self.engine) as session, session.begin():
            case = hydrate(self._row(session, case_id))
            if query.internal_order_id not in case.scope.allowed_order_ids:
                raise CasePolicyError("order outside case scope")
            if tool not in case.scope.allowed_tools or tool.value in case.constraints.forbidden_actions:
                raise CasePolicyError("tool outside case scope")
            if tool != ToolName.PROTOCOL and (query.protocol_version or query.effective_at):
                raise CasePolicyError("protocol parameters require protocol tool")
            # A single conditional UPDATE serializes concurrent budget reservations on
            # both SQLite and PostgreSQL. No DB transaction spans the HTTP call.
            result = session.execute(update(CaseRow).where(
                CaseRow.case_id == case_id, CaseRow.tenant_id == self.tenant_id,
                CaseRow.status.in_([CaseStatus.NEW.value, CaseStatus.INVESTIGATING.value]),
                CaseRow.used_tool_calls < CaseRow.max_tool_calls,
            ).values(used_tool_calls=CaseRow.used_tool_calls + 1,
                     status=CaseStatus.INVESTIGATING.value, updated_at=utc_now().isoformat())
                .returning(CaseRow.used_tool_calls).execution_options(synchronize_session=False))
            sequence = result.scalar_one_or_none()
            if sequence is None:
                raise CasePolicyError("case paused/closed or tool budget exhausted")
            call_id = str(uuid4())
            session.add(CaseCallRow(call_id=call_id, case_id=case_id, sequence=sequence,
                                    tool=tool.value, request=query.model_dump(mode="json"),
                                    state=CallState.DISPATCHED.value))
        return self.get(case_id), call_id

    def mark_error(self, case_id: str, call_id: str) -> None:
        with Session(self.engine) as session, session.begin():
            self._row(session, case_id)
            session.execute(update(CaseCallRow).where(
                CaseCallRow.case_id == case_id, CaseCallRow.call_id == call_id,
                CaseCallRow.state == CallState.DISPATCHED.value,
            ).values(state=CallState.ERROR.value))

    def pause(self, case_id: str, status: CaseStatus) -> Case:
        if status not in (CaseStatus.WAITING, CaseStatus.ESCALATED):
            raise CasePolicyError("only WAITING or ESCALATED is supported; closure is unavailable")
        with Session(self.engine) as session, session.begin():
            self._row(session, case_id)
            result = session.execute(update(CaseRow).where(
                CaseRow.case_id == case_id, CaseRow.tenant_id == self.tenant_id,
                CaseRow.status != CaseStatus.CLOSED.value,
            ).values(status=status.value, updated_at=utc_now().isoformat()))
            if result.rowcount != 1:
                raise CasePolicyError("closed case")
        return self.get(case_id)
