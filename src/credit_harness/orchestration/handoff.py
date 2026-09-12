"""Transactional handoffs. These functions publish work, never Evidence."""
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.cases.repository import CaseRepository
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.tables import CaseRow
from credit_harness.authorization.models import EffectStatus
from credit_harness.remediation.models import RemediationActionType as A
from credit_harness.evaluation.models import (EvaluationReport, EvaluationVerdict as V,
    VerificationRequirement as Q, ClosureError, ClosureCode)
from credit_harness.evaluation.tables import EvaluationReportRow
from credit_harness.evaluation.evaluator import report_identity
from .models import WorkType as T, WorkReason as R, Trigger, SignalType, OrchestrationError, PauseReservation
from .repository import create_work, lock_case, state_in
from .tables import EvaluationHandoffRow


def pause_handoff(session, case, reservation, now):
    if type(reservation) is not PauseReservation:
        raise OrchestrationError("typed pause reservation required")
    reservation = PauseReservation.model_validate(reservation.model_dump())
    waiting = case.status == CaseStatus.WAITING.value
    if waiting != (reservation.wait_seconds is not None):
        raise OrchestrationError("pause duration/status mismatch")
    _, state = state_in(session, case)
    if waiting and state.resume_cycle_count >= state.budget.max_resume_cycles:
        case.status = CaseStatus.ESCALATED.value
        return create_work(session, case, work_type=T.OPERATOR_FOLLOWUP, reason=R.RESUME_LIMIT,
            source_ref=reservation.decision_id, snapshot_ref=reservation.snapshot_id,
            previous_run_id=reservation.run_id, trigger=Trigger.TIMER, now=now,
            required_signal=SignalType.OPERATOR_ACKNOWLEDGED)
    return create_work(session, case,
        work_type=T.INVESTIGATION_RESUME if waiting else T.OPERATOR_FOLLOWUP,
        reason=R.WAIT_REQUESTED if waiting else reservation.reason,
        source_ref=reservation.decision_id, snapshot_ref=reservation.snapshot_id,
        previous_run_id=reservation.run_id, wait_seconds=reservation.wait_seconds,
        trigger=Trigger.TIMER if waiting else Trigger.RESOLUTION_SIGNAL, now=now,
        not_before=now+timedelta(seconds=reservation.wait_seconds or 0),
        required_signal=None if waiting else reservation.required_signal or SignalType.OPERATOR_ACKNOWLEDGED)


def effect_handoff(session, ledger):
    # Both normal execution and recovered transitions call this inside the
    # Ledger transaction, through their existing synchronize_effect path.
    if ledger.status not in (EffectStatus.APPLIED, EffectStatus.FAILED_CONFIRMED,
                             EffectStatus.UNKNOWN, EffectStatus.DISPATCHED):
        return None
    case = session.get(CaseRow, ledger.case_id)
    if ledger.status in (EffectStatus.UNKNOWN, EffectStatus.DISPATCHED):
        return create_work(session, case, work_type=T.RECOVERY_RECHECK, reason=R.EFFECT_UNRESOLVED,
            source_ref=ledger.effect_id, trigger=Trigger.RECOVERY, now=ledger.updated_at,
            not_before=ledger.updated_at+timedelta(seconds=30))
    requirement = {A.REPLAY_CALLBACK_CONSUMPTION: Q.POST_EFFECT_MESSAGE_STATUS,
                   A.REDELIVER_ASSET_NOTIFICATION: Q.POST_EFFECT_DELIVERY_STATUS}.get(ledger.action_type)
    if ledger.status == EffectStatus.APPLIED and requirement:
        return create_work(session, case, work_type=T.VERIFICATION_REQUIRED, reason=R.EFFECT_APPLIED,
            source_ref=ledger.effect_id, trigger=Trigger.SIDE_EFFECT, now=ledger.updated_at, requirement=requirement)
    return create_work(session, case, work_type=T.OPERATOR_FOLLOWUP,
        reason=R.EFFECT_FAILED if ledger.status == EffectStatus.FAILED_CONFIRMED else R.OPERATOR_REQUIRED,
        source_ref=ledger.effect_id, trigger=Trigger.SIDE_EFFECT, now=ledger.updated_at,
        required_signal=SignalType.OPERATOR_ACKNOWLEDGED)


class EvaluationHandoffService:
    def __init__(self, repository, closure):
        self.repository, self.closure = repository, closure
        if closure.cases is not repository.cases:
            raise ValueError("handoff must share Case boundary")

    def consume(self, report):
        result = self._consume(report)
        self._ack(report, "COMPLETED")
        return result

    def _ack(self, report, status):
        cases = self.repository.cases
        with Session(cases.engine) as session, session.begin():
            row = session.scalar(select(EvaluationHandoffRow).where(
                EvaluationHandoffRow.evaluation_run_id == report.evaluation_run_id,
                EvaluationHandoffRow.tenant_id == cases.tenant_id))
            if row:
                row.status = status

    def _consume(self, report):
        if type(report) is not EvaluationReport or report_identity(report) != report.report_id:
            raise OrchestrationError("invalid report")
        cases = self.repository.cases
        if report.overall_verdict == V.PASS:
            self.closure.close(report)  # Sole existing closure authority revalidates fresh state.
            return ()
        with Session(cases.engine) as session, session.begin():
            case = lock_case(session, cases, report.case_id)
            saved = session.get(EvaluationReportRow, report.evaluation_run_id)
            if (not saved or saved.payload != report.model_dump(mode="json")
                    or report.snapshot.tenant_id != cases.tenant_id
                    or case.updated_at != report.snapshot.case_revision.isoformat()):
                raise OrchestrationError("unpersisted or stale report")
            if CaseStatus(case.status).is_terminal:
                return ()
            if report.overall_verdict == V.FAIL:
                return (create_work(session, case, work_type=T.OPERATOR_FOLLOWUP, reason=R.EVALUATION_FAILED,
                    source_ref=report.report_id, trigger=Trigger.EVALUATION, now=report.created_at,
                    required_signal=SignalType.OPERATOR_ACKNOWLEDGED),)
            return tuple(create_work(session, case, work_type=T.VERIFICATION_REQUIRED,
                reason=R.EVALUATION_INCONCLUSIVE, source_ref=report.report_id, trigger=Trigger.EVALUATION,
                now=report.created_at, not_before=report.created_at+timedelta(seconds=30), requirement=q,
                verification_requirements=tuple(sorted({r.requirement for r in report.unresolved_requirements})))
                for q in sorted({r.requirement for r in report.unresolved_requirements}))

    def poll_unhanded_reports(self, *, limit=100):
        # Report persistence and this bounded outbox are atomic. Work keys
        # tolerate interruption between work commit and acknowledgement.
        if not 1 <= limit <= 1000:
            raise ValueError("bounded report scan required")
        cases = self.repository.cases
        with Session(cases.engine) as session:
            rows = session.scalars(select(EvaluationReportRow).join(EvaluationHandoffRow,
                EvaluationHandoffRow.evaluation_run_id == EvaluationReportRow.evaluation_run_id)
                .where(EvaluationHandoffRow.tenant_id == cases.tenant_id, EvaluationHandoffRow.status == "PENDING")
                .order_by(EvaluationHandoffRow.sequence).limit(limit))
            reports = [EvaluationReport.model_validate(r.payload) for r in rows]
        results = []
        for report in reports:
            try:
                results.extend(self.consume(report))
            except OrchestrationError:
                self._ack(report, "CANCELED")  # Stale reports cannot starve newer reports.
            except ClosureError as exc:
                if exc.code != ClosureCode.STALE_EVALUATION:
                    raise
                self._ack(report, "CANCELED")
        return tuple(results)
