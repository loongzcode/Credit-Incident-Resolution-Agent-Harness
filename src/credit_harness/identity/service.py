from typing import Protocol

from .models import IdentityMatch


class IdentityVerificationService(Protocol):
    def verify_customer(self, expected: str | None, observed: str | None) -> IdentityMatch: ...
    def verify_beneficiary(self, expected: str | None, observed: str | None) -> IdentityMatch: ...
    def verify_account(self, expected: str | None, observed: str | None) -> IdentityMatch: ...


class ReferenceIdentityVerificationService:
    """Exact reference equality only; no name matching, vault access or Tool registration."""
    @staticmethod
    def _compare(expected, observed):
        if expected is None or observed is None:
            return IdentityMatch.UNKNOWN
        return IdentityMatch.MATCH if expected == observed else IdentityMatch.MISMATCH

    verify_customer = _compare
    verify_beneficiary = _compare
    verify_account = _compare
