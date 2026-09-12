"""Synthetic external-world progression ONLY for tests and the local demo.

Every business fact goes through the real HTTP Observation / Case executor /
Evidence publication chain. No Evidence or evaluation result is inserted here.
"""
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session
from scripts.demo_case_evidence import LocalHTTPClient
from scripts.demo_side_effect import ready_projection
from credit_harness.api.app import create_app
from credit_harness.cases.schema import create_harness_schema
from credit_harness.cases.repository import CaseRepository
from credit_harness.cases import repository as case_repository
from credit_harness.cases.service import CaseService
from credit_harness.cases.fixtures import investigation_case
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.domain.enums import (ScenarioId, ToolName as T, LoanStatus, FundBusinessStatus,
    PaymentFinality, ConsumeStatus, DeliveryStatus, AccountingStatus, CallbackStatus)
from credit_harness.domain.models import AccountingEntry, AssetDelivery, WorldState
from credit_harness.persistence.store import (SimulatorAdmin, SimulationRow, SnapshotRow, create_schema, snapshots)
from credit_harness.simulator.scenarios import build_scenario
from credit_harness.simulator import service as projection_service
from credit_harness.tools.contracts import ToolQuery
from credit_harness.remediation.state import InvestigationStateReader
from credit_harness.remediation.model import FakeRemediationModel
from credit_harness.remediation.models import RemediationActionType as A, RemediationCandidate, RemediationDraft
from credit_harness.remediation.service import RemediationPlanner
from credit_harness.authorization.models import ApprovalDecision, ApprovalStatus
from credit_harness.authorization.signing import HMACCapabilitySigner
from credit_harness.authorization.service import RemediationAuthorizationService, RemediationExecutionService
from credit_harness.authorization.tables import create_authorization_schema
from credit_harness.adapters.synthetic_remediation import SyntheticRemediationAdapter, create_synthetic_effect_schema
from credit_harness.evaluation.tables import create_evaluation_schema
from credit_harness.evaluation.evaluator import IndependentEvaluator
from credit_harness.evaluation.repository import EvaluationRepository
from credit_harness.evaluation.closure import VerifiedClosureService

CASE = "CASE-JD202609100001"
READS = (T.TRACE, T.FUND, T.PAYMENT, T.GUARANTEE, T.ASSET, T.ACCOUNTING,
         T.CALLBACK, T.MESSAGES, T.ASSET_DELIVERY)


class EvaluationFixture:
    def __init__(self, engine, *, scenario=ScenarioId.S6, case_id=CASE, tenant_id="demo", max_tool_calls=100, ready=True):
        self.engine = engine
        for create in (create_schema, create_harness_schema, create_authorization_schema,
                       create_synthetic_effect_schema, create_evaluation_schema):
            create(engine)
        self.stack = ExitStack()
        if ready:
            self.stack.enter_context(patch.object(projection_service, "project", ready_projection(projection_service.project)))
        self.admin = SimulatorAdmin(engine)
        self.sid = self.admin.seed(build_scenario(scenario))
        self.token = self.admin.grant(self.sid, set(T), ttl_seconds=7200)
        # Align Case audit timestamps with this fixture's controlled clock too;
        # a historical simulation must not appear closed before Case creation.
        self.stack.enter_context(patch.object(case_repository, "utc_now", self.business_time))
        self.cases = CaseRepository(engine, tenant_id)
        started_at = self.business_time()
        task = investigation_case(self.sid, max_tool_calls=max_tool_calls).model_copy(update={
            "created_at": started_at, "updated_at": started_at, "case_id": case_id, "tenant_id": tenant_id})
        self.case = CaseService(self.cases).create(task, tool_credential=self.token)
        self.evidence = EvidenceRepository(self.cases)
        http = self.stack.enter_context(TestClient(create_app(engine)))
        self.client = LocalHTTPClient(http, self.token)
        self.executor = CaseToolExecutor(self.cases, self.evidence, lambda _: self.client)
        self.reader = InvestigationStateReader(self.cases, self.evidence)
        self.clock = SimpleNamespace(now=self.business_time())
        self.evaluator = IndependentEvaluator(self.cases, clock=lambda: self.clock.now)
        self.repository = EvaluationRepository(self.cases)
        self.closure = VerifiedClosureService(self.evaluator)
        self.advance(40)

    def close(self):
        self.stack.close()

    def business_time(self):
        with Session(self.engine) as session:
            return datetime.fromisoformat(session.get(SimulationRow, self.sid).clock)

    def advance(self, seconds):
        self.admin.advance(self.sid, seconds)
        self.clock.now = self.business_time()

    def read(self, *tools):
        for tool in tools or READS:
            self.advance(1)
            self.executor.execute(self.case.case_id, tool, ToolQuery(internal_order_id=self.case.internal_order_id))
        if not tools:
            for version in ("2.3", "2.2"):
                self.advance(1)
                self.executor.execute(self.case.case_id, T.PROTOCOL,
                    ToolQuery(internal_order_id=self.case.internal_order_id, protocol_version=version))

    def progress(self, *, converged=True, no_disbursement=False, transaction_updates=None, changes=None):
        with Session(self.engine) as session, session.begin():
            session.execute(update(SimulationRow).where(SimulationRow.id == self.sid).values(clock_version=SimulationRow.clock_version))
            simulation = session.get(SimulationRow, self.sid)
            now = datetime.fromisoformat(simulation.clock) + timedelta(seconds=1)
            world = [(r, w) for r, w in snapshots(session, self.sid) if w.event_time <= now][-1][1]
            updates = dict(event_time=now)
            if converged:
                status = LoanStatus.FAILED if no_disbursement else LoanStatus.SUCCESS
                updates.update(guarantee=world.guarantee.model_copy(update=dict(status=status, event_time=now,
                    version=world.guarantee.version + 1, accounting_status=AccountingStatus.NOT_CREATED if no_disbursement else AccountingStatus.POSTED,
                    callback_status=CallbackStatus.APPLIED)),
                    asset=world.asset.model_copy(update=dict(loan_status=status, event_time=now, update_time=now)),
                    accounting=world.accounting.model_copy(update=dict(event_time=now, actual_entry=None if no_disbursement else AccountingEntry(
                        event_time=now, entry_id="ENTRY-SYNTHETIC-001", internal_order_id=self.case.internal_order_id,
                        loan_no=world.fund.loan_no or "LOAN-SYNTHETIC-001", amount=world.fund.amount, currency=world.fund.currency))))
                if not no_disbursement:
                    updates.update(messages=tuple(m.model_copy(update=dict(consume_status=ConsumeStatus.CONSUMED,
                        error=None, dlq=None, event_time=now)) for m in world.messages),
                        asset_delivery=AssetDelivery(event_time=now, event_id=world.asset_delivery.event_id or "DELIVERY-SYNTHETIC-001",
                            delivery_status=DeliveryStatus.DELIVERED))
            fund = world.fund
            if no_disbursement:
                fund = fund.model_copy(update=dict(business_status=FundBusinessStatus.FAILED,
                    payment_finality=PaymentFinality.NOT_EXECUTED, disbursement_transaction=None, loan_no=None, event_time=now))
            if transaction_updates:
                t = fund.disbursement_transaction.model_copy(update=transaction_updates)
                fund = fund.model_copy(update=dict(disbursement_transaction=t, amount=t.amount, currency=t.currency,
                    fund_request_id=t.fund_request_id, payment_finality=t.payment_finality, event_time=now))
            updates["fund"] = fund
            if changes:
                updates.update(changes(world, now))
            changed = WorldState.model_validate(world.model_copy(update=updates).model_dump())
            revision = session.scalar(select(func.max(SnapshotRow.revision)).where(SnapshotRow.simulation_id == self.sid)) + 1
            session.add(SnapshotRow(simulation_id=self.sid, revision=revision, state=changed.model_dump(mode="json")))
            simulation.clock, simulation.clock_version = now.isoformat(), simulation.clock_version + 1
        self.clock.now = self.business_time()

    def remediate(self, action=A.REPLAY_CALLBACK_CONSUMPTION, *, timeout=False, prepared_only=False):
        def proposal(bundle):
            state = self.reader.read(self.case.case_id)
            return RemediationDraft(snapshot_id=bundle.snapshot_id, candidates=(RemediationCandidate(
                candidate_id="synthetic-action", action_type=action, target_order_id=self.case.internal_order_id,
                target_problem_ids=("H6",) if action == A.REPLAY_CALLBACK_CONSUMPTION else
                    ("H4",) if action == A.REQUEST_OPERATOR_REVIEW else ("H7",),
                evidence_refs=tuple(e.evidence_id for e in state.index.evidence), reason_summary="Synthetic test proposal."),))
        decision = RemediationPlanner(self.reader, FakeRemediationModel(proposal)).plan(self.case.case_id)
        assert decision.final_intent is not None
        signer = HMACCapabilitySigner()
        self.auth = RemediationAuthorizationService(self.reader, signer, clock=lambda: self.clock.now)
        approval = None
        if decision.final_intent.requires_approval:
            requested = self.auth.request_approval(decision.final_intent)
            approval = self.auth.decide_approval(ApprovalDecision(approval_id=requested.approval_id,
                decision=ApprovalStatus.APPROVED, actor_ref="SYNTHETIC-OPERATOR"))
        self.capability = self.auth.issue_capability(decision.final_intent,
            approval_id=approval.approval_id if approval else None)
        adapter = SyntheticRemediationAdapter(self.engine, self.case.tenant_id)
        class LostResponse:
            def dispatch(_, command, correlation):
                adapter.dispatch(command, correlation)
                raise TimeoutError("synthetic lost receipt")
        self.runtime = RemediationExecutionService(self.reader, signer, LostResponse() if timeout else adapter,
                                                   clock=lambda: self.clock.now)
        self.effect = (self.runtime.store.prepare(self.capability.payload, lambda: self.clock.now)[0]
                       if prepared_only else self.runtime.execute(self.capability).ledger)
        return self.effect

    def evaluate(self):
        return self.repository.record(self.evaluator.evaluate(self.case.case_id))

