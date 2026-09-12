from credit_harness.evidence.models import ClaimType
from .models import (AccessMode, ResolvedCapability, RegistryError, ResolutionCode as R,
                     AuthorityLevel as A)
from .repository import fingerprint
from .routing import eligible, validate_route
from .authority import rules_for, AUTHORITY_ORDER


class CapabilityResolver:
    def __init__(self, repository):
        self.repository = repository

    def candidates(self, case, route, *, session=None):
        validate_route(case, route)
        if case.tenant_id != self.repository.tenant_id:
            raise RegistryError(R.INVALID_ROUTE_CONTEXT)
        version, definition = self.repository.current(session)
        systems = {s.system_id: s for s in definition.systems}
        caps = tuple(c for c in definition.capabilities
            if c.read_or_write == AccessMode.READ and c.supports_lookup
            and c.tool_name in case.scope.allowed_tools
            and c.tool_name.value not in case.constraints.forbidden_actions
            and eligible(c, case, route) and eligible(systems[c.system_id], case, route))
        return version, definition, caps

    def _resolved(self, case, route, version, definition, c):
        return ResolvedCapability(case_id=case.case_id, capability_id=c.capability_id,
            system_id=c.system_id, adapter_id=c.adapter_id, tool_name=c.tool_name,
            claim_types=c.produces_claim_types, authority=rules_for(definition, c),
            registry_version=version, routing_fingerprint=fingerprint(route),
            query_contract_version=c.query_contract_version, response_contract_version=c.response_contract_version)

    def resolve(self, case, requirement, route, *, session=None):
        version, definition, caps = self.candidates(case, route, session=session)
        entries = []
        for c in caps:
            rules = rules_for(definition, c)
            if isinstance(requirement, ClaimType):
                rule = next((r for r in rules if r.claim_type == requirement), None)
                if rule:
                    entries.append((AUTHORITY_ORDER[rule.authority_level], c))
            elif requirement in c.contributes_requirements:
                entries.append((AUTHORITY_ORDER[c.authority_level], c))
        if not entries:
            raise RegistryError(R.NO_REGISTERED_SOURCE_FOR_PAYMENT_FINALITY
                                if requirement == ClaimType.PAYMENT_FINALITY else R.NO_REGISTERED_SOURCE)
        rank = min(i for i, _ in entries)
        best = [c for i, c in entries if i == rank]
        if len(best) != 1:
            raise RegistryError(R.AMBIGUOUS_AUTHORITATIVE_SOURCE if rank == 0 else R.AMBIGUOUS_SOURCE)
        return self._resolved(case, route, version, definition, best[0])

    def resolve_tool(self, case, tool, route, *, session=None):
        version, definition, caps = self.candidates(case, route, session=session)
        matches = [c for c in caps if c.tool_name == tool]
        if not matches:
            raise RegistryError(R.NO_REGISTERED_SOURCE)
        # ToolName cannot represent which of two source instances was intended.
        # Fail closed even if one source has a lower diagnostic rank.
        if len(matches) != 1:
            raise RegistryError(R.AMBIGUOUS_AUTHORITATIVE_SOURCE)
        return self._resolved(case, route, version, definition, matches[0])

    def revalidate(self, case, resolved, *, session=None):
        route = self.repository.route(case.case_id, session)
        try:
            current = self.resolve_tool(case, resolved.tool_name, route, session=session)
        except RegistryError:
            raise RegistryError(R.STALE_CAPABILITY) from None
        if current != resolved:
            raise RegistryError(R.STALE_CAPABILITY)
        return current
