from .models import RegistryStatus as S, PartnerRole, ResolutionCode, RegistryError


def matches(scope, route):
    if (scope.tenant_id != route.tenant_id or scope.business_domain != route.business_domain
            or scope.environment != route.environment or route.product_code not in scope.product_codes):
        return False
    partner = {PartnerRole.ASSET: route.asset_partner, PartnerRole.FUNDING: route.funding_partner,
               PartnerRole.GUARANTEE: route.guarantee_partner}.get(scope.partner_role)
    if scope.partner_role is not None and partner != scope.partner_id:
        return False
    return scope.version_agnostic or route.protocol_version in scope.protocol_versions


def eligible(definition, case, route):
    if not matches(definition.scope, route):
        return False
    if definition.status == S.DISABLED:
        return False
    if definition.status == S.DRAINING and not (
            route.allow_existing_draining and definition.draining_since is not None
            and case.created_at < definition.draining_since):
        return False
    return (definition.effective_from <= route.effective_at and
            (definition.effective_until is None or route.effective_at < definition.effective_until))


def validate_route(case, route):
    if case.case_id != route.case_id or case.tenant_id != route.tenant_id:
        raise RegistryError(ResolutionCode.INVALID_ROUTE_CONTEXT)
