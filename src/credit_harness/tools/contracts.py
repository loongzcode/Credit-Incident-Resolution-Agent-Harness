from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from credit_harness.domain.enums import (
    Completeness, ConsumeStatus, Currency, Freshness, FundBusinessStatus, KnowledgeStatus,
    ObservationStatus, PaymentFinality, SourceKind, ToolName, FieldType,
)
from credit_harness.domain.models import (
    AccountingEntry, AssetDelivery, AssetSystem, CallbackPayload,
    EventModel, GuaranteeCore, MessageError, MinorAmount,
    Model, ProtocolRegistry, RequestTrace,
)
from credit_harness.domain.identity import AccountRef, BeneficiaryRef, CustomerRef


# Infrastructure header only; never part of ToolQuery or business Observation.
DISPATCH_CORRELATION_HEADER = "X-Dispatch-Correlation-Id"
DispatchCorrelationId = Annotated[str, Field(
    strict=True, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
)]


class ToolQuery(Model):
    internal_order_id: Annotated[str, Field(min_length=1, max_length=80)]
    protocol_version: Annotated[str, Field(pattern=r"^\d+\.\d+$")] | None = None
    effective_at: AwareDatetime | None = None


class AssetData(Model):
    kind: Literal["asset"] = "asset"
    record: AssetSystem


class GuaranteeData(Model):
    kind: Literal["guarantee"] = "guarantee"
    record: GuaranteeCore


class FundRecord(EventModel):
    fund_request_id: str
    business_status: FundBusinessStatus
    loan_no: str | None
    amount: MinorAmount
    currency: Currency


class FundData(Model):
    kind: Literal["fund"] = "fund"
    record: FundRecord


class PaymentTransactionRecord(EventModel):
    """Allowlisted payment DTO, deliberately separate from server transaction state."""
    transaction_id: str
    fund_request_id: str
    loan_no: str
    amount: MinorAmount
    currency: Currency
    payment_finality: PaymentFinality
    customer_ref: CustomerRef | None = None
    beneficiary_ref: BeneficiaryRef | None = None
    account_ref: AccountRef | None = None


class PaymentRecord(EventModel):
    fund_request_id: str
    payment_finality: PaymentFinality
    transaction: PaymentTransactionRecord | None


class PaymentData(Model):
    kind: Literal["payment"] = "payment"
    record: PaymentRecord


class LoanNoteRecord(EventModel):
    fund_request_id: str
    loan_no: str
    amount: MinorAmount
    currency: Currency


class LoanNoteData(Model):
    kind: Literal["loan_note"] = "loan_note"
    record: LoanNoteRecord


class CallbackRecord(EventModel):
    event_id: str
    received_at: AwareDatetime
    signature_verified: bool
    protocol_version: str


class CallbackData(Model):
    kind: Literal["callback"] = "callback"
    record: CallbackRecord


class CallbackRawRecord(CallbackRecord):
    raw_callback: CallbackPayload


class CallbackRawData(Model):
    kind: Literal["callback_raw"] = "callback_raw"
    record: CallbackRawRecord


class ConsumerDeploymentObservation(EventModel):
    """Optional directly observed current runtime configuration, not an inference
    from a historical parser error. Normal simulator projections don't provide it.
    A trusted test observation adapter supplies synthetic readiness attestations.
    """
    schema_version: Annotated[str, Field(pattern=r"^\d+\.\d+$")]
    accepted_protocol_version: Annotated[str, Field(pattern=r"^\d+\.\d+$")]
    loan_no_type: FieldType


class MessageRecord(EventModel):
    message_id: str
    event_id: str
    topic: str
    consume_status: ConsumeStatus
    dlq: str | None
    error: MessageError | None
    consumer_deployment: ConsumerDeploymentObservation | None = None


class MessagesData(Model):
    kind: Literal["messages"] = "messages"
    records: tuple[MessageRecord, ...]


class AccountingData(Model):
    kind: Literal["accounting"] = "accounting"
    actual_entry: AccountingEntry | None


class TraceData(Model):
    kind: Literal["trace"] = "trace"
    record: RequestTrace


class DeliveryData(Model):
    kind: Literal["asset_delivery"] = "asset_delivery"
    record: AssetDelivery


class ProtocolData(Model):
    kind: Literal["protocol"] = "protocol"
    record: ProtocolRegistry


ObservationData = Annotated[
    AssetData | GuaranteeData | FundData | PaymentData | LoanNoteData |
    CallbackData | CallbackRawData | MessagesData | AccountingData |
    TraceData | DeliveryData | ProtocolData,
    Field(discriminator="kind"),
]


class Observation(Model):
    observation_id: str
    tool: ToolName
    internal_order_id: str
    status: ObservationStatus
    # Unknown on timeout/not-found. OBSERVED means visible data, NOT financial finality.
    knowledge: KnowledgeStatus
    event_time: AwareDatetime | None
    observed_at: AwareDatetime
    source_as_of: AwareDatetime | None
    source_kind: SourceKind
    completeness: Completeness
    freshness: Freshness
    data: ObservationData | None

    @model_validator(mode="after")
    def validate_observation(self):
        for timestamp in (self.event_time, self.source_as_of):
            if timestamp is not None and timestamp > self.observed_at:
                raise ValueError("observations cannot contain future evidence")
        if self.status != ObservationStatus.OK:
            if self.data is not None or self.knowledge != KnowledgeStatus.UNKNOWN:
                raise ValueError("failed lookups carry no facts and remain UNKNOWN")
        elif self.data is None or self.knowledge != KnowledgeStatus.OBSERVED:
            raise ValueError("OK requires an observed projection")
        return self
