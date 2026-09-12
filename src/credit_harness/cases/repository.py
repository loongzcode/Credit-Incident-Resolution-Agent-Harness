from datetime import datetime, timedelta, timezone
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from credit_harness.persistence.store import GrantRow, SimulationRow, token_hash
from credit_harness.tools.contracts import ToolQuery
from credit_harness.domain.enums import ToolName
from .models import Case, CaseAccessError, CasePolicyError, CaseStatus
from .tables import CaseCallRow, CaseRow
from .preconditions import AgentExecutionPrecondition, AgentPreconditionFailed


class CallState(StrEnum):
    DISPATCHED = "DISPATCHED"
    OBSERVED = "OBSERVED"
    ERROR = "ERROR"


def utc_now():
    return datetime.now(timezone.utc)


def next_update_time(previous):
    # A revision represented by an ISO timestamp, monotonic even with a frozen clock.
    return max(utc_now(), previous + timedelta(microseconds=1)).isoformat()


def execution_conditions(case_id, tenant_id, precondition):
    if type(precondition) is not AgentExecutionPrecondition:
        raise AgentPreconditionFailed("typed execution precondition required")
    if precondition.case_id != case_id or precondition.tenant_id != tenant_id:
        raise AgentPreconditionFailed("execution precondition scope mismatch")
    return (
        CaseRow.status == precondition.expected_case_status.value,
        CaseRow.used_tool_calls == precondition.expected_used_tool_calls,
        CaseRow.updated_at == precondition.expected_case_updated_at.isoformat(),
    )


def hydrate(row: CaseRow) -> Case:
    return Case.model_validate({
        **row.payload, "status": row.status, "updated_at": row.updated_at,
        "budget": {"max_tool_calls": row.max_tool_calls, "used_tool_calls": row.used_tool_calls},
    })


class CaseRepository:
    """Trusted server repository. Tenant comes from deployment/auth, never query JSON."""

    def __init__(self, engine, tenant_id: str, *, registry_guard=None):
        self.engine, self.tenant_id = engine, tenant_id
        self.registry_guard = registry_guard

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
        if case.status != CaseStatus.NEW or case.budget.used_tool_calls != 0 or case.lookup_retry_after is not None:
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

    def reserve_call(self, case_id: str, tool: ToolName, query: ToolQuery, *,
                     precondition: AgentExecutionPrecondition | None = None) -> tuple[Case, str]:
        conditions = () if precondition is None else execution_conditions(case_id, self.tenant_id, precondition)
        with Session(self.engine) as session, session.begin():
            self._work_fence(session, case_id, precondition)
            case = hydrate(self._row(session, case_id))
            if query.internal_order_id not in case.scope.allowed_order_ids:
                raise CasePolicyError("order outside case scope")
            if tool not in case.scope.allowed_tools or tool.value in case.constraints.forbidden_actions:
                raise CasePolicyError("tool outside case scope")
            if tool != ToolName.PROTOCOL and (query.protocol_version or query.effective_at):
                raise CasePolicyError("protocol parameters require protocol tool")
            from credit_harness.registry.tables import RouteContextRow
            registered_route = session.get(RouteContextRow, case_id)
            if registered_route is not None and self.registry_guard is None:
                raise AgentPreconditionFailed("registry-bound Case requires registry dispatch guard")
            resolved = (self.registry_guard.prepare(session, case, tool, precondition)
                        if self.registry_guard is not None else None)
            # A single conditional UPDATE serializes concurrent budget reservations on
            # both SQLite and PostgreSQL. No DB transaction spans the HTTP call.
            result = session.execute(update(CaseRow).where(
                CaseRow.case_id == case_id, CaseRow.tenant_id == self.tenant_id,
                CaseRow.status.in_([CaseStatus.NEW.value, CaseStatus.INVESTIGATING.value]),
                CaseRow.used_tool_calls < CaseRow.max_tool_calls,
                *conditions,
            ).values(used_tool_calls=CaseRow.used_tool_calls + 1,
                     status=CaseStatus.INVESTIGATING.value, updated_at=next_update_time(case.updated_at))
                .returning(CaseRow.used_tool_calls).execution_options(synchronize_session=False))
            sequence = result.scalar_one_or_none()
            if sequence is None:
                if precondition is not None:
                    raise AgentPreconditionFailed("execution precondition changed; no dispatch reserved")
                raise CasePolicyError("case paused/closed or tool budget exhausted")
            call_id = str(uuid4())
            session.add(CaseCallRow(call_id=call_id, case_id=case_id, sequence=sequence,
                                    dispatch_correlation_id=call_id,
                                    tool=tool.value, request=query.model_dump(mode="json"),
                                    state=CallState.DISPATCHED.value))
            if resolved is not None:
                session.flush()
                self.registry_guard.record(session, call_id, case, resolved)
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
            row = self._row(session, case_id)
            result = session.execute(update(CaseRow).where(
                CaseRow.case_id == case_id, CaseRow.tenant_id == self.tenant_id,
                CaseRow.status.not_in([s.value for s in CaseStatus if s.is_terminal]),
            ).values(status=status.value, updated_at=next_update_time(datetime.fromisoformat(row.updated_at))))
            if result.rowcount != 1:
                raise CasePolicyError("closed case")
        return self.get(case_id)

    def pause_if_current(self, case_id: str, status: CaseStatus, *,
                         precondition: AgentExecutionPrecondition, orchestration=None) -> Case:
        if status not in (CaseStatus.WAITING, CaseStatus.ESCALATED):
            raise CasePolicyError("agent may only wait or escalate")
        conditions = execution_conditions(case_id, self.tenant_id, precondition)
        with Session(self.engine) as session, session.begin():
            self._work_fence(session, case_id, precondition)
            row = self._row(session, case_id)
            result = session.execute(update(CaseRow).where(
                CaseRow.case_id == case_id, CaseRow.tenant_id == self.tenant_id,
                CaseRow.status.in_([CaseStatus.NEW.value, CaseStatus.INVESTIGATING.value]),
                *conditions,
            ).values(status=status.value, updated_at=next_update_time(datetime.fromisoformat(row.updated_at))))
            if result.rowcount != 1:
                raise AgentPreconditionFailed("case changed; pause not committed")
            if orchestration is not None:
                from credit_harness.orchestration.handoff import pause_handoff
                session.refresh(row)
                pause_handoff(session, row, orchestration, utc_now())
        return self.get(case_id)

    def _work_fence(self, session, case_id, precondition):
        from credit_harness.orchestration.tables import WorkItemRow
        # The same Case lock is used by lease claims, so loss of ownership
        # cannot race a Tool reservation or lifecycle action.
        session.execute(update(CaseRow).where(CaseRow.case_id == case_id,
            CaseRow.tenant_id == self.tenant_id).values(updated_at=CaseRow.updated_at))
        active = session.scalar(select(WorkItemRow).where(WorkItemRow.case_id == case_id,
            WorkItemRow.tenant_id == self.tenant_id, WorkItemRow.status == "CLAIMED"))
        fence = precondition.work_lease if precondition else None
        if active is None and fence is None:
            return
        if (active is None or fence is None or active.work_item_id != fence.work_item_id
                or active.payload["claimed_by"] != fence.worker_id
                or active.payload["lease_token"] != fence.lease_token
                or active.lease_until is None or active.lease_until <= utc_now().timestamp()):
            raise AgentPreconditionFailed("work lease lost; no dispatch reserved")
