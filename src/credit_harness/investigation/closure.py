"""Read-side closure binding verification, independent of the Case status label."""
from datetime import datetime, timedelta
from credit_harness.context.budget import digest
from credit_harness.context.eligibility import ContextEligibilityError
from credit_harness.evaluation.models import CaseClosureRecord, EvaluationReport
from credit_harness.evaluation.evaluator import report_identity


def checked_closure(case, row, report_row, evidence_fingerprint, ledger_fingerprint, calls_fingerprint):
    record = CaseClosureRecord.model_validate(row["payload"])
    report = EvaluationReport.model_validate(report_row["payload"])
    expected_id = digest(dict(case_id=report.case_id, report_id=report.report_id,
        snapshot=report.verification_snapshot_id))
    expected_revision = max(record.closed_at, report.snapshot.case_revision + timedelta(microseconds=1))
    checks = (
        case.status.value == "CLOSED_VERIFIED", record.case_id == case.case_id == row["case_id"] == report.case_id,
        report.snapshot.tenant_id == case.tenant_id, report.snapshot.case_id == case.case_id,
        record.closure_id == row["closure_id"] == expected_id,
        record.evaluation_run_id == row["evaluation_run_id"] == report_row["evaluation_run_id"] == report.evaluation_run_id,
        record.report_id == row["report_id"] == report_row["report_id"] == report.report_id == report_identity(report),
        record.verification_snapshot_id == report.verification_snapshot_id == report.snapshot.verification_snapshot_id,
        record.evidence_fingerprint == report.snapshot.evidence_fingerprint == evidence_fingerprint,
        record.ledger_fingerprint == report.snapshot.side_effect_ledger_fingerprint == ledger_fingerprint,
        report.snapshot.call_history_fingerprint == calls_fingerprint,
        record.evaluation_policy_version == report.policy_version == report.snapshot.evaluation_policy_version,
        record.contract_version == report.contract_version == report.snapshot.verification_contract_version,
        case.updated_at == expected_revision, report.overall_verdict.value == "PASS",
    )
    if not all(checks):
        raise ContextEligibilityError("verified closure binding invalid")
    return record, report
