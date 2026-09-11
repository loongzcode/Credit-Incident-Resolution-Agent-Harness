"""Validate archived seals, never re-extract with today's extractor or re-evaluate outcome."""
from datetime import timedelta
from types import SimpleNamespace
from sqlalchemy import select
from credit_harness.cases.repository import hydrate, CallState
from credit_harness.cases.models import CaseStatus
from credit_harness.cases.tables import CaseCallRow
from credit_harness.context.budget import digest
from credit_harness.evaluation.models import EvaluationReport, CaseClosureRecord, EvaluationVerdict
from credit_harness.evaluation.tables import CaseClosureRow, EvaluationReportRow
from credit_harness.evaluation.evaluator import report_identity
from credit_harness.evidence.tables import EvidenceRow, EvidenceOriginRow
from credit_harness.evidence.models import Evidence, fingerprint, json_hash
from credit_harness.persistence.store import ObservationRow
from credit_harness.authorization.tables import EffectRow
from credit_harness.authorization.models import SideEffectLedger
from credit_harness.recovery.tables import EffectRecoveryStateRow, EffectRecoveryAttemptRow, ReadDispatchRecoveryRow
from .models import MemoryError


def row_data(row):
    return {c.key: getattr(row, c.key) for c in row.__table__.columns}


def read_closed_source(session, case_row):
    case = hydrate(case_row)
    if case.status != CaseStatus.CLOSED_VERIFIED:
        raise MemoryError("verified closure required")
    cr = session.get(CaseClosureRow, case.case_id)
    rr = session.get(EvaluationReportRow, cr.evaluation_run_id) if cr else None
    if cr is None or rr is None:
        raise MemoryError("closure binding incomplete")
    closure, report = CaseClosureRecord.model_validate(cr.payload), EvaluationReport.model_validate(rr.payload)
    snapshot = report.snapshot
    if (report.overall_verdict != EvaluationVerdict.PASS or report.outcome_path is None
            or closure.case_id != case.case_id or report.case_id != case.case_id or rr.case_id != case.case_id
            or snapshot.case_id != case.case_id or snapshot.tenant_id != case.tenant_id
            or cr.closure_id != closure.closure_id or cr.report_id != report.report_id
            or closure.report_id != report.report_id or rr.report_id != report.report_id
            or closure.evaluation_run_id != report.evaluation_run_id or rr.evaluation_run_id != report.evaluation_run_id
            or closure.verification_snapshot_id != report.verification_snapshot_id
            or snapshot.verification_snapshot_id != report.verification_snapshot_id
            or report_identity(report) != report.report_id
            or digest(snapshot.model_dump(mode="json", exclude={"verification_snapshot_id"})) != snapshot.verification_snapshot_id
            or digest(dict(case_id=case.case_id, report_id=report.report_id,
                           snapshot=report.verification_snapshot_id)) != closure.closure_id
            or closure.evidence_fingerprint != snapshot.evidence_fingerprint
            or closure.ledger_fingerprint != snapshot.side_effect_ledger_fingerprint
            or closure.evaluation_policy_version != report.policy_version
            or closure.contract_version != report.contract_version
            or report.policy_version != snapshot.evaluation_policy_version
            or report.contract_version != snapshot.verification_contract_version):
        raise MemoryError("closure binding invalid")
    # Closure changes exactly status and revision. Restore only those fields to
    # validate the archived Case identity; do not change any durable Case state.
    matched = False
    for status in CaseStatus:
        if status.is_terminal:
            continue
        payload = case.model_copy(update={"status": status, "updated_at": snapshot.case_revision}).model_dump(mode="json")
        payload["scope"] = dict(allowed_order_ids=sorted(case.scope.allowed_order_ids),
            allowed_tools=sorted(t.value for t in case.scope.allowed_tools))
        matched |= digest(payload) == snapshot.case_fingerprint
    if not matched or case.updated_at != max(closure.closed_at, snapshot.case_revision + timedelta(microseconds=1)):
        raise MemoryError("closed case changed")
    rows = tuple(session.scalars(select(EvidenceRow).where(EvidenceRow.case_id == case.case_id).order_by(EvidenceRow.evidence_id)))
    calls = tuple(session.scalars(select(CaseCallRow).where(CaseCallRow.case_id == case.case_id).order_by(CaseCallRow.sequence)))
    origins = {(r.evidence_id, r.call_id) for r in session.scalars(select(EvidenceOriginRow).join(CaseCallRow)
        .where(CaseCallRow.case_id == case.case_id))}
    receipts = []
    for call in calls:
        if call.state != CallState.OBSERVED.value:
            raise MemoryError("closed source contains incomplete call")
        r = session.get(ObservationRow, call.observation_id)
        if (r is None or r.content_hash != json_hash(r.observation) or r.simulation_id != case.simulation_id
                or r.grant_hash != case_row.grant_hash or r.tool != call.tool or r.request != call.request
                or not call.dispatch_correlation_id or r.dispatch_correlation_id != call.dispatch_correlation_id):
            raise MemoryError("archived receipt invalid")
        receipts.append((r.id, r.content_hash, json_hash(r.observation), r.grant_hash,
                         r.simulation_id, r.tool, r.request, r.dispatch_correlation_id))
    evidence_seal = json_hash(dict(evidence=[(r.evidence_id, r.observation_id, r.payload) for r in rows],
                                  origins=sorted(origins), receipts=receipts))
    call_data = tuple(dict(call_id=c.call_id, sequence=c.sequence, tool=c.tool, request=c.request,
        state=c.state, observation_id=c.observation_id, dispatch_correlation_id=c.dispatch_correlation_id) for c in calls)
    if evidence_seal != snapshot.evidence_fingerprint or digest(call_data) != snapshot.call_history_fingerprint:
        raise MemoryError("archived evidence or history seal invalid")
    evidence = tuple(Evidence.model_validate(r.payload) for r in rows)
    ids = {e.evidence_id for e in evidence}
    if (any(e.case_id != case.case_id or e.evidence_id != "E-" + fingerprint(e) for e in evidence)
            or not set(report.supporting_evidence_refs) <= ids):
        raise MemoryError("archived evidence references invalid")
    effects = tuple(session.scalars(select(EffectRow).where(EffectRow.case_id == case.case_id).order_by(EffectRow.effect_id)))
    if digest([row_data(r) for r in effects]) != snapshot.side_effect_ledger_fingerprint:
        raise MemoryError("archived ledger seal invalid")
    recovery = list(session.scalars(select(EffectRecoveryStateRow).where(EffectRecoveryStateRow.case_id == case.case_id)
        .order_by(EffectRecoveryStateRow.effect_id)))
    attempts = tuple(session.scalars(select(EffectRecoveryAttemptRow).where(EffectRecoveryAttemptRow.effect_id.in_(
        [e.effect_id for e in effects])).order_by(EffectRecoveryAttemptRow.recovery_id)))
    recovery.extend(attempts)
    recovery.extend(session.scalars(select(ReadDispatchRecoveryRow).where(ReadDispatchRecoveryRow.case_id == case.case_id)
        .order_by(ReadDispatchRecoveryRow.call_id)))
    if digest([row_data(r) for r in recovery]) != snapshot.recovery_fingerprint:
        raise MemoryError("archived recovery seal invalid")
    return SimpleNamespace(case=case, closure=closure, report=report, evidence=evidence,
        calls=calls, origins=origins, effects=tuple(SideEffectLedger.model_validate(r.payload) for r in effects), attempts=attempts)
