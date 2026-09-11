from sqlalchemy import select, update
from sqlalchemy.orm import Session
from credit_harness.cases.tables import CaseRow
from credit_harness.cases.models import CaseAccessError, CaseStatus
from .models import EvaluationReport, CaseClosureRecord, ClosureError, ClosureCode as C
from .tables import EvaluationReportRow, CaseClosureRow
from .evaluator import report_identity


def lock_case(session, cases, case_id):
    result = session.execute(update(CaseRow).where(CaseRow.case_id == case_id,
        CaseRow.tenant_id == cases.tenant_id).values(updated_at=CaseRow.updated_at))
    if result.rowcount != 1:
        raise CaseAccessError("case unavailable")
    row = cases._row(session, case_id)
    session.refresh(row)
    return row


class EvaluationRepository:
    """Append-only audit storage, never authority to assert business truth."""
    def __init__(self, cases):
        self.cases, self.engine = cases, cases.engine

    def record(self, report):
        if type(report) is not EvaluationReport:
            raise ClosureError(C.REPORT_INVALID)
        report = EvaluationReport.model_validate(report.model_dump())
        if (report_identity(report) != report.report_id or report.case_id != report.snapshot.case_id
                or report.snapshot.tenant_id != self.cases.tenant_id
                or report.verification_snapshot_id != report.snapshot.verification_snapshot_id):
            raise ClosureError(C.REPORT_INVALID)
        with Session(self.engine) as session, session.begin():
            row = lock_case(session, self.cases, report.case_id)
            old = session.get(EvaluationReportRow, report.evaluation_run_id)
            if old is not None:
                if old.payload != report.model_dump(mode="json"):
                    raise ClosureError(C.REPORT_INVALID)
                return report
            if CaseStatus(row.status).is_terminal:
                raise ClosureError(C.STALE_EVALUATION)
            session.add(EvaluationReportRow(evaluation_run_id=report.evaluation_run_id,
                report_id=report.report_id, case_id=report.case_id, payload=report.model_dump(mode="json")))
        return report

    def reports(self, case_id):
        with Session(self.engine) as session:
            self.cases._row(session, case_id)
            return tuple(EvaluationReport.model_validate(r.payload) for r in session.scalars(
                select(EvaluationReportRow).where(EvaluationReportRow.case_id == case_id)
                    .order_by(EvaluationReportRow.evaluation_run_id)))

    def closure(self, case_id):
        with Session(self.engine) as session:
            self.cases._row(session, case_id)
            row = session.get(CaseClosureRow, case_id)
            return CaseClosureRecord.model_validate(row.payload) if row else None
