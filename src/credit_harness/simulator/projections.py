"""Explicit allowlist projections. Never serialize WorldState wholesale."""

from credit_harness.domain.enums import FundBusinessStatus, ToolName
from credit_harness.domain.models import WorldState
from credit_harness.tools.contracts import (
    AccountingData, AssetData, CallbackData, CallbackRawData, CallbackRawRecord,
    CallbackRecord, DeliveryData, FundData, FundRecord, GuaranteeData,
    LoanNoteData, LoanNoteRecord, MessageRecord, MessagesData, PaymentData,
    PaymentRecord, PaymentTransactionRecord, ProtocolData, ToolQuery, TraceData,
)


def project(world: WorldState, tool: ToolName, query: ToolQuery, observed_at):
    if tool == ToolName.ASSET:
        return AssetData(record=world.asset), world.asset.event_time
    if tool == ToolName.GUARANTEE:
        return GuaranteeData(record=world.guarantee), world.guarantee.event_time
    if tool in (ToolName.FUND, ToolName.PAYMENT, ToolName.LOAN_NOTE):
        fund = world.fund
        if fund.business_status == FundBusinessStatus.NOT_RECEIVED:
            # Absence does NOT disclose the server-only NOT_RECEIVED fact.
            return None, None
        if tool == ToolName.FUND:
            return FundData(record=FundRecord(
                event_time=fund.event_time, fund_request_id=fund.fund_request_id,
                business_status=fund.business_status, loan_no=fund.loan_no,
                amount=fund.amount, currency=fund.currency,
            )), fund.event_time
        if tool == ToolName.PAYMENT:
            t = fund.disbursement_transaction
            transaction = None if t is None else PaymentTransactionRecord(
                event_time=t.event_time, transaction_id=t.transaction_id,
                fund_request_id=t.fund_request_id, loan_no=t.loan_no,
                amount=t.amount, currency=t.currency, payment_finality=t.payment_finality,
                customer_ref=t.customer_ref, beneficiary_ref=t.beneficiary_ref, account_ref=t.account_ref,
            )
            return PaymentData(record=PaymentRecord(
                event_time=fund.event_time, fund_request_id=fund.fund_request_id,
                payment_finality=fund.payment_finality, transaction=transaction,
            )), fund.event_time
        if fund.loan_no is None:
            return None, None
        return LoanNoteData(record=LoanNoteRecord(
            event_time=fund.event_time, fund_request_id=fund.fund_request_id,
            loan_no=fund.loan_no, amount=fund.amount, currency=fund.currency,
        )), fund.event_time
    if tool in (ToolName.CALLBACK, ToolName.CALLBACK_RAW):
        gateway = world.callback_gateway
        if gateway is None:
            return None, None
        values = dict(
            event_time=gateway.event_time, event_id=gateway.raw_callback.event_id,
            received_at=gateway.received_at, signature_verified=gateway.signature_verified,
            protocol_version=gateway.protocol_version,
        )
        if tool == ToolName.CALLBACK_RAW:
            return CallbackRawData(record=CallbackRawRecord(
                **values, raw_callback=gateway.raw_callback,
            )), gateway.event_time
        return CallbackData(record=CallbackRecord(**values)), gateway.event_time
    if tool == ToolName.MESSAGES:
        records = tuple(MessageRecord(
            event_time=message.event_time, message_id=message.message_id,
            event_id=message.message.event_id, topic=message.topic,
            consume_status=message.consume_status, dlq=message.dlq, error=message.error,
        ) for message in world.messages)
        return MessagesData(records=records), max((r.event_time for r in records), default=None)
    if tool == ToolName.ACCOUNTING:
        entry = world.accounting.actual_entry
        # Expected entry is oracle data and intentionally never exposed.
        return AccountingData(actual_entry=entry), entry.event_time if entry else None
    if tool == ToolName.TRACE:
        return TraceData(record=world.request_trace), world.request_trace.event_time
    if tool == ToolName.ASSET_DELIVERY:
        return DeliveryData(record=world.asset_delivery), world.asset_delivery.event_time
    if tool == ToolName.PROTOCOL:
        effective_at = query.effective_at or world.guarantee.event_time
        applicable = [record for record in world.protocols
                      if record.effective_time <= effective_at
                      and record.event_time <= observed_at
                      and (query.protocol_version is None
                           or record.protocol_version == query.protocol_version)]
        if not applicable:
            return None, None
        record = max(applicable, key=lambda p: p.effective_time)
        return ProtocolData(record=record), record.event_time
    raise ValueError("unsupported tool")
