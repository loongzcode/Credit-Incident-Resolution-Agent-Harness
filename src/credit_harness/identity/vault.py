"""Synthetic server-private boundary. Never imported by API, Evidence or reasoning.

No reveal endpoint; this is not an IAM or cryptographic vault implementation.
"""
from pydantic import ConfigDict, SecretStr, field_validator

from credit_harness.domain.identity import AccountRef, BeneficiaryRef, CustomerRef
from credit_harness.domain.models import Model


class SyntheticIdentityRecord(Model):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    customer_ref: CustomerRef
    beneficiary_ref: BeneficiaryRef
    account_ref: AccountRef
    full_name: SecretStr
    id_card: SecretStr
    bank_card: SecretStr
    mobile: SecretStr

    @field_validator("full_name", "id_card", "bank_card", "mobile")
    @classmethod
    def synthetic_only(cls, value):
        if not value.get_secret_value().startswith("TEST-"):
            raise ValueError("only explicitly synthetic TEST- values allowed")
        return value


class SyntheticIdentityVault:
    """Internal fixture container. Public code has neither a handle nor a raw DTO."""
    def __init__(self, records: tuple[SyntheticIdentityRecord, ...]):
        self.__records = {r.customer_ref: r for r in records}

    def contains(self, customer_ref: str) -> bool:
        return customer_ref in self.__records


def synthetic_identity_fixture() -> SyntheticIdentityRecord:
    return SyntheticIdentityRecord(
        customer_ref="CUS-JD-001", beneficiary_ref="BEN-JD-001", account_ref="ACC-JD-001",
        full_name="TEST-PERSON-0001", id_card="TEST-ID-0001",
        bank_card="TEST-CARD-0001", mobile="TEST-MOBILE-0001",
    )
