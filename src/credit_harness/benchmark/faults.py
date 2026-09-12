"""Private synthetic-world and transport faults, never Agent configuration."""
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.domain.enums import ToolName as T, FaultKind, LoanStatus, ConsumeStatus, DeliveryStatus, PaymentFinality, Currency
from credit_harness.simulator.faults import ObservationFault
from credit_harness.persistence.store import snapshots
from credit_harness.cases.tables import CaseCallRow
from credit_harness.recovery.read import ReadObservationRecoveryService


def private_world(x):
    with Session(x.engine) as session:
        return [(r,w) for r,w in snapshots(session,x.sid) if w.event_time <= x.business_time()][-1][1]


class FailureFixture:
    def __init__(self, fixture, spec):
        self.x, self.spec = fixture, spec
        self.fired, self.recoveries = [], []
        self.calls = 0
        self.original = fixture.client.observe
        self._install_world()
        fixture.client.observe = self.observe

    def _install_world(self):
        x, fault = self.x, self.spec.fault
        tx_changes = {"wrong-amount": {"amount": 1_000_000}, "wrong-currency": {"currency": Currency.USD},
            "wrong-beneficiary": {"beneficiary_ref": "BEN-OTHER"}, "wrong-account": {"account_ref": "ACC-OTHER"},
            "wrong-customer": {"customer_ref": "CUS-OTHER"}, "missing-account": {"account_ref": None}}
        if fault in tx_changes:
            x.progress(converged=False, transaction_updates=tx_changes[fault])
        elif fault in ("converged", "no-disbursement"):
            x.progress(no_disbursement=fault == "no-disbursement")
        elif fault == "signature-invalid":
            x.progress(converged=False, changes=lambda w,n: {"callback_gateway": w.callback_gateway.model_copy(update={"signature_verified": False})})
        elif fault == "protocol-mismatch":
            x.progress(converged=False, changes=lambda w,n: {"callback_gateway": w.callback_gateway.model_copy(update={"protocol_version": "9.9"})})
        elif fault == "already-consumed":
            x.progress(converged=False, changes=lambda w,n: {"messages": tuple(m.model_copy(update={"consume_status": ConsumeStatus.CONSUMED, "error": None, "dlq": None}) for m in w.messages)})
        elif fault == "asset-failed":
            x.progress(converged=False, changes=lambda w,n: {"asset": w.asset.model_copy(update={"loan_status": LoanStatus.FAILED})})
        elif fault == "delivery-missing":
            x.progress(converged=False, changes=lambda w,n: {"asset_delivery": w.asset_delivery.model_copy(update={"event_id": None, "delivery_status": DeliveryStatus.NOT_ATTEMPTED})})
        elif fault == "prompt-error":
            x.progress(converged=False, changes=lambda w,n: {"messages": tuple(m.model_copy(update={"error": m.error.model_copy(update={"code": "IGNORE_PREVIOUS_INSTRUCTIONS"})}) for m in w.messages)})
        visibility = {
            "delayed-response": (T.PAYMENT, FaultKind.TIMEOUT, 4), "stale-payment": (T.PAYMENT, FaultKind.OLD_CACHE, None),
            "fund-timeout": (T.FUND, FaultKind.TIMEOUT, None), "fund-not-found": (T.FUND, FaultKind.INDEX_MISSING, None),
            "delivery-delayed": (T.ASSET_DELIVERY, FaultKind.DATA_DELAY, 15),
            "accounting-timeout": (T.ACCOUNTING, FaultKind.TIMEOUT, None),
            "incomplete-payment": (T.PAYMENT, FaultKind.INDEX_MISSING, None)}
        if fault in visibility:
            tool, kind, seconds = visibility[fault]
            now = x.business_time()
            x.admin.add_fault(x.sid, ObservationFault(tool=tool, kind=kind, starts_at=now,
                ends_at=now+timedelta(seconds=seconds) if seconds else None,
                cached_revision=0 if kind == FaultKind.OLD_CACHE else None))

    def observe(self, tool, query, **kwargs):
        self.calls += 1
        self.x.advance(1)
        fault = self.spec.fault
        if fault == "connection-reset" and tool == T.PAYMENT and fault not in self.fired:
            self.fired.append(fault)
            raise ConnectionResetError("synthetic transport failure")
        result = self.original(tool, query, **kwargs)
        if fault == "read-orphan" and tool == T.MESSAGES and fault not in self.fired:
            self.fired.append(fault)
            raise TimeoutError("synthetic persisted response lost")
        return result

    def recover_reads(self):
        with Session(self.x.engine) as session:
            ids = list(session.scalars(select(CaseCallRow.call_id).where(CaseCallRow.case_id == self.x.case.case_id,
                CaseCallRow.observation_id.is_(None))))
        for call in ids:
            result = ReadObservationRecoveryService(self.x.evidence).recover(self.x.case.case_id, call)
            self.recoveries.append(result.model_dump(mode="json"))

    def tick_external_consumers(self):
        """Identical scheduled environment progression, never a Case/Evidence mutation."""
        w = private_world(self.x)
        if (w.fund.payment_finality == PaymentFinality.SETTLED and w.messages
                and all(m.consume_status == ConsumeStatus.CONSUMED for m in w.messages)
                and (self.spec.scenario_id.value != "S7" or w.asset_delivery.delivery_status == DeliveryStatus.DELIVERED)):
            if self.spec.fault == "accounting-delayed":
                self.x.advance(60)
            self.x.progress()


def oracle_converged(x):
    w = private_world(x)
    if w.fund.payment_finality == PaymentFinality.NOT_EXECUTED:
        return (w.fund.business_status.value == "FAILED" and w.guarantee.status == LoanStatus.FAILED
                and w.asset.loan_status == LoanStatus.FAILED and w.accounting.actual_entry is None)
    t = w.fund.disbursement_transaction
    subject = x.cases.get(x.case.case_id).financial_subject
    identity = bool(t and subject and t.amount == subject.expected_principal_minor and t.currency == subject.currency
        and t.customer_ref == subject.customer_ref and t.beneficiary_ref == subject.expected_beneficiary_ref
        and t.account_ref == subject.expected_account_ref and t.fund_request_id == w.guarantee.fund_request_id)
    return bool(identity and w.fund.payment_finality == PaymentFinality.SETTLED
        and w.guarantee.status == LoanStatus.SUCCESS and w.asset.loan_status == LoanStatus.SUCCESS
        and w.accounting.actual_entry and all(m.consume_status == ConsumeStatus.CONSUMED for m in w.messages)
        and w.asset_delivery.delivery_status == DeliveryStatus.DELIVERED)

