"""Explicit deployment fixtures: entirely synthetic, never inferred from Scenario/Oracle."""
from datetime import datetime, timezone
from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from .models import (SystemType as S, CapabilityType as K, AuthorityLevel as A, BusinessDomain,
    Environment, PartnerRole, RoutingScope, SystemDefinition, CapabilityDefinition, ClaimAuthorityRule,
    RegistryDefinition, CaseRouteContext, RecoveryContract, SourceChannel)

TOOL_SYSTEMS = {
    T.PAYMENT: (S.PAYMENT_STATUS_SOURCE, K.ESTABLISH_PAYMENT_FINALITY),
    T.FUND: (S.FUNDING_INTEGRATION, K.READ_FUND_STATE), T.LOAN_NOTE: (S.FUNDING_INTEGRATION, K.READ_FUND_STATE),
    T.GUARANTEE: (S.GUARANTEE_CORE, K.READ_GUARANTEE_STATE),
    T.TRACE: (S.GUARANTEE_CORE, K.ESTABLISH_REQUEST_ASSOCIATION),
    T.CALLBACK: (S.CALLBACK_GATEWAY, K.READ_CALLBACK_RECEIPT),
    T.CALLBACK_RAW: (S.CALLBACK_GATEWAY, K.READ_CALLBACK_RECEIPT),
    T.MESSAGES: (S.MESSAGE_PLATFORM, K.READ_MESSAGE_CONSUMPTION),
    T.ASSET: (S.ASSET_PLATFORM, K.READ_ASSET_STATE),
    T.ASSET_DELIVERY: (S.ASSET_PLATFORM, K.READ_ASSET_DELIVERY),
    T.ACCOUNTING: (S.ACCOUNTING, K.READ_ACCOUNTING_ENTRY),
    T.PROTOCOL: (S.PROTOCOL_REGISTRY, K.READ_PROTOCOL_SCHEMA),
}


def synthetic_registry(tenant_id="demo", *, partner="SUSHANG", product="CONSUMER_LOAN"):
    systems, capabilities, rules = [], [], []
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for tool in ToolCapabilityCatalog.entries:
        system_type, capability_type = TOOL_SYSTEMS[tool.tool_name]
        key = tool.tool_name.name.lower()
        # Funding/asset official integration, versus our own core and event pipeline.
        funding = tool.tool_name in (T.PAYMENT, T.FUND, T.LOAN_NOTE)
        asset = tool.tool_name in (T.ASSET, T.ASSET_DELIVERY)
        role = PartnerRole.FUNDING if funding else PartnerRole.ASSET if asset else None
        partner_ref = partner if funding else "JD" if asset else None
        suffix = partner_ref.lower() if partner_ref else "internal"
        channel = SourceChannel.PARTNER_OFFICIAL_API if funding or asset else SourceChannel.INTERNAL_SYSTEM
        display = ("我司资金接入系统 / 苏商官方接口（synthetic）" if tool.tool_name in (T.FUND, T.LOAN_NOTE) else
                   "合法授权的支付结果来源（synthetic official API）" if tool.tool_name == T.PAYMENT else
                   "JD-like asset integration（synthetic）" if asset else f"我司内部 {key}（synthetic）")
        scope = RoutingScope(tenant_id=tenant_id, business_domain=BusinessDomain.PERSONAL_CREDIT,
            environment=Environment.SIMULATOR, partner_role=role, partner_id=partner_ref,
            product_codes=(product,), protocol_versions=("2.3",) if tool.tool_name == T.MESSAGES else (),
            version_agnostic=tool.tool_name != T.MESSAGES)
        systems.append(SystemDefinition(system_id=f"sim-{key}-{suffix}", display_name=display,
            system_type=system_type, source_channel=channel, scope=scope, owner_team="synthetic-support", effective_from=since,
            credential_ref=f"vault://synthetic/{key}"))
        c = CapabilityDefinition(capability_id=f"cap-{key}", system_id=systems[-1].system_id,
            capability_type=capability_type, tool_name=tool.tool_name, produces_claim_types=tool.produces_claim_types,
            contributes_requirements=tool.contributes_requirements, scope=scope, authority_level=A.AUTHORITATIVE,
            adapter_id=f"synthetic-{key}-v1", effective_from=since,
            recovery=RecoveryContract(supports_status_lookup=tool.tool_name in (T.MESSAGES, T.ASSET_DELIVERY)))
        capabilities.append(c)
        rules.extend(ClaimAuthorityRule(capability_id=c.capability_id, claim_type=claim,
            authority_level=A.DIAGNOSTIC if claim == C.SOURCE_LOOKUP_STATUS else A.AUTHORITATIVE,
            identity_binding_required=tool.tool_name == T.PAYMENT) for claim in c.produces_claim_types)
    return RegistryDefinition(tenant_id=tenant_id, systems=tuple(systems), capabilities=tuple(capabilities),
                              authority_rules=tuple(rules))


def synthetic_route(case, **changes):
    return CaseRouteContext(case_id=case.case_id, tenant_id=case.tenant_id,
        business_domain=BusinessDomain.PERSONAL_CREDIT, environment=Environment.SIMULATOR,
        funding_partner="SUSHANG", asset_partner="JD", product_code="CONSUMER_LOAN", protocol_version="2.3",
        effective_at=case.created_at).model_copy(update=changes)
