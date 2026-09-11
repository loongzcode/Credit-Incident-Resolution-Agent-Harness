from uuid import uuid4
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from pydantic import ValidationError
from credit_harness.cases.models import CaseAccessError, CaseStatus, CasePolicyError
from credit_harness.cases.repository import CallState, utc_now
from credit_harness.cases.tables import CaseRow, CaseCallRow
from credit_harness.persistence.store import ObservationRow
from credit_harness.evidence.repository import ProvenanceError, raw_observation
from .models import DispatchRecoveryStatus as S, RecoveryResult, RecoverySubject
from .tables import ReadDispatchRecoveryRow


class ReadObservationRecoveryService:
    """Only durable provenance + extraction. There is no read client here."""
    def __init__(self, evidence, *, clock=utc_now):
        self.evidence, self.cases, self.engine, self.clock = evidence, evidence.cases, evidence.engine, clock

    def pending_calls(self, case_id):
        with Session(self.engine) as session:
            self.cases._row(session, case_id)
            return tuple(session.scalars(select(CaseCallRow.call_id).where(CaseCallRow.case_id == case_id,
                CaseCallRow.state.in_([CallState.DISPATCHED.value, CallState.ERROR.value]))
                .order_by(CaseCallRow.sequence)))

    def recover(self, case_id, call_id):
        with Session(self.engine) as session, session.begin():
            result = session.execute(update(CaseRow).where(CaseRow.case_id == case_id,
                CaseRow.tenant_id == self.cases.tenant_id).values(updated_at=CaseRow.updated_at))
            if result.rowcount != 1:
                raise CaseAccessError("case unavailable")
            call = session.get(CaseCallRow, call_id)
            if call is None or call.case_id != case_id:
                raise CaseAccessError("call unavailable")
            case = self.cases._row(session, case_id)
            if CaseStatus(case.status).is_terminal:
                raise CasePolicyError("terminal Case recovery violates closure invariant")
            previous = session.get(ReadDispatchRecoveryRow, call_id)
            before = S(previous.status) if previous else None
            refs, observation_id = (), None
            status = S.PROVENANCE_INVALID
            if call.dispatch_correlation_id:
                calls = session.scalars(select(CaseCallRow.call_id).where(
                    CaseCallRow.dispatch_correlation_id == call.dispatch_correlation_id)).all()
                observations = session.scalars(select(ObservationRow).where(
                    ObservationRow.dispatch_correlation_id == call.dispatch_correlation_id).limit(2)).all()
                if len(calls) != 1 or len(observations) > 1:
                    status = S.AMBIGUOUS_OBSERVATION
                elif not observations:
                    status = S.OBSERVATION_NOT_FOUND
                else:
                    try:
                        # Shared original publication validation, including grant,
                        # simulation, order, request, hash and single call binding.
                        with session.begin_nested():
                            was_published = call.state == CallState.OBSERVED.value
                            raw = raw_observation(observations[0])
                            refs = self.evidence._record_call(session, case_id, call_id, raw.observation, recovery=True)
                            observation_id = raw.observation_id
                            status = S.OBSERVATION_ALREADY_PUBLISHED if was_published else S.OBSERVATION_RECOVERED
                    except (ProvenanceError, ValidationError, ValueError):
                        status, refs, observation_id = S.PROVENANCE_INVALID, (), None
            fields = dict(case_id=case_id, dispatch_correlation_id=call.dispatch_correlation_id,
                status=status.value, recovered_observation_id=observation_id,
                recovered_evidence_refs=list(refs), attempted_at=self.clock().timestamp())
            if previous is None:
                session.add(ReadDispatchRecoveryRow(call_id=call_id, **fields))
            else:
                for key, value in fields.items():
                    setattr(previous, key, value)
            return RecoveryResult(recovery_id=str(uuid4()), subject_type=RecoverySubject.READ_DISPATCH,
                subject_id=call_id, before_status=before, after_status=status, action_taken=status,
                recovered_evidence_refs=tuple(sorted(refs)), requires_escalation=status in (
                    S.PROVENANCE_INVALID, S.AMBIGUOUS_OBSERVATION))
