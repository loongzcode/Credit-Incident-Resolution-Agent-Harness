import pytest

from credit_harness.domain.enums import (
    AccountingStatus, CallbackStatus, ConsumeStatus, DeliveryStatus, FieldType,
    Freshness, FundBusinessStatus, KnowledgeStatus, LoanStatus, ObservationStatus,
    PaymentFinality, RootCause, ScenarioId, SourceKind, ToolName, TransportStatus,
)
from credit_harness.simulator.scenarios import AMOUNT, LOAN_NO, ORDER_ID
from tests.support.inspector import ground_truth, world_state


def test_s1_request_never_sent(engine, seeded, query):
    sid, token = seeded(ScenarioId.S1)
    truth = ground_truth(engine, sid)
    assert truth.root_cause == RootCause.REQUEST_NOT_SENT
    assert not truth.request_sent and not truth.fund_accepted
    assert query(token, ToolName.TRACE).data.record.sent is False
    observation = query(token, ToolName.FUND)
    assert observation.status == ObservationStatus.NOT_FOUND
    assert observation.knowledge == KnowledgeStatus.UNKNOWN
    assert observation.data is None  # Must not expose hidden NOT_RECEIVED.


def test_s2_sent_but_fund_not_accepted(engine, seeded, query):
    sid, token = seeded(ScenarioId.S2)
    truth = ground_truth(engine, sid)
    assert truth.root_cause == RootCause.NOT_ACCEPTED
    assert truth.request_sent and not truth.fund_accepted
    trace = query(token, ToolName.TRACE).data.record
    assert trace.sent and trace.http_response == TransportStatus.TIMEOUT
    assert query(token, ToolName.FUND).knowledge == KnowledgeStatus.UNKNOWN
    assert query(token, ToolName.PAYMENT).knowledge == KnowledgeStatus.UNKNOWN


def test_s3_explicit_failure(engine, seeded, query):
    sid, token = seeded(ScenarioId.S3)
    truth = ground_truth(engine, sid)
    assert truth.root_cause == RootCause.FUND_REJECTED
    assert truth.fund_accepted and truth.disbursement_count == 0
    assert query(token, ToolName.FUND).data.record.business_status == FundBusinessStatus.FAILED
    assert query(token, ToolName.PAYMENT).data.record.payment_finality == PaymentFinality.NOT_EXECUTED
    assert query(token, ToolName.TRACE).data.record.response_business_status == FundBusinessStatus.FAILED


def test_s4_response_lost_and_replica_lags(engine, seeded, query, admin):
    sid, token = seeded(ScenarioId.S4)
    assert ground_truth(engine, sid).root_cause == RootCause.RESPONSE_LOST
    assert query(token, ToolName.TRACE).data.record.http_response == TransportStatus.TIMEOUT
    assert query(token, ToolName.PAYMENT).data.record.payment_finality == PaymentFinality.SETTLED
    assert world_state(engine, sid).guarantee.status == LoanStatus.SUCCESS
    stale = query(token, ToolName.GUARANTEE)
    assert stale.data.record.status == LoanStatus.PROCESSING
    assert stale.freshness == Freshness.STALE and stale.source_kind == SourceKind.REPLICA
    admin.advance(sid, 10)
    assert query(token, ToolName.GUARANTEE).data.record.status == LoanStatus.SUCCESS


def test_s5_payment_settled_callback_never_arrived(engine, seeded, query):
    sid, token = seeded(ScenarioId.S5)
    truth = ground_truth(engine, sid)
    assert truth.root_cause == RootCause.CALLBACK_NOT_DELIVERED
    assert truth.disbursement_count == 1 and not truth.callback_reached_gateway
    assert query(token, ToolName.CALLBACK).status == ObservationStatus.NOT_FOUND
    assert query(token, ToolName.MESSAGES).data.records == ()
    assert query(token, ToolName.PAYMENT).data.record.payment_finality == PaymentFinality.SETTLED
    assert query(token, ToolName.ACCOUNTING).data.actual_entry is None


def test_s6_jd_order_real_parser_failure(engine, seeded, query):
    sid, token = seeded(ScenarioId.S6)
    truth = ground_truth(engine, sid)
    world = world_state(engine, sid)
    assert truth.root_cause == RootCause.CALLBACK_SCHEMA_MISMATCH
    assert truth.actual_payment_finality == PaymentFinality.SETTLED
    assert truth.callback_reached_gateway and not truth.callback_consumed
    assert world.guarantee.internal_order_id == ORDER_ID == "JD202609100001"
    assert world.fund.amount == AMOUNT == 2_000_000
    assert world.fund.loan_no == LOAN_NO
    assert world.asset.loan_status == world.guarantee.status == LoanStatus.PROCESSING
    assert world.guarantee.version == 17
    assert world.guarantee.accounting_status == AccountingStatus.NOT_CREATED
    assert world.accounting.expected_entry is not None and world.accounting.actual_entry is None
    assert query(token, ToolName.TRACE).data.record.http_response == TransportStatus.TIMEOUT
    gateway = query(token, ToolName.CALLBACK).data.record
    assert gateway.signature_verified and gateway.protocol_version == "2.3"
    message = query(token, ToolName.MESSAGES).data.records[0]
    assert message.consume_status == ConsumeStatus.FAILED
    assert message.dlq == "loan.callback.dlq"
    assert message.error.field == "loanNo"
    assert message.error.expected_type == FieldType.INTEGER
    assert message.error.actual_type == FieldType.STRING
    raw = query(token, ToolName.CALLBACK_RAW).data.record.raw_callback
    assert isinstance(raw.loanNo, str)
    current = query(token, ToolName.PROTOCOL, protocol_version="2.3").data.record
    old = query(token, ToolName.PROTOCOL, protocol_version="2.2").data.record
    assert current.field_schema["loanNo"] == FieldType.STRING
    assert old.field_schema["loanNo"] == FieldType.INTEGER
    assert query(token, ToolName.GUARANTEE).data.record.callback_status == CallbackStatus.PARSE_FAILED


def test_s7_internal_success_asset_delivery_failed(engine, seeded, query):
    sid, token = seeded(ScenarioId.S7)
    truth = ground_truth(engine, sid)
    assert truth.root_cause == RootCause.ASSET_NOTIFICATION_FAILED
    assert truth.callback_consumed and truth.accounting_created
    assert query(token, ToolName.GUARANTEE).data.record.status == LoanStatus.SUCCESS
    assert query(token, ToolName.ASSET).data.record.loan_status == LoanStatus.PROCESSING
    assert query(token, ToolName.ASSET_DELIVERY).data.record.delivery_status == DeliveryStatus.FAILED
    assert query(token, ToolName.ACCOUNTING).data.actual_entry.amount == AMOUNT


def test_s8_unobservable_payment_stays_unknown(engine, seeded, query, admin):
    sid, token = seeded(ScenarioId.S8)
    truth = ground_truth(engine, sid)
    assert truth.root_cause == RootCause.PAYMENT_PENDING_UNOBSERVABLE
    assert truth.fund_accepted and truth.actual_payment_finality == PaymentFinality.PENDING
    assert truth.funds_must_remain_unknown and truth.disbursement_count == 0
    for _ in range(3):
        fund = query(token, ToolName.FUND)
        payment = query(token, ToolName.PAYMENT)
        assert fund.status == ObservationStatus.NOT_FOUND and fund.source_kind == SourceKind.CACHE
        assert payment.status == ObservationStatus.TIMEOUT
        assert payment.data is None and payment.knowledge == KnowledgeStatus.UNKNOWN
        assert payment.event_time is None  # Must not leak true payment event time on timeout.
        assert query(token, ToolName.GUARANTEE).data.record.status == LoanStatus.PROCESSING
        admin.advance(sid, 15)


@pytest.mark.parametrize("scenario", list(ScenarioId))
def test_all_scenarios_ground_truth_matches_independent_world_facts(engine, seeded, query, scenario):
    sid, token = seeded(scenario)
    before = world_state(engine, sid)
    truth = ground_truth(engine, sid)
    assert truth.request_sent == before.request_trace.sent
    assert truth.fund_accepted == (before.fund.accepted_at is not None)
    assert truth.disbursement_count == int(before.fund.disbursement_transaction is not None)
    assert truth.callback_reached_gateway == (before.callback_gateway is not None)
    assert truth.accounting_created == (before.accounting.actual_entry is not None)
    for tool in ToolName:
        observation = query(token, tool)
        assert observation.observed_at.tzinfo is not None
        if observation.event_time is not None:
            assert observation.event_time <= observation.observed_at
    assert world_state(engine, sid) == before  # Queries never change financial facts.
    assert before.disbursement_intent_count == 1
