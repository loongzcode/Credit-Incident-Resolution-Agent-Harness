from enum import StrEnum

from pydantic import AwareDatetime, Field

from credit_harness.domain.enums import Currency
from credit_harness.domain.identity import AccountRef, BeneficiaryRef, CustomerRef
from credit_harness.domain.models import Model, MinorAmount


class FinancialSubject(Model):
    expected_principal_minor: MinorAmount
    currency: Currency
    customer_ref: CustomerRef
    expected_beneficiary_ref: BeneficiaryRef
    expected_account_ref: AccountRef


class IdentityMatch(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNKNOWN = "UNKNOWN"


class IdentityDimension(StrEnum):
    FINALITY = "FINALITY"
    TRANSACTION = "TRANSACTION"
    REQUEST = "REQUEST"
    AMOUNT = "AMOUNT"
    CURRENCY = "CURRENCY"
    CUSTOMER = "CUSTOMER"
    BENEFICIARY = "BENEFICIARY"
    ACCOUNT = "ACCOUNT"


class PaymentIdentityWitness(Model):
    case_id: str
    observation_id: str
    transaction_ref: str
    result: IdentityMatch
    mismatch_dimensions: tuple[IdentityDimension, ...]
    unknown_dimensions: tuple[IdentityDimension, ...]
    evidence_refs: tuple[str, ...]


class PaymentIdentityResult(Model):
    case_id: str
    result: IdentityMatch
    mismatch_dimensions: tuple[IdentityDimension, ...]
    unknown_dimensions: tuple[IdentityDimension, ...]
    witnesses: tuple[PaymentIdentityWitness, ...]
    evidence_refs: tuple[str, ...]
    verification_version: str = "1"


class PIILevel(StrEnum):
    BUSINESS = "PII-A"
    TOKEN = "PII-B"
    MASKED = "PII-C"
    RAW = "PII-D"


class PIIField(StrEnum):
    FULL_NAME = "full_name"
    ID_CARD = "id_card"
    BANK_CARD = "bank_card"
    MOBILE = "mobile"


class PIIAccessPurpose(StrEnum):
    MANUAL_INCIDENT_INVESTIGATION = "MANUAL_INCIDENT_INVESTIGATION"


class PIIAccessIntent(Model):
    """Future policy input only: constructing this does NOT authorize access."""
    case_id: str = Field(min_length=1)
    purpose: PIIAccessPurpose
    subject_ref: CustomerRef | BeneficiaryRef | AccountRef
    allowed_fields: frozenset[PIIField] = Field(min_length=1)
    expires_at: AwareDatetime
    actor: str = Field(min_length=1)
    audit_id: str = Field(min_length=1)
