from enum import StrEnum
from typing import Protocol
from credit_harness.registry_admin.identity import EnterpriseIdentity, OIDCIdentityProvider


class InvestigationIdentityProvider(Protocol):
    def authenticate(self, bearer: str) -> EnterpriseIdentity: ...


class InvestigationRole(StrEnum):
    VIEWER = "VIEWER"
    INVESTIGATOR = "INVESTIGATOR"
    FINANCIAL_REVIEWER = "FINANCIAL_REVIEWER"
    SUPERVISOR = "SUPERVISOR"


ROLE_PERMISSIONS = {
    InvestigationRole.VIEWER: frozenset({"CASE_VIEW"}),
    InvestigationRole.INVESTIGATOR: frozenset({"CASE_VIEW", "CASE_TRACE_VIEW"}),
    InvestigationRole.FINANCIAL_REVIEWER: frozenset({"CASE_VIEW", "CASE_FINANCIAL_VIEW"}),
    InvestigationRole.SUPERVISOR: frozenset({"CASE_VIEW", "CASE_TRACE_VIEW", "CASE_FINANCIAL_VIEW"}),
}


class InvestigationAccess:
    def __init__(self, group_roles):
        self.mapping = {group: tuple(InvestigationRole(r) for r in roles) for group, roles in group_roles.items()}

    def permissions(self, identity):
        return frozenset(p for group in identity.groups for role in self.mapping.get(group, ()) for p in ROLE_PERMISSIONS[role])


class OIDCInvestigationIdentityProvider(OIDCIdentityProvider):
    def __init__(self, *, issuer, audience, jwks_url):
        super().__init__(issuer=issuer, audience=audience, jwks_url=jwks_url, required_scope="case.investigate")
