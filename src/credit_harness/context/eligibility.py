"""Positive structured-field allowlist, not a regex PII detector."""
from typing import ClassVar
from pydantic import ValidationError

from credit_harness.evidence.models import ClaimType as C
from credit_harness.domain.models import Model
from .models import InformationClass as I, LookupScope, CaseContextIdentifiers
from .compaction import fact
from .value_contracts import validate_claim_value


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
    try:
        validate_claim_value(e.claim_type, e.value)
        return True
    except ValueError:
        return False


class ContextEligibilityPolicy(Model):
    # Deployment policy may narrow access, never enable raw/masked/oracle classes.
    denied_claims: frozenset[C] = frozenset()
    allowed_classes: ClassVar[frozenset[I]] = frozenset({I.BUSINESS, I.TOKENIZED_IDENTITY, I.INTERNAL_CONTROL})

    def allows_class(self, classification: I) -> bool:
        return classification in self.allowed_classes

    def classify(self, claim: C) -> I | None:
        return I.TOKENIZED_IDENTITY if claim in TOKEN_CLAIMS else I.BUSINESS if claim in BUSINESS_CLAIMS else None

    def validate_case(self, case):
        try:
            CaseContextIdentifiers(case_id=case.case_id, internal_order_id=case.internal_order_id)
        except ValidationError:
            raise ContextEligibilityError("case identifiers are ineligible for Context") from None

    def allows(self, evidence) -> bool:
        classification = self.classify(evidence.claim_type)
        if classification is None or not self.allows_class(classification) or evidence.claim_type in self.denied_claims:
            return False
        try:
            # Same typed capsule and lookup contracts used by final envelope validation.
            fact(evidence)
            LookupScope(**evidence.metadata.scope.model_dump())
            return True
        except ValueError:
            return False


class MandatoryContextFactPolicy:
    claims = frozenset({
        C.REQUEST_SENT, C.PAYMENT_FINALITY, C.PAYMENT_TRANSACTION_ID, C.TRANSACTION_FUND_REQUEST_ID,
        C.PAYMENT_AMOUNT, C.PAYMENT_CURRENCY, C.PAYMENT_CUSTOMER_REF, C.PAYMENT_BENEFICIARY_REF,
        C.PAYMENT_ACCOUNT_REF, C.HTTP_RESPONSE_STATUS, C.FUND_BUSINESS_STATUS,
        C.GUARANTEE_STATUS, C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS,
    })
