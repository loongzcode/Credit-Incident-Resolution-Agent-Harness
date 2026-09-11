import ast
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
import pytest
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session
from tests.support.evaluation_fixture import EvaluationFixture, CASE
from credit_harness.cases.models import CaseStatus, CasePolicyError, CaseAccessError
from credit_harness.cases.repository import CaseRepository, CallState
from credit_harness.cases.tables import CaseRow, CaseCallRow
from credit_harness.domain.enums import ToolName as T, ScenarioId, LoanStatus, ConsumeStatus, DeliveryStatus, Currency
from credit_harness.domain.models import AssetDelivery
from credit_harness.evidence.models import ClaimType as C
from credit_harness.evidence.tables import EvidenceRow
from credit_harness.tools.contracts import ToolQuery
from credit_harness.authorization.models import EffectStatus as E, AuthorizationError
from credit_harness.authorization.tables import EffectRow
from credit_harness.remediation.models import RemediationActionType as A
from credit_harness.evaluation.models import (EvaluationVerdict as V, EvaluationDimension as D, DimensionStatus as S,
    EvaluationReason as R, OutcomePath as P, ClosureError, ClosureCode, ClosureStatus)
from credit_harness.evaluation.contract import EvaluationPolicy
from credit_harness.evaluation.evaluator import IndependentEvaluator, report_identity
from credit_harness.evaluation.repository import EvaluationRepository
from credit_harness.evaluation.closure import VerifiedClosureService
from credit_harness.evaluation.tables import EvaluationReportRow, CaseClosureRow


@pytest.fixture
def ev(engine, monkeypatch):
    monkeypatch.setenv("CAPABILITY_SIGNING_SECRET", "synthetic-evaluator-test-key-not-production-0001")
    instances = []
    def make(*, converged=False, no_disbursement=False, scenario=ScenarioId.S6):
        x = EvaluationFixture(engine, scenario=scenario)
        instances.append(x)
        if converged or no_disbursement:
            x.progress(no_disbursement=no_disbursement)
        x.read()
        return x
    yield make
    for x in reversed(instances):
        x.close()


def dimension(report, name):
    return next(d for d in report.dimensions if d.dimension == name)


def assert_verdict(report, verdict):
    assert report.overall_verdict == verdict, report.model_dump(mode="json")


def test_settled_converged_case_passes(ev):
    x = ev(converged=True)
    report = x.evaluate()
    assert_verdict(report, V.PASS)
    assert report.outcome_path == P.SETTLED_PATH


def test_no_disbursement_case_passes(ev):
    x = ev(no_disbursement=True, scenario=ScenarioId.S3)
    report = x.evaluate()
    assert_verdict(report, V.PASS)
    assert report.outcome_path == P.NO_DISBURSEMENT_PATH
    assert dimension(report, D.IDENTITY).status == S.NOT_APPLICABLE
    assert x.closure.close(report).status == ClosureStatus.CLOSED_VERIFIED


def test_applied_effect_without_post_read_is_inconclusive(ev):
    x = ev()
    x.remediate()
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert R.POST_EFFECT_EVIDENCE_MISSING in report.reason_codes


def test_s6_partial_repair_not_verified(ev):
    x = ev()
    x.remediate()
    x.read(T.MESSAGES)
    report = x.evaluate()
    assert dimension(report, D.SIDE_EFFECT).status == S.PASS
    assert report.overall_verdict != V.PASS
    assert not x.cases.get(CASE).status.is_terminal


def test_s6_fully_converged_after_replay(ev):
    x = ev()
    x.remediate()
    x.progress()
    x.read()
    report = x.evaluate()
    assert_verdict(report, V.PASS)
    assert x.closure.close(report).status == ClosureStatus.CLOSED_VERIFIED


def test_unknown_effect_is_inconclusive(ev):
    x = ev()
    x.remediate(timeout=True)
    x.progress()
    x.read()
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert R.UNRESOLVED_SIDE_EFFECT in report.reason_codes


def test_payment_identity_mismatch_fails(ev):
    x = ev(converged=True)
    x.progress(transaction_updates={"beneficiary_ref": "BEN-OTHER"})
    x.read()
    report = x.evaluate()
    assert_verdict(report, V.FAIL)
    assert R.PAYMENT_IDENTITY_MISMATCH in report.reason_codes


def test_payment_identity_unknown_is_inconclusive(ev):
    x = ev(converged=True)
    x.progress(transaction_updates={"account_ref": None})
    x.read()
    assert_verdict(x.evaluate(), V.INCONCLUSIVE)


def test_only_pass_report_can_close(ev):
    x = ev(converged=True)
    report = x.evaluate()
    result = x.closure.close(report)
    assert result.record.report_id == report.report_id
    assert result.record.verification_snapshot_id == report.verification_snapshot_id
    assert x.cases.get(CASE).status == CaseStatus.CLOSED_VERIFIED


@pytest.mark.parametrize("failed", [True, False], ids=["fail", "inconclusive"])
def test_non_pass_report_cannot_close(ev, failed):
    x = ev(converged=True)
    x.progress(transaction_updates={"account_ref": "ACC-OTHER" if failed else None})
    x.read()
    report = x.evaluate()
    assert_verdict(report, V.FAIL if failed else V.INCONCLUSIVE)
    with pytest.raises(ClosureError) as error:
        x.closure.close(report)
    assert error.value.code == ClosureCode.PASS_REQUIRED


def test_new_evidence_between_evaluate_and_close_blocks_closure(ev):
    x = ev(converged=True)
    report = x.evaluate()
    x.read(T.PAYMENT)
    with pytest.raises(ClosureError) as error:
        x.closure.close(report)
    assert error.value.code == ClosureCode.STALE_EVALUATION
    assert not x.cases.get(CASE).status.is_terminal


def test_ledger_mutation_between_evaluate_and_close_blocks_closure(ev):
    x = ev()
    x.remediate()
    x.progress()
    x.read()
    report = x.evaluate()
    with Session(x.engine) as session, session.begin():
        row = session.get(EffectRow, x.effect.effect_id)
        ledger = {**row.payload, "status": "UNKNOWN"}
        row.status, row.payload = "UNKNOWN", ledger
    with pytest.raises(ClosureError) as error:
        x.closure.close(report)
    assert error.value.code == ClosureCode.STALE_EVALUATION


def test_pending_read_blocks_closure(ev):
    x = ev(converged=True)
    report = x.evaluate()
    x.cases.reserve_call(CASE, T.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))
    newer = x.evaluate()
    assert_verdict(newer, V.INCONCLUSIVE)
    assert R.PENDING_READ_DISPATCH in newer.reason_codes
    with pytest.raises(ClosureError):
        x.closure.close(report)


def test_error_orphan_blocks_closure_until_publication(ev):
    x = ev(converged=True)
    original = x.client.observe
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("synthetic")
    x.client.observe = lost
    with pytest.raises(TimeoutError):
        x.read(T.PAYMENT)
    assert R.PENDING_READ_DISPATCH in x.evaluate().reason_codes
    from credit_harness.recovery.read import ReadObservationRecoveryService
    recovery = ReadObservationRecoveryService(x.evidence)
    for call in recovery.pending_calls(CASE):
        recovery.recover(CASE, call)
    assert_verdict(x.evaluate(), V.PASS)


def test_closed_verified_binds_report(ev):
    x = ev(converged=True)
    report = x.evaluate()
    closed = x.closure.close(report)
    assert x.repository.closure(CASE) == closed.record
    assert closed.record.evidence_fingerprint == report.snapshot.evidence_fingerprint
    assert closed.record.ledger_fingerprint == report.snapshot.side_effect_ledger_fingerprint


def test_repeated_close_is_idempotent(ev):
    x = ev(converged=True)
    report = x.evaluate()
    first = x.closure.close(report)
    revision = x.cases.get(CASE).updated_at
    second = x.closure.close(report)
    assert first.record == second.record
    assert second.status == ClosureStatus.ALREADY_CLOSED_VERIFIED
    assert x.cases.get(CASE).updated_at == revision


def test_two_workers_only_one_closure_wins(ev):
    x = ev(converged=True)
    reports = (x.evaluate(), x.evaluate())
    assert reports[0].report_id == reports[1].report_id
    barrier = Barrier(2)
    def close(report):
        evaluator = IndependentEvaluator(CaseRepository(x.engine, "demo"), clock=lambda: x.clock.now)
        barrier.wait()
        return VerifiedClosureService(evaluator).close(report)
    with ThreadPoolExecutor(2) as pool:
        results = tuple(pool.map(close, reports))
    assert {r.status for r in results} == {ClosureStatus.CLOSED_VERIFIED, ClosureStatus.ALREADY_CLOSED_VERIFIED}
    assert results[0].record == results[1].record
    with Session(x.engine) as session:
        assert session.scalar(select(func.count()).select_from(CaseClosureRow)) == 1


def test_report_is_deterministic_but_runs_are_distinct(ev):
    x = ev(converged=True)
    first = x.evaluate()
    x.clock.now += timedelta(seconds=1)
    second = x.evaluate()
    assert first.report_id == second.report_id
    assert first.evaluation_run_id != second.evaluation_run_id
    assert first.dimensions == second.dimensions
    assert len(x.repository.reports(CASE)) == 2


def test_evaluate_is_read_only(ev):
    x = ev(converged=True)
    before = x.cases.get(CASE), x.evidence.list(CASE)
    x.evaluator.evaluate(CASE)
    assert (x.cases.get(CASE), x.evidence.list(CASE)) == before
    assert not x.repository.reports(CASE)


def test_report_store_is_append_only(ev):
    x = ev(converged=True)
    report = x.evaluate()
    changed = report.model_copy(update={"overall_verdict": V.FAIL})
    changed = changed.model_copy(update={"report_id": report_identity(changed)})
    with pytest.raises(ClosureError):
        x.repository.record(changed)
    assert x.repository.reports(CASE) == (report,)


def test_forged_pass_report_cannot_close(ev):
    x = ev()
    report = x.evaluator.evaluate(CASE)
    forged = report.model_copy(update={"overall_verdict": V.PASS})
    forged = forged.model_copy(update={"report_id": report_identity(forged)})
    x.repository.record(forged)
    with pytest.raises(ClosureError):
        x.closure.close(forged)


@pytest.mark.parametrize("field", ["policy", "contract"])
def test_policy_or_contract_change_invalidates_report(ev, field):
    x = ev(converged=True)
    report = x.evaluate()
    old = getattr(x.evaluator, field)
    kwargs = {field: old.model_copy(update={"version": "changed"})}
    newer = IndependentEvaluator(x.cases, clock=lambda: x.clock.now, **kwargs)
    with pytest.raises(ClosureError):
        VerifiedClosureService(newer).close(report)


def test_stale_evidence_does_not_verify_current_state(ev):
    x = ev(converged=True)
    report = x.evaluate()
    x.advance(301)
    stale = x.evaluate()
    assert_verdict(stale, V.INCONCLUSIVE)
    assert R.EVIDENCE_STALE in stale.reason_codes
    with pytest.raises(ClosureError):
        x.closure.close(report)


def test_accounting_missing_false_fails_after_convergence_window(ev):
    x = ev(converged=True)
    x.progress(changes=lambda w, now: {"accounting": w.accounting.model_copy(update={"actual_entry": None, "event_time": now})})
    x.advance(31)
    x.read(T.ACCOUNTING)
    report = x.evaluate()
    assert_verdict(report, V.FAIL)
    assert R.ACCOUNTING_NOT_CONVERGED in report.reason_codes


def test_missing_authoritative_evidence_is_inconclusive(ev, monkeypatch):
    x = ev(converged=True)
    from credit_harness.simulator.faults import ObservationFault
    from credit_harness.domain.enums import FaultKind
    x.admin.add_fault(x.sid, ObservationFault(tool=T.ACCOUNTING, kind=FaultKind.TIMEOUT, starts_at=x.clock.now))
    x.read(T.ACCOUNTING)
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert R.ACCOUNTING_NOT_CONVERGED in report.reason_codes


def test_open_safety_gap_prevents_pass(ev):
    x = ev(converged=True)
    # Application protocol is not provided by the currently visible registry.
    x.progress(changes=lambda w, now: {"guarantee": w.guarantee.model_copy(update={"protocol_version": "9.9", "event_time": now})})
    x.read(T.GUARANTEE)
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert R.OPEN_SAFETY_CRITICAL_GAP in report.reason_codes


def test_replay_pre_effect_consumed_evidence_does_not_count(ev):
    x = ev()
    x.remediate()
    x.read(T.MESSAGES)
    with Session(x.engine) as session, session.begin():
        row = session.get(EffectRow, x.effect.effect_id)
        row.payload = {**row.payload, "updated_at": (x.clock.now + timedelta(seconds=1)).isoformat()}
    assert R.POST_EFFECT_EVIDENCE_MISSING in x.evaluate().reason_codes


def test_replay_wrong_message_evidence_does_not_count(ev):
    x = ev()
    x.remediate()
    x.progress(changes=lambda w, now: {"messages": tuple(m.model_copy(update={"message_id": "MSG-OTHER", "event_time": now}) for m in w.messages)})
    x.read()
    report = x.evaluate()
    assert dimension(report, D.SIDE_EFFECT).status == S.INCONCLUSIVE
    assert R.POST_EFFECT_EVIDENCE_MISSING in report.reason_codes


def test_replay_applied_requires_consumed_evidence(ev):
    x = ev()
    x.remediate()
    x.progress(changes=lambda w, now: {"messages": tuple(m.model_copy(update={"consume_status": ConsumeStatus.FAILED, "event_time": now}) for m in w.messages)})
    x.read(T.MESSAGES)
    report = x.evaluate()
    assert dimension(report, D.SIDE_EFFECT).status == S.FAIL
    assert R.CALLBACK_NOT_CONSUMED in report.reason_codes


def test_redelivery_applied_requires_delivered_evidence(ev):
    x = ev(scenario=ScenarioId.S7)
    x.remediate(A.REDELIVER_ASSET_NOTIFICATION)
    assert R.POST_EFFECT_EVIDENCE_MISSING in x.evaluate().reason_codes
    x.read(T.ASSET_DELIVERY)
    assert dimension(x.evaluate(), D.SIDE_EFFECT).status == S.PASS


def test_admin_task_applied_does_not_resolve_business_case(ev):
    x = ev()
    x.remediate(A.CREATE_RECONCILIATION_TASK)
    report = x.evaluate()
    assert report.overall_verdict != V.PASS
    assert dimension(report, D.SIDE_EFFECT).status == S.NOT_APPLICABLE


def test_policy_violation_fails_even_if_business_state_converged(ev, monkeypatch):
    x = ev()
    x.remediate()
    x.progress()
    x.read()
    from credit_harness.remediation.models import ActionRiskLevel
    catalog = x.evaluator.reader.catalog
    monkeypatch.setattr(catalog, "entries", tuple(e.model_copy(update={"risk_level": ActionRiskLevel.L3_MONEY_MOVEMENT})
        if e.action_type == A.REPLAY_CALLBACK_CONSUMPTION else e for e in catalog.entries))
    report = x.evaluate()
    assert_verdict(report, V.FAIL)
    assert R.FORBIDDEN_EFFECT_OBSERVED in report.reason_codes


def test_unknown_catalog_action_fails_closed(ev):
    x = ev()
    x.remediate()
    x.progress()
    x.read()
    with Session(x.engine) as session, session.begin():
        row = session.get(EffectRow, x.effect.effect_id)
        row.payload = {**row.payload, "action_type": "NEW_MONEY_ACTION"}
    assert_verdict(x.evaluate(), V.FAIL)


def test_tampered_evidence_cannot_be_verification_truth(ev):
    x = ev(converged=True)
    with Session(x.engine) as session, session.begin():
        row = next(r for r in session.scalars(select(EvidenceRow)) if r.payload["claim_type"] == C.PAYMENT_AMOUNT.value)
        row.payload = {**row.payload, "value": 1}
    report = x.evaluate()
    assert report.overall_verdict != V.PASS
    assert R.PROVENANCE_INVALID in report.reason_codes


def test_closed_case_blocks_tools_pause_capability_and_effect(ev):
    x = ev()
    x.remediate()
    x.progress()
    x.read()
    x.closure.close(x.evaluate())
    with pytest.raises(CasePolicyError):
        x.read(T.PAYMENT)
    with pytest.raises(CasePolicyError):
        x.cases.pause(CASE, CaseStatus.INVESTIGATING)
    with pytest.raises(CasePolicyError):
        x.cases.pause(CASE, CaseStatus.ESCALATED)
    with pytest.raises(AuthorizationError):
        x.auth.issue_capability(x.runtime.store.get_intent(x.effect.intent_id))
    with pytest.raises(AuthorizationError):
        x.runtime.store.prepare(x.capability.payload, lambda: x.clock.now)


def test_foreign_tenant_cannot_evaluate_or_close(ev):
    x = ev(converged=True)
    report = x.evaluate()
    foreign = IndependentEvaluator(CaseRepository(x.engine, "other"), clock=lambda: x.clock.now)
    with pytest.raises(CaseAccessError):
        foreign.evaluate(CASE)
    with pytest.raises(CaseAccessError):
        VerifiedClosureService(foreign).close(report)


@pytest.mark.parametrize("package", ["agent", "planner", "remediation", "authorization", "recovery", "adapters"])
def test_only_verified_closure_owns_transition(package):
    for path in Path("src/credit_harness", package).glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                assert node.attr != "CLOSED_VERIFIED", path


def test_evaluator_never_reads_ground_truth_or_llm():
    for path in Path("src/credit_harness/evaluation").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not any(module == s or module.startswith(s + ".") for s in (
                    "credit_harness.simulator", "credit_harness.planner.model", "credit_harness.remediation.model",
                    "openai", "credit_harness.adapters"))
            if isinstance(node, ast.Name):
                assert node.id not in ("WorldState", "GroundTruth", "RootCause", "ScenarioId", "SimulatorAdmin")
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("dispatch", "lookup", "recover", "execute_tool", "observe")


def test_evaluation_report_contains_no_raw_pii_or_model_rationale(ev):
    x = ev(converged=True)
    text = x.evaluate().model_dump_json()
    for denied in ("310101199001011234", "6222021234567890123", "13800138000", "raw_callback",
                   "ground_truth", "scenario_id", "root_cause", "signature", "reason_summary", "goal"):
        assert denied not in text


def test_new_side_effect_between_evaluate_and_close_blocks_closure(ev):
    x = ev(converged=True)
    report = x.evaluate()
    ledger = x.remediate(A.REQUEST_OPERATOR_REVIEW, prepared_only=True)
    assert ledger.status == E.PREPARED
    with pytest.raises(ClosureError):
        x.closure.close(report)
    assert R.UNRESOLVED_SIDE_EFFECT in x.evaluate().reason_codes


def test_recovered_applied_still_requires_read_evidence(ev):
    x = ev()
    x.remediate(timeout=True)
    from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
    from credit_harness.recovery.repository import RecoveryRepository
    from credit_harness.recovery.service import SideEffectRecoveryCoordinator
    from credit_harness.recovery.models import RecoveryPolicy
    resolver = SyntheticEffectStatusResolver(x.engine, "demo", clock=lambda: x.clock.now)
    repository = RecoveryRepository(x.runtime.store, resolver.capability, policy=RecoveryPolicy(grace_seconds=0))
    recovery = SideEffectRecoveryCoordinator(repository, resolver, clock=lambda: x.clock.now)
    result = recovery.recover(x.effect.effect_id)
    assert result.after_status == E.APPLIED
    assert R.POST_EFFECT_EVIDENCE_MISSING in x.evaluate().reason_codes
    x.read(T.MESSAGES)
    assert dimension(x.evaluate(), D.SIDE_EFFECT).status == S.PASS
    x.progress()
    x.read()
    assert_verdict(x.evaluate(), V.PASS)


def test_unknown_recovery_never_passes(ev):
    x = ev()
    x.remediate(timeout=True)
    x.progress()
    x.read()
    from credit_harness.recovery.repository import RecoveryRepository
    from credit_harness.recovery.service import SideEffectRecoveryCoordinator
    from credit_harness.recovery.models import RecoveryPolicy, RecoveryCapability, SideEffectLookupResult, LookupStatus
    from credit_harness.context.budget import digest
    class Resolver:
        capability = RecoveryCapability(resolver_id="TEST-INDETERMINATE", contract_version="1")
        def lookup(self, correlation_id, identity):
            return SideEffectLookupResult(correlation_id=correlation_id, command_identity_hash=digest(identity.model_dump(mode="json")),
                resolver_id=self.capability.resolver_id, lookup_status=LookupStatus.INDETERMINATE, observed_at=x.clock.now)
    resolver = Resolver()
    repository = RecoveryRepository(x.runtime.store, resolver.capability,
        policy=RecoveryPolicy(grace_seconds=0, backoff_seconds=(1,), max_attempts=3))
    recovery = SideEffectRecoveryCoordinator(repository, resolver, clock=lambda: x.clock.now)
    for _ in range(3):
        result = recovery.recover(x.effect.effect_id)
        x.advance(2)
    assert result.after_status == E.UNKNOWN and result.requires_escalation
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert R.RECOVERY_UNRESOLVED in report.reason_codes


def test_recovery_state_change_blocks_old_pass(ev):
    x = ev()
    x.remediate()
    x.progress()
    x.read()
    report = x.evaluate()
    from credit_harness.recovery.tables import EffectRecoveryStateRow
    with Session(x.engine) as session, session.begin():
        session.get(EffectRecoveryStateRow, x.effect.effect_id).requires_escalation = True
    with pytest.raises(ClosureError):
        x.closure.close(report)


def test_no_disbursement_cannot_be_inferred_from_lookup_not_found(ev):
    x = ev(no_disbursement=True, scenario=ScenarioId.S3)
    from credit_harness.simulator.faults import ObservationFault
    from credit_harness.domain.enums import FaultKind
    x.admin.add_fault(x.sid, ObservationFault(tool=T.PAYMENT, kind=FaultKind.INDEX_MISSING, starts_at=x.clock.now))
    x.read(T.PAYMENT)
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert report.outcome_path is None


def test_historical_settlement_cannot_be_erased_by_no_effect(ev):
    x = ev(converged=True)
    x.progress(no_disbursement=True)
    x.read()
    report = x.evaluate()
    assert_verdict(report, V.FAIL)
    assert R.MONEY_STATE_CONTRADICTION in report.reason_codes


def test_new_stale_payment_does_not_resurrect_old_success(ev):
    x = ev(converged=True)
    from credit_harness.simulator.faults import ObservationFault
    from credit_harness.domain.enums import FaultKind
    x.admin.add_fault(x.sid, ObservationFault(tool=T.PAYMENT, kind=FaultKind.OLD_CACHE,
                                             starts_at=x.clock.now, cached_revision=0))
    x.read(T.PAYMENT)
    report = x.evaluate()
    assert_verdict(report, V.INCONCLUSIVE)
    assert dimension(report, D.MONEY).status == S.INCONCLUSIVE


@pytest.mark.parametrize("seconds,verdict", [(0, V.INCONCLUSIVE), (31, V.FAIL)])
def test_convergence_window_distinguishes_waiting_from_failure(ev, seconds, verdict):
    x = ev()
    x.remediate()
    x.read(T.MESSAGES)
    x.advance(seconds)
    report = x.evaluate()
    assert_verdict(report, verdict)


def test_failed_confirmed_remediation_does_not_force_business_failure(ev):
    x = ev()
    # A valid preflight exists, but the remote operation proves no effect.
    x.remediate(prepared_only=True)
    from credit_harness.authorization.models import SideEffectReceipt, ReceiptOutcome
    store = x.runtime.store
    ledger = store.dispatch_prepared(x.capability.payload, lambda: x.clock.now)
    store.transition(ledger.effect_id, E.DISPATCHED, E.FAILED_CONFIRMED, x.clock.now,
        receipt=SideEffectReceipt(correlation_id=ledger.dispatch_correlation_id,
            outcome=ReceiptOutcome.FAILED_CONFIRMED, external_effect_ref=None, observed_at=x.clock.now, no_effect_confirmed=True))
    x.progress()
    x.read()
    assert_verdict(x.evaluate(), V.PASS)


def test_terminal_case_with_orphan_is_invariant_violation(ev):
    x = ev(converged=True)
    _, call_id = x.cases.reserve_call(CASE, T.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))
    with Session(x.engine) as session, session.begin():
        session.get(CaseRow, CASE).status = CaseStatus.CLOSED_VERIFIED.value
    from credit_harness.recovery.read import ReadObservationRecoveryService
    with pytest.raises(CasePolicyError):
        ReadObservationRecoveryService(x.evidence).recover(CASE, call_id)
    assert R.CLOSURE_INVARIANT_VIOLATION in x.evaluator.evaluate(CASE).reason_codes


def test_closure_report_must_be_persisted(ev):
    x = ev(converged=True)
    with pytest.raises(ClosureError) as error:
        x.closure.close(x.evaluator.evaluate(CASE))
    assert error.value.code == ClosureCode.REPORT_NOT_PERSISTED


@pytest.mark.parametrize("field,value", [("amount", 1000000), ("currency", Currency.USD),
    ("customer_ref", "CUS-OTHER"), ("account_ref", "ACC-OTHER"), ("fund_request_id", "FR-OTHER")])
def test_each_financial_identity_mismatch_fails(ev, field, value):
    x = ev(converged=True)
    x.progress(transaction_updates={field: value})
    x.read()
    report = x.evaluate()
    assert_verdict(report, V.FAIL)
    assert dimension(report, D.IDENTITY).status == S.FAIL


def test_no_oracle_sql_or_raw_pii_access_during_evaluation(ev, monkeypatch):
    x = ev(converged=True)
    from credit_harness.domain.models import WorldState
    from sqlalchemy import event
    monkeypatch.setattr(WorldState, "model_validate", Mock(side_effect=AssertionError("Oracle access")))
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(x.engine, "before_cursor_execute", capture)
    try:
        assert_verdict(x.evaluator.evaluate(CASE), V.PASS)
    finally:
        event.remove(x.engine, "before_cursor_execute", capture)
    assert not any(name in sql for sql in statements for name in (
        "world_snapshots", "evaluation_ground_truth", "observation_faults", "capability_signatures"))


def test_post_closure_pending_work_is_not_hidden_by_idempotent_close(ev):
    x = ev(converged=True)
    report = x.evaluate()
    x.closure.close(report)
    with Session(x.engine) as session, session.begin():
        call = session.scalar(select(CaseCallRow).where(CaseCallRow.case_id == CASE))
        call.state = CallState.ERROR.value
    with pytest.raises(ClosureError) as error:
        x.closure.close(report)
    assert error.value.code == ClosureCode.CLOSURE_INVARIANT_VIOLATION


def test_closure_and_new_dispatch_race_have_single_winner(ev):
    x = ev(converged=True)
    report = x.evaluate()
    barrier = Barrier(2)
    def close():
        barrier.wait()
        try:
            return x.closure.close(report).status
        except ClosureError:
            return "STALE"
    def dispatch():
        barrier.wait()
        try:
            x.cases.reserve_call(CASE, T.PAYMENT, ToolQuery(internal_order_id=x.case.internal_order_id))
            return "RESERVED"
        except CasePolicyError:
            return "BLOCKED"
    with ThreadPoolExecutor(2) as pool:
        closing, dispatching = pool.submit(close), pool.submit(dispatch)
        results = (closing.result(), dispatching.result())
    assert results in ((ClosureStatus.CLOSED_VERIFIED, "BLOCKED"), ("STALE", "RESERVED"))


@pytest.mark.parametrize("scenario,expected", [("s6-partially-repaired", V.INCONCLUSIVE),
    ("s6-converged", V.PASS), ("unknown-effect", V.INCONCLUSIVE),
    ("identity-mismatch", V.FAIL), ("no-disbursement", V.PASS)])
def test_evaluator_demo(engine, monkeypatch, scenario, expected):
    monkeypatch.setenv("CAPABILITY_SIGNING_SECRET", "synthetic-demo-evaluation-test-secret-0001")
    from scripts.demo_evaluator import run_demo
    output = run_demo(engine, scenario)
    assert output["report"]["overall_verdict"] == expected.value
    assert (output["case_status"] == CaseStatus.CLOSED_VERIFIED.value) == (expected == V.PASS)
    if scenario == "s6-partially-repaired":
        assert "BUT BUSINESS CASE NOT VERIFIED" in output["message"]


def test_snapshot_hash_is_reproducible_from_utc_serialized_report(ev):
    from datetime import datetime, timezone
    from credit_harness.context.budget import digest
    x = ev(converged=True)
    with Session(x.engine) as session, session.begin():
        row = session.get(CaseRow, CASE)
        row.updated_at = datetime.fromisoformat(row.updated_at).astimezone(timezone.utc).isoformat()
    report = x.evaluate()
    assert report.snapshot.verification_snapshot_id == digest(
        report.snapshot.model_dump(mode="json", exclude={"verification_snapshot_id"}))
    assert x.closure.close(report).status == ClosureStatus.CLOSED_VERIFIED
