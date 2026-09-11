"""Shared positive claim contracts for eligibility AND final model validation."""
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, TypeAdapter
from credit_harness.domain.enums import (
    BusinessMeaning, ConsumeStatus, Currency, DeliveryStatus, FieldType,
    FundBusinessStatus, LoanStatus, ObservationStatus, PaymentFinality, TransportStatus,
)
from credit_harness.domain.identity import CustomerRef, BeneficiaryRef, AccountRef
from credit_harness.evidence.models import ClaimType as C
from .structured_values import (
    OpaqueBusinessRef, StructuredErrorCode, StructuredFieldPath, StructuredTopic, StructuredVersion,
)

_TYPES = {
    C.LOAN_NOTE_REFERENCE: OpaqueBusinessRef,
    C.PAYMENT_TRANSACTION_ID: OpaqueBusinessRef,
    C.TRANSACTION_FUND_REQUEST_ID: OpaqueBusinessRef,
    C.MESSAGE_ERROR_CODE: StructuredErrorCode,
    C.MESSAGE_ERROR_FIELD: StructuredFieldPath,
    C.MESSAGE_DLQ: StructuredTopic,
    C.PAYMENT_CUSTOMER_REF: CustomerRef,
    C.PAYMENT_BENEFICIARY_REF: BeneficiaryRef,
    C.PAYMENT_ACCOUNT_REF: AccountRef,
    C.CALLBACK_PROTOCOL_VERSION: StructuredVersion,
}
for claim, enum in {
    C.HTTP_RESPONSE_STATUS: TransportStatus, C.FUND_BUSINESS_STATUS: FundBusinessStatus,
    C.PAYMENT_FINALITY: PaymentFinality, C.PAYMENT_CURRENCY: Currency,
    C.MESSAGE_CONSUME_STATUS: ConsumeStatus, C.MESSAGE_EXPECTED_FIELD_TYPE: FieldType,
    C.MESSAGE_ACTUAL_FIELD_TYPE: FieldType, C.ASSET_STATUS: LoanStatus, C.GUARANTEE_STATUS: LoanStatus,
    C.ASSET_DELIVERY_STATUS: DeliveryStatus, C.PROTOCOL_FIELD_TYPE: FieldType,
    C.PROTOCOL_BUSINESS_SEMANTICS: BusinessMeaning, C.SOURCE_LOOKUP_STATUS: ObservationStatus,
}.items():
    _TYPES[claim] = Literal[tuple(item.value for item in enum)]
for claim in (C.REQUEST_SENT, C.LOAN_NO_PRESENT, C.CALLBACK_GATEWAY_RECEIVED,
              C.CALLBACK_SIGNATURE_VERIFIED, C.ACCOUNTING_ENTRY_PRESENT):
    _TYPES[claim] = StrictBool
for claim in (C.PAYMENT_AMOUNT, C.GUARANTEE_VERSION):
    _TYPES[claim] = Annotated[StrictInt, Field(ge=0)]

_ADAPTERS = {claim: TypeAdapter(kind) for claim, kind in _TYPES.items()}
_PATH = TypeAdapter(StructuredFieldPath)
_STATUS_KEY = TypeAdapter(StructuredErrorCode)


def validate_claim_value(claim, value):
    if claim not in _ADAPTERS:
        raise ValueError("claim has no Context value contract")
    _ADAPTERS[claim].validate_python(value)


def validate_subject_field(claim, field):
    if claim == C.PROTOCOL_BUSINESS_SEMANTICS:
        _STATUS_KEY.validate_python(field)
    elif claim == C.PROTOCOL_FIELD_TYPE or field is not None:
        _PATH.validate_python(field)
