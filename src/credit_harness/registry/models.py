from enum import StrEnum
from typing import Annotated
from pydantic import AwareDatetime, Field, model_validator
from credit_harness.domain.models import Model
from credit_harness.domain.enums import ToolName, Freshness, Completeness
from credit_harness.evidence.models import ClaimType
from credit_harness.hypotheses.models import UncollectedClaimType

Ref = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")]
Version = Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SystemType(StrEnum):
    PAYMENT_STATUS_SOURCE = "PAYMENT_STATUS_SOURCE"
    FUNDING_INTEGRATION = "FUNDING_INTEGRATION"
    GUARANTEE_CORE = "GUARANTEE_CORE"
    CALLBACK_GATEWAY = "CALLBACK_GATEWAY"
    MESSAGE_PLATFORM = "MESSAGE_PLATFORM"
    ASSET_PLATFORM = "ASSET_PLATFORM"
    ACCOUNTING = "ACCOUNTING"
    PROTOCOL_REGISTRY = "PROTOCOL_REGISTRY"


class BusinessDomain(StrEnum):
    PERSONAL_CREDIT = "PERSONAL_CREDIT"


class SourceChannel(StrEnum):
    INTERNAL_SYSTEM = "INTERNAL_SYSTEM"
    INTERNAL_SETTLEMENT = "INTERNAL_SETTLEMENT"
    PARTNER_OFFICIAL_API = "PARTNER_OFFICIAL_API"
    RECONCILIATION_FILE = "RECONCILIATION_FILE"
    PAYMENT_INSTITUTION_API = "PAYMENT_INSTITUTION_API"


class Environment(StrEnum):
    SIMULATOR = "SIMULATOR"
    TEST = "TEST"
    PROD = "PROD"


class PartnerRole(StrEnum):
    ASSET = "ASSET"
    FUNDING = "FUNDING"
    GUARANTEE = "GUARANTEE"


class RegistryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    DISABLED = "DISABLED"


class CapabilityType(StrEnum):
    ESTABLISH_PAYMENT_FINALITY = "ESTABLISH_PAYMENT_FINALITY"
    ESTABLISH_PAYMENT_IDENTITY = "ESTABLISH_PAYMENT_IDENTITY"
    ESTABLISH_REQUEST_ASSOCIATION = "ESTABLISH_REQUEST_ASSOCIATION"
    READ_FUND_STATE = "READ_FUND_STATE"
    READ_GUARANTEE_STATE = "READ_GUARANTEE_STATE"
    READ_CALLBACK_RECEIPT = "READ_CALLBACK_RECEIPT"
    READ_MESSAGE_CONSUMPTION = "READ_MESSAGE_CONSUMPTION"
    READ_ASSET_STATE = "READ_ASSET_STATE"
    READ_ASSET_DELIVERY = "READ_ASSET_DELIVERY"
    READ_ACCOUNTING_ENTRY = "READ_ACCOUNTING_ENTRY"
    READ_PROTOCOL_SCHEMA = "READ_PROTOCOL_SCHEMA"


class AccessMode(StrEnum):
    READ = "READ"
    WRITE = "WRITE"  # metadata only; never projected into investigation tools


class AuthorityLevel(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    CORROBORATING = "CORROBORATING"
    DIAGNOSTIC = "DIAGNOSTIC"


class RegistryEvent(StrEnum):
    REGISTERED = "REGISTERED"
    ACTIVATED = "ACTIVATED"
    DRAINING = "DRAINING"
    DISABLED = "DISABLED"
    RETIRED = "RETIRED"


class ResolutionCode(StrEnum):
    NO_REGISTERED_SOURCE = "NO_REGISTERED_SOURCE"
    NO_REGISTERED_SOURCE_FOR_PAYMENT_FINALITY = "NO_REGISTERED_SOURCE_FOR_PAYMENT_FINALITY"
    AMBIGUOUS_AUTHORITATIVE_SOURCE = "AMBIGUOUS_AUTHORITATIVE_SOURCE"
    AMBIGUOUS_SOURCE = "AMBIGUOUS_SOURCE"
    STALE_CAPABILITY = "STALE_CAPABILITY"
    INVALID_ROUTE_CONTEXT = "INVALID_ROUTE_CONTEXT"
    INVALID_ROUTING_PROOF = "INVALID_ROUTING_PROOF"
    ROUTING_FACT_CONFLICT = "ROUTING_FACT_CONFLICT"
    STALE_ROUTE_REVISION = "STALE_ROUTE_REVISION"


class RegistryError(ValueError):
    def __init__(self, code):
        self.code = ResolutionCode(code)
        super().__init__(self.code.value)


class EffectiveDefinition(Model):
    effective_from: AwareDatetime
    effective_until: AwareDatetime | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("invalid effective interval")
        return self


class RoutingScope(Model):
    tenant_id: Ref
    business_domain: BusinessDomain
    environment: Environment
    partner_role: PartnerRole | None = None
    partner_id: Ref | None = None
    product_codes: tuple[Ref, ...] = Field(min_length=1)
    protocol_versions: tuple[Version, ...] = ()
    version_agnostic: bool = False

    @model_validator(mode="after")
    def explicit_protocol(self):
        if bool(self.protocol_versions) == self.version_agnostic:
            raise ValueError("declare supported versions or explicit version-agnostic scope")
        if (self.partner_role is None) != (self.partner_id is None):
            raise ValueError("partner role and id must be paired")
        return self


class SystemDefinition(EffectiveDefinition):
    system_id: Ref
    display_name: Annotated[str, Field(min_length=1, max_length=120)]
    system_type: SystemType
    source_channel: SourceChannel
    scope: RoutingScope
    status: RegistryStatus = RegistryStatus.ACTIVE
    owner_team: Ref
    # A server-side reference only. No credential contents or endpoint field.
    credential_ref: Annotated[str, Field(pattern=r"^vault://[A-Za-z0-9/_-]+$")] | None = None
    draining_since: AwareDatetime | None = None

    @model_validator(mode="after")
    def drain_date(self):
        if self.status == RegistryStatus.DRAINING and self.draining_since is None:
            raise ValueError("draining requires explicit cutoff")
        return self


class RecoveryContract(Model):
    supports_status_lookup: bool = False
    not_found_proves_no_effect: bool = False
    resolver_contract_version: Version = "1"


class CapabilityDefinition(EffectiveDefinition):
    capability_id: Ref
    system_id: Ref
    capability_type: CapabilityType
    tool_name: ToolName
    read_or_write: AccessMode = AccessMode.READ
    produces_claim_types: tuple[ClaimType, ...] = Field(min_length=1)
    contributes_requirements: tuple[UncollectedClaimType, ...] = ()
    supports_lookup: bool = True
    authority_level: AuthorityLevel
    scope: RoutingScope
    query_contract_version: Version = "1"
    response_contract_version: Version = "1"
    adapter_id: Ref
    status: RegistryStatus = RegistryStatus.ACTIVE
    draining_since: AwareDatetime | None = None
    recovery: RecoveryContract = RecoveryContract()

    @model_validator(mode="after")
    def drain_date(self):
        if self.status == RegistryStatus.DRAINING and self.draining_since is None:
            raise ValueError("draining requires explicit cutoff")
        return self


class ClaimAuthorityRule(Model):
    claim_type: ClaimType
    capability_id: Ref
    authority_level: AuthorityLevel
    subject_binding_required: bool = True
    identity_binding_required: bool = False
    freshness_requirement: Freshness = Freshness.CURRENT
    completeness_requirement: Completeness = Completeness.COMPLETE


class CaseRouteContext(Model):
    case_id: Ref
    tenant_id: Ref
    business_domain: BusinessDomain
    asset_partner: Ref | None = None
    funding_partner: Ref | None = None
    guarantee_partner: Ref | None = None
    product_code: Ref | None = None
    protocol_version: Version | None = None
    environment: Environment
    effective_at: AwareDatetime
    allow_existing_draining: bool = False


class RegistryDefinition(Model):
    tenant_id: Ref
    systems: tuple[SystemDefinition, ...]
    capabilities: tuple[CapabilityDefinition, ...]
    authority_rules: tuple[ClaimAuthorityRule, ...]

    @model_validator(mode="after")
    def integrity(self):
        systems = {s.system_id: s for s in self.systems}
        caps = {c.capability_id: c for c in self.capabilities}
        rules = {(r.capability_id, r.claim_type): r for r in self.authority_rules}
        if len(systems) != len(self.systems) or len(caps) != len(self.capabilities) or len(rules) != len(self.authority_rules):
            raise ValueError("duplicate registry definition")
        for s in self.systems:
            if s.scope.tenant_id != self.tenant_id:
                raise ValueError("foreign system tenant")
        for c in self.capabilities:
            if c.system_id not in systems or c.scope.tenant_id != self.tenant_id:
                raise ValueError("unbound capability")
            if any((c.capability_id, claim) not in rules for claim in c.produces_claim_types):
                raise ValueError("every produced claim requires explicit authority")
        for r in self.authority_rules:
            c = caps.get(r.capability_id)
            if c is None or r.claim_type not in c.produces_claim_types:
                raise ValueError("authority cannot invent evidence claims")
            if (r.claim_type == ClaimType.PAYMENT_FINALITY and r.authority_level == AuthorityLevel.AUTHORITATIVE
                    and systems[c.system_id].system_type != SystemType.PAYMENT_STATUS_SOURCE):
                raise ValueError("only a registered payment status source may authoritatively observe payment finality")
            if (r.claim_type == ClaimType.PAYMENT_FINALITY and r.authority_level == AuthorityLevel.AUTHORITATIVE
                    and not (r.subject_binding_required and r.identity_binding_required
                             and r.freshness_requirement == Freshness.CURRENT
                             and r.completeness_requirement == Completeness.COMPLETE)):
                raise ValueError("authoritative payment finality requires subject, identity, current and complete evidence")
        return self


class ResolvedCapability(Model):
    case_id: Ref
    capability_id: Ref
    system_id: Ref
    adapter_id: Ref
    tool_name: ToolName
    claim_types: tuple[ClaimType, ...]
    authority: tuple[ClaimAuthorityRule, ...]
    registry_version: Hash
    routing_fingerprint: Hash
    query_contract_version: Version
    response_contract_version: Version


class VisibleAuthority(Model):
    claim_type: ClaimType
    authority_level: AuthorityLevel
    subject_binding_required: bool
    identity_binding_required: bool
    freshness_requirement: Freshness
    completeness_requirement: Completeness


class AvailableCapability(Model):
    capability_type: CapabilityType
    tool_name: ToolName
    claim_types: tuple[ClaimType, ...]
    authority: tuple[VisibleAuthority, ...]
    cost_class: str = Field(default="LOW", pattern=r"^(LOW|MEDIUM)$")
    latency_class: str = Field(default="LOW", pattern=r"^(LOW|MEDIUM)$")


class CaseCapabilitySnapshot(Model):
    snapshot_id: Hash
    case_id: Ref
    registry_version: Hash
    routing_context_fingerprint: Hash
    available_capabilities: tuple[AvailableCapability, ...]
    assembled_at: AwareDatetime


class DispatchSource(Model):
    """Runtime-only provenance; linked to CaseCall, never evidence of business success."""
    call_id: str
    case_id: Ref
    resolved: ResolvedCapability


class RouteChangeReason(StrEnum):
    INITIAL_BINDING = "INITIAL_BINDING"
    LEGACY_IMPORT = "LEGACY_IMPORT"
    PROTOCOL_VERIFIED = "PROTOCOL_VERIFIED"


class RouteRevisionStatus(StrEnum):
    TRUSTED_INITIAL = "TRUSTED_INITIAL"
    VERIFIED = "VERIFIED"


class CaseRouteRevision(Model):
    route_revision_id: Hash
    case_id: Ref
    tenant_id: Ref
    parent_revision_id: Hash | None
    route_context: CaseRouteContext
    routing_fingerprint: Hash
    change_reason: RouteChangeReason
    supporting_evidence_refs: tuple[Annotated[str, Field(pattern=r"^E-[0-9a-f]{64}$")], ...]
    created_at: AwareDatetime
    created_by: Ref
    status: RouteRevisionStatus

    @model_validator(mode="after")
    def revision_binding(self):
        if self.case_id != self.route_context.case_id or self.tenant_id != self.route_context.tenant_id:
            raise ValueError("revision scope mismatch")
        if self.change_reason == RouteChangeReason.PROTOCOL_VERIFIED:
            if (not self.supporting_evidence_refs or self.parent_revision_id is None
                    or self.route_context.protocol_version is None or self.status != RouteRevisionStatus.VERIFIED):
                raise ValueError("protocol revision requires proof and parent")
        elif self.parent_revision_id is not None or self.supporting_evidence_refs or self.status != RouteRevisionStatus.TRUSTED_INITIAL:
            raise ValueError("initial route cannot impersonate evidence promotion")
        return self
