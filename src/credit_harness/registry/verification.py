"""Registry path is authoritative for routing; static fallback is migration-only."""
from credit_harness.evaluation.models import VerificationRequirement as Q
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.models import UncollectedClaimType as U
from .models import RegistryError

REQUIREMENTS = {
    Q.PAYMENT_FINALITY: C.PAYMENT_FINALITY, Q.PAYMENT_IDENTITY: C.PAYMENT_ACCOUNT_REF,
    Q.REQUEST_ASSOCIATION: U.REQUEST_ASSOCIATION, Q.FUND_FINAL_STATE: C.FUND_BUSINESS_STATUS,
    Q.GUARANTEE_FINAL_STATE: C.GUARANTEE_STATUS, Q.ASSET_FINAL_STATE: C.ASSET_STATUS,
    Q.ACCOUNTING_ENTRY: C.ACCOUNTING_ENTRY_PRESENT, Q.CALLBACK_CONSUMPTION: C.MESSAGE_CONSUME_STATUS,
    Q.POST_EFFECT_MESSAGE_STATUS: C.MESSAGE_CONSUME_STATUS,
    Q.ASSET_DELIVERY: C.ASSET_DELIVERY_STATUS, Q.POST_EFFECT_DELIVERY_STATUS: C.ASSET_DELIVERY_STATUS,
}


def verification_tools(case, *, resolver=None, session=None):
    if resolver is None:
        from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
        from credit_harness.orchestration.routing import VERIFICATION_TOOLS
        allowed = {t.tool_name for t in ToolCapabilityCatalog().for_case(case)}
        return {q: t for q, t in VERIFICATION_TOOLS.items() if t in allowed}
    route = resolver.repository.route(case.case_id, session)
    result = {}
    for q, claim in REQUIREMENTS.items():
        try:
            resolved = resolver.resolve(case, claim, route, session=session)
            # Execution by ToolName must independently resolve the same source.
            if resolver.resolve_tool(case, resolved.tool_name, route, session=session) == resolved:
                result[q] = resolved.tool_name
        except RegistryError:
            continue
    return result
