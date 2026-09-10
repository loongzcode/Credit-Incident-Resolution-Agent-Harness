from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

from .enums import (
    AccountingStatus, BusinessMeaning, CallbackStatus, ConsumeStatus, Currency,
    DeliveryStatus, FieldType, FundBusinessStatus, LoanStatus, PaymentFinality,
    TransportStatus,
)

MinorAmount = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(ge=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventModel(Model):
    event_time: AwareDatetime


class AssetSystem(EventModel):
    asset_order_id: str
    loan_status: LoanStatus
    last_callback_event: str | None = None
    update_time: AwareDatetime


class GuaranteeCore(EventModel):
    internal_order_id: str
    status: LoanStatus
    version: PositiveInt
    fund_request_id: str
    loan_no: str | None = None
    accounting_status: AccountingStatus
    callback_status: CallbackStatus
    protocol_version: str


class DisbursementTransaction(EventModel):
    transaction_id: str
    fund_request_id: str
    loan_no: str
    amount: MinorAmount
    currency: Currency
    payment_finality: PaymentFinality


class FundSystem(EventModel):
    fund_request_id: str
    business_status: FundBusinessStatus
    accepted_at: AwareDatetime | None = None
    loan_no: str | None = None
    disbursement_transaction: DisbursementTransaction | None = None
    amount: MinorAmount
    currency: Currency
    payment_finality: PaymentFinality

    @model_validator(mode="after")
    def validate_transaction(self):
        transaction = self.disbursement_transaction
        if transaction is not None:
            if (transaction.fund_request_id, transaction.loan_no, transaction.amount,
                transaction.currency, transaction.payment_finality) != (
                self.fund_request_id, self.loan_no, self.amount, self.currency,
                self.payment_finality,
            ):
                raise ValueError("transaction must match its fund record")
        if self.payment_finality == PaymentFinality.SETTLED and transaction is None:
            raise ValueError("SETTLED requires a disbursement transaction")
        return self


class CallbackPayload(Model):
    event_id: str
    fund_request_id: str
    loanNo: StrictInt | str
    business_status: FundBusinessStatus


class CallbackGateway(EventModel):
    raw_callback: CallbackPayload
    received_at: AwareDatetime
    signature_verified: bool
    protocol_version: str


class MessageError(Model):
    code: str
    field: str
    expected_type: FieldType
    actual_type: FieldType
    detail: str


class MessageSystem(EventModel):
    message_id: str
    topic: str
    message: CallbackPayload
    consume_status: ConsumeStatus
    dlq: str | None = None
    error: MessageError | None = None


class AccountingEntry(EventModel):
    entry_id: str
    internal_order_id: str
    loan_no: str
    amount: MinorAmount
    currency: Currency
    entry_type: str = "GUARANTEE_LOAN_BUSINESS_PROJECTION"


class AccountingSystem(EventModel):
    expected_entry: AccountingEntry | None = None
    actual_entry: AccountingEntry | None = None


class IdempotencySemantics(Model):
    key_fields: tuple[str, ...]
    retention_seconds: PositiveInt
    same_key_different_payload: str
    timeout_policy: str


class ProtocolRegistry(EventModel):
    partner: str
    protocol_version: str
    effective_time: AwareDatetime
    field_schema: dict[str, FieldType]
    business_semantics: dict[FundBusinessStatus, BusinessMeaning]
    idempotency_semantics: IdempotencySemantics


class RequestTrace(EventModel):
    trace_id: str
    fund_request_id: str
    sent: bool
    sent_at: AwareDatetime | None = None
    http_response: TransportStatus
    response_business_status: FundBusinessStatus | None = None


class AssetDelivery(EventModel):
    event_id: str | None = None
    delivery_status: DeliveryStatus
    error_code: str | None = None


class WorldState(EventModel):
    """Server-only facts. NEVER use this model as a tool/API response."""

    asset: AssetSystem
    guarantee: GuaranteeCore
    fund: FundSystem
    callback_gateway: CallbackGateway | None = None
    messages: tuple[MessageSystem, ...] = ()
    accounting: AccountingSystem
    protocols: tuple[ProtocolRegistry, ...]
    request_trace: RequestTrace
    asset_delivery: AssetDelivery
    disbursement_intent_count: Annotated[StrictInt, Field(ge=0)] = 1

