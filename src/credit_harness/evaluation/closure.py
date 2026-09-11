from datetime import timedelta
from sqlalchemy import update
from sqlalchemy.orm import Session
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.tables import CaseRow
from credit_harness.context.budget import digest
from .models import (EvaluationReport, EvaluationVerdict as V, ClosureError, ClosureCode as C,
                     CaseClosureRecord, VerifiedClosureResult, ClosureStatus)
from .tables import EvaluationReportRow, CaseClosureRow
from .repository import lock_case
from .evaluator import report_identity
from .snapshot import UNRESOLVED_EFFECT_STATES


class VerifiedClosureService:
    """Sole owner of the verified closure transition. No effect dispatch.

    Shared lock order: Case -> reads of calls/effects/recovery -> report/closure.
    Producers also lock Case, so no publication can interleave with the final
    verification and closure commit. Unique CaseClosureRow is the second fence.
    """
    def __init__(self, evaluator):
        self.evaluator, self.cases = evaluator, evaluator.cases

    def close(self, report):
        if type(report) is not EvaluationReport:
            raise ClosureError(C.REPORT_INVALID)
        report = EvaluationReport.model_validate(report.model_dump())
        if report.overall_verdict != V.PASS:
            raise ClosureError(C.PASS_REQUIRED)
        if report_identity(report) != report.report_id:
            raise ClosureError(C.REPORT_INVALID)
        with Session(self.cases.engine) as session, session.begin():
            row = lock_case(session, self.cases, report.case_id)
            saved = session.get(EvaluationReportRow, report.evaluation_run_id)
            if saved is None or saved.payload != report.model_dump(mode="json"):
                raise ClosureError(C.REPORT_NOT_PERSISTED)
            old = session.get(CaseClosureRow, report.case_id)
            if old:
                record = CaseClosureRecord.model_validate(old.payload)
                if row.status != CaseStatus.CLOSED_VERIFIED.value or record.report_id != report.report_id:
                    raise ClosureError(C.STALE_EVALUATION)
                current = self.evaluator.reader.read(session, report.case_id, self.evaluator.clock())
                if (current.pending_reads or current.recovery_unresolved or not current.evidence.provenance_valid
                        or any(e.ledger.status in UNRESOLVED_EFFECT_STATES for e in current.effects)
                        or current.snapshot.evidence_fingerprint != report.snapshot.evidence_fingerprint
                        or current.snapshot.side_effect_ledger_fingerprint != report.snapshot.side_effect_ledger_fingerprint
                        or current.snapshot.call_history_fingerprint != report.snapshot.call_history_fingerprint):
                    raise ClosureError(C.CLOSURE_INVARIANT_VIOLATION)
                return VerifiedClosureResult(status=ClosureStatus.ALREADY_CLOSED_VERIFIED, record=record)
            if CaseStatus(row.status).is_terminal:
                raise ClosureError(C.CLOSURE_INVARIANT_VIOLATION)
            now = self.evaluator.clock()
            fresh = self.evaluator._evaluate(session, report.case_id, now)
            # Compare semantic report as well as snapshot: persisting a forged
            # PASS with a recomputed hash never grants closure authority.
            if (fresh.overall_verdict != V.PASS or fresh.snapshot != report.snapshot
                    or fresh.report_id != report.report_id):
                raise ClosureError(C.STALE_EVALUATION)
            record = CaseClosureRecord(closure_id=digest(dict(case_id=report.case_id,
                report_id=report.report_id, snapshot=report.verification_snapshot_id)),
                case_id=report.case_id, evaluation_run_id=report.evaluation_run_id, report_id=report.report_id,
                verification_snapshot_id=report.verification_snapshot_id,
                evidence_fingerprint=report.snapshot.evidence_fingerprint,
                ledger_fingerprint=report.snapshot.side_effect_ledger_fingerprint,
                evaluation_policy_version=report.policy_version, contract_version=report.contract_version,
                closed_at=now)
            updated = max(now, report.snapshot.case_revision + timedelta(microseconds=1))
            result = session.execute(update(CaseRow).where(CaseRow.case_id == report.case_id,
                CaseRow.tenant_id == self.cases.tenant_id,
                CaseRow.updated_at == report.snapshot.case_revision.isoformat(),
                CaseRow.status == row.status).values(status=CaseStatus.CLOSED_VERIFIED.value,
                    updated_at=updated.isoformat()))
            if result.rowcount != 1:
                raise ClosureError(C.STALE_EVALUATION)
            session.add(CaseClosureRow(case_id=report.case_id, closure_id=record.closure_id,
                evaluation_run_id=report.evaluation_run_id, report_id=report.report_id,
                payload=record.model_dump(mode="json")))
            return VerifiedClosureResult(status=ClosureStatus.CLOSED_VERIFIED, record=record)
