"""Positive structured-field allowlist, not a regex PII detector."""
import re
from typing import ClassVar

from credit_harness.domain.enums import (
    BusinessMeaning, ConsumeStatus, Currency, DeliveryStatus, FieldType,
    FundBusinessStatus, LoanStatus, ObservationStatus, PaymentFinality, TransportStatus,
)
from credit_harness.evidence.models import ClaimType as C
from credit_harness.domain.models import Model
from .models import InformationClass as I


class ContextEligibilityError(ValueError):
    pass


TOKEN_CLAIMS = frozenset({C.PAYMENT_CUSTOMER_REF, C.PAYMENT_BENEFICIARY_REF, C.PAYMENT_ACCOUNT_REF})
BUSINESS_CLAIMS = frozenset({
    C.REQUEST_SENT, C.HTTP_RESPONSE_STATUS, C.FUND_BUSINESS_STATUS, C.LOAN_NO_PRESENT, C.LOAN_NOTE_REFERENCE,
    C.PAYMENT_FINALITY, C.PAYMENT_TRANSACTION_ID, C.PAYMENT_AMOUNT, C.PAYMENT_CURRENCY,
    C.TRANSACTION_FUND_REQUEST_ID, C.CALLBACK_GATEWAY_RECEIVED, C.CALLBACK_SIGNATURE_VERIFIED,
    C.CALLBACK_PROTOCOL_VERSION, C.MESSAGE_CONSUME_STATUS, C.MESSAGE_DLQ, C.MESSAGE_ERROR_CODE,
    C.MESSAGE_ERROR_FIELD, C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE,
    C.ACCOUNTING_ENTRY_PRESENT, C.ASSET_STATUS, C.GUARANTEE_STATUS, C.GUARANTEE_VERSION,
    C.ASSET_DELIVERY_STATUS, C.PROTOCOL_FIELD_TYPE, C.PROTOCOL_BUSINESS_SEMANTICS, C.SOURCE_LOOKUP_STATUS,
})


def structured_value_allowed(e) -> bool:
    enums = {
        C.HTTP_RESPONSE_STATUS: TransportStatus, C.FUND_BUSINESS_STATUS: FundBusinessStatus,
        C.PAYMENT_FINALITY: PaymentFinality, C.PAYMENT_CURRENCY: Currency,
        C.MESSAGE_CONSUME_STATUS: ConsumeStatus, C.MESSAGE_EXPECTED_FIELD_TYPE: FieldType,
        C.MESSAGE_ACTUAL_FIELD_TYPE: FieldType, C.ASSET_STATUS: LoanStatus, C.GUARANTEE_STATUS: LoanStatus,
        C.ASSET_DELIVERY_STATUS: DeliveryStatus, C.PROTOCOL_FIELD_TYPE: FieldType,
        C.PROTOCOL_BUSINESS_SEMANTICS: BusinessMeaning, C.SOURCE_LOOKUP_STATUS: ObservationStatus,
    }
    if e.claim_type in enums:
        return type(e.value) is str and e.value in {v.value for v in enums[e.claim_type]}
    if e.claim_type in {C.REQUEST_SENT, C.LOAN_NO_PRESENT, C.CALLBACK_GATEWAY_RECEIVED,
                        C.CALLBACK_SIGNATURE_VERIFIED, C.ACCOUNTING_ENTRY_PRESENT}:
        return type(e.value) is bool
    if e.claim_type in {C.PAYMENT_AMOUNT, C.GUARANTEE_VERSION}:
        return type(e.value) is int and e.value >= 0
    patterns = {
        C.LOAN_NOTE_REFERENCE: r"LN-[A-Za-z0-9-]+", C.PAYMENT_TRANSACTION_ID: r"PAY-[A-Za-z0-9-]+",
        C.TRANSACTION_FUND_REQUEST_ID: r"FREQ-[A-Za-z0-9-]+", C.PAYMENT_CUSTOMER_REF: r"CUS-[A-Za-z0-9-]+",
        C.PAYMENT_BENEFICIARY_REF: r"BEN-[A-Za-z0-9-]+", C.PAYMENT_ACCOUNT_REF: r"ACC-[A-Za-z0-9-]+",
        C.CALLBACK_PROTOCOL_VERSION: r"\d+\.\d+", C.MESSAGE_DLQ: r"loan\.callback\.dlq",
        C.MESSAGE_ERROR_CODE: r"CALLBACK_SCHEMA_MISMATCH", C.MESSAGE_ERROR_FIELD: r"loanNo",
    }
    return (e.claim_type in patterns and type(e.value) is str and len(e.value) <= 96
            and re.fullmatch(patterns[e.claim_type], e.value) is not None)


class ContextEligibilityPolicy(Model):
    # Deployment policy may narrow access, never enable raw/masked/oracle classes.
    denied_claims: frozenset[C] = frozenset()
    allowed_classes: ClassVar[frozenset[I]] = frozenset({I.BUSINESS, I.TOKENIZED_IDENTITY, I.INTERNAL_CONTROL})

    def allows_class(self, classification: I) -> bool:
        return classification in self.allowed_classes

    def classify(self, claim: C) -> I | None:
        return I.TOKENIZED_IDENTITY if claim in TOKEN_CLAIMS else I.BUSINESS if claim in BUSINESS_CLAIMS else None

    def allows(self, evidence) -> bool:
        classification = self.classify(evidence.claim_type)
        return (classification is not None and self.allows_class(classification)
                and evidence.claim_type not in self.denied_claims and structured_value_allowed(evidence))


class MandatoryContextFactPolicy:
    claims = frozenset({
        C.REQUEST_SENT, C.PAYMENT_FINALITY, C.PAYMENT_TRANSACTION_ID, C.TRANSACTION_FUND_REQUEST_ID,
        C.PAYMENT_AMOUNT, C.PAYMENT_CURRENCY, C.PAYMENT_CUSTOMER_REF, C.PAYMENT_BENEFICIARY_REF,
        C.PAYMENT_ACCOUNT_REF, C.HTTP_RESPONSE_STATUS, C.FUND_BUSINESS_STATUS,
        C.GUARANTEE_STATUS, C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS,
    })
