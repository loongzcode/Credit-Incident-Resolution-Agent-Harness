"""Source eligibility is not a payment conclusion, and never modifies Evidence strength."""
from .models import AuthorityLevel as A

AUTHORITY_ORDER = {A.AUTHORITATIVE: 0, A.CORROBORATING: 1, A.DIAGNOSTIC: 2}


def rules_for(definition, capability):
    return tuple(sorted((r for r in definition.authority_rules if r.capability_id == capability.capability_id),
                        key=lambda r: r.claim_type.value))


def can_support(rule, evidence, *, subject_bound, identity_match):
    """Deterministic eligibility only. MATCH must come from the existing identity service."""
    from credit_harness.identity.models import IdentityMatch
    return (evidence.claim_type == rule.claim_type
            and (not rule.subject_binding_required or subject_bound)
            and (not rule.identity_binding_required or identity_match == IdentityMatch.MATCH)
            and evidence.freshness == rule.freshness_requirement
            and evidence.completeness == rule.completeness_requirement)
