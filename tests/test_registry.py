from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_case_evidence import harness, Q
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.cases.preconditions import AgentPreconditionFailed
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.planner.renderer import ModelInputRenderer
from credit_harness.agent.revalidation import execution_precondition
from credit_harness.registry.models import *
from credit_harness.registry.fixtures import synthetic_registry, synthetic_route
from credit_harness.registry.repository import RegistryRepository, RegistryAdmin, fingerprint
from credit_harness.registry.resolver import CapabilityResolver
from credit_harness.registry.snapshot import RegistryBackedCatalog
from credit_harness.registry.adapters import TrustedAdapterResolver
from credit_harness.registry.dispatch import RegistryDispatchGuard
from credit_harness.registry.tables import RegistryAuditRow, DispatchSourceRow
from credit_harness.registry.verification import verification_tools


@pytest.fixture
def registered(harness):
    cases, evidence, executor, *_ = harness()
    case = cases.get("CASE-JD202609100001")
    repo = RegistryRepository(cases.engine, case.tenant_id)
    admin = RegistryAdmin(repo)
    definition = synthetic_registry(case.tenant_id)
    version = admin.register(definition, actor="fixture-admin")
    admin.activate(version, expected_version=None, actor="fixture-admin")
    route = synthetic_route(case)
    admin.bind_case_route(cases, route, actor="routing-admin")
    resolver = CapabilityResolver(repo)
    bindings = {(c.system_id, c.adapter_id, c.query_contract_version, c.response_contract_version):
                executor._client_for_case for c in definition.capabilities}
    cases.registry_guard = RegistryDispatchGuard(resolver, TrustedAdapterResolver(bindings))
    return SimpleNamespace(cases=cases, case=case, evidence=evidence, executor=executor, repo=repo, admin=admin,
        definition=definition, version=version, route=route, resolver=resolver, catalog=RegistryBackedCatalog(resolver))


def payment(x, **route_changes):
    return x.resolver.resolve(x.case, C.PAYMENT_FINALITY, x.route.model_copy(update=route_changes))


def publish(x, *, capabilities=None, systems=None):
    changes = {}
    if capabilities is not None:
        changes["capabilities"] = tuple(capabilities)
        ids = {c.capability_id for c in capabilities}
        changes["authority_rules"] = tuple(r for r in x.definition.authority_rules if r.capability_id in ids)
    if systems is not None:
        changes["systems"] = tuple(systems)
    definition = x.definition.model_copy(update=changes)
    version = x.admin.register(definition, actor="test-admin")
    x.admin.activate(version, expected_version=x.repo.current()[0], actor="test-admin")
    return version


def test_case_sees_only_scoped_capabilities(registered):
    x = registered
    case = x.case.model_copy(update=dict(scope=x.case.scope.model_copy(update=dict(allowed_tools=frozenset({T.PAYMENT})))))
    assert [c.tool_name for c in x.catalog.for_case(case)] == [T.PAYMENT]


def test_wrong_tenant_capability_hidden(registered):
    x = registered
    with pytest.raises(RegistryError):
        payment(x, tenant_id="other")


@pytest.mark.parametrize("field,value", [("funding_partner", "OTHER"), ("product_code", "OTHER"),
    ("environment", Environment.PROD), ("funding_partner", None), ("product_code", None)])
def test_wrong_partner_hidden(registered, field, value):
    with pytest.raises(RegistryError):
        payment(registered, **{field: value})


def test_wrong_product_hidden(registered):
    with pytest.raises(RegistryError):
        payment(registered, product_code="OTHER")


def test_wrong_protocol_hidden(registered):
    x = registered
    with pytest.raises(RegistryError):
        x.resolver.resolve(x.case, C.MESSAGE_CONSUME_STATUS, x.route.model_copy(update=dict(protocol_version="2.2")))


def test_future_capability_hidden(registered):
    x = registered
    publish(x, capabilities=[c.model_copy(update=dict(effective_from=x.route.effective_at+timedelta(days=30)))
                            if c.tool_name == T.PAYMENT else c for c in x.definition.capabilities])
    with pytest.raises(RegistryError):
        payment(x)


@pytest.mark.parametrize("target", ["system", "capability"])
def test_disabled_capability_hidden(registered, target):
    x = registered
    if target == "system":
        publish(x, systems=[s.model_copy(update=dict(status=RegistryStatus.DISABLED))
                           if s.system_type == SystemType.PAYMENT_STATUS_SOURCE else s for s in x.definition.systems])
    else:
        publish(x, capabilities=[c.model_copy(update=dict(status=RegistryStatus.DISABLED))
                                if c.tool_name == T.PAYMENT else c for c in x.definition.capabilities])
    with pytest.raises(RegistryError):
        payment(x)


def test_draining_policy(registered):
    x = registered
    cutoff = x.case.created_at + timedelta(days=1)
    publish(x, systems=[s.model_copy(update=dict(status=RegistryStatus.DRAINING, draining_since=cutoff))
                       if s.system_type == SystemType.PAYMENT_STATUS_SOURCE else s for s in x.definition.systems])
    with pytest.raises(RegistryError):
        payment(x)
    assert payment(x, allow_existing_draining=True).tool_name == T.PAYMENT
    late_case = x.case.model_copy(update=dict(created_at=cutoff))
    with pytest.raises(RegistryError):
        x.resolver.resolve(late_case, C.PAYMENT_FINALITY, x.route.model_copy(update=dict(allow_existing_draining=True)))


def test_authoritative_payment_source_selected(registered):
    r = payment(registered)
    assert r.system_id == "sim-payment-sushang"
    assert next(a for a in r.authority if a.claim_type == C.PAYMENT_FINALITY).authority_level == AuthorityLevel.AUTHORITATIVE


def test_fund_success_not_authoritative_payment_finality(registered):
    x = registered
    fund = x.resolver.resolve(x.case, C.FUND_BUSINESS_STATUS, x.route)
    assert C.PAYMENT_FINALITY not in fund.claim_types
    publish(x, capabilities=[c for c in x.definition.capabilities if c.tool_name != T.PAYMENT])
    with pytest.raises(RegistryError, match="NO_REGISTERED_SOURCE_FOR_PAYMENT_FINALITY"):
        payment(x)


def test_no_registered_source_escalates(registered):
    x = registered
    publish(x, capabilities=[c for c in x.definition.capabilities if c.tool_name != T.ACCOUNTING])
    from credit_harness.evaluation.models import VerificationRequirement as V
    assert V.ACCOUNTING_ENTRY not in verification_tools(x.case, resolver=x.resolver)
    with pytest.raises(RegistryError, match="NO_REGISTERED_SOURCE"):
        x.resolver.resolve(x.case, C.ACCOUNTING_ENTRY_PRESENT, x.route)


def test_ambiguous_authoritative_sources_fail_closed(registered):
    x = registered
    c = next(c for c in x.definition.capabilities if c.tool_name == T.PAYMENT)
    duplicate = c.model_copy(update=dict(capability_id="other-payment"))
    definition = x.definition.model_copy(update=dict(capabilities=(*x.definition.capabilities, duplicate),
        authority_rules=(*x.definition.authority_rules, *(r.model_copy(update=dict(capability_id=duplicate.capability_id))
            for r in x.definition.authority_rules if r.capability_id == c.capability_id))))
    v = x.admin.register(definition, actor="admin")
    x.admin.activate(v, expected_version=x.version, actor="admin")
    with pytest.raises(RegistryError, match="AMBIGUOUS_AUTHORITATIVE_SOURCE"):
        payment(x)
    assert T.PAYMENT not in {c.tool_name for c in x.catalog.for_case(x.case)}


def test_registry_snapshot_is_content_addressed(registered):
    x = registered
    a = x.catalog.project(x.case)[1]
    assert a == x.catalog.project(x.case)[1]
    assert a.snapshot_id == fingerprint(a.model_dump(mode="json", exclude={"snapshot_id"}))


def test_registry_change_makes_execution_stale(registered):
    x = registered
    snapshot = ReasoningContextAssembler(catalog=x.catalog).build(x.case, ())
    publish(x, capabilities=[c.model_copy(update=dict(status=RegistryStatus.DISABLED))
                            if c.tool_name == T.PAYMENT else c for c in x.definition.capabilities])
    with pytest.raises(AgentPreconditionFailed):
        x.executor.execute_if_current(x.case.case_id, T.PAYMENT, Q, precondition=execution_precondition(x.case, snapshot))
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 0


@pytest.mark.parametrize("field", ["credential_ref", "vault://", "https://", "system_id", "adapter_id", "api_key"])
def test_planner_never_sees_credentials(registered, field):
    x = registered
    snapshot = ReasoningContextAssembler(catalog=x.catalog).build(x.case, ())
    assert field not in snapshot.model_dump_json()
    bundle = ModelInputRenderer().render(snapshot)
    assert field not in bundle.model_dump_json()
    assert bundle.trusted_control.capability_snapshot.available_capabilities


def test_planner_never_sees_urls(registered):
    x = registered
    with pytest.raises(ValidationError):
        SystemDefinition.model_validate({**x.definition.systems[0].model_dump(), "url": "https://private.local"})


def test_registry_cannot_authorize_write(registered):
    x = registered
    publish(x, capabilities=[c.model_copy(update=dict(read_or_write=AccessMode.WRITE)) for c in x.definition.capabilities])
    assert x.catalog.for_case(x.case) == ()
    with pytest.raises(AgentPreconditionFailed):
        x.executor.execute(x.case.case_id, T.PAYMENT, Q)
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 0


def test_identity_requirement_uses_match_result_not_raw_pii(registered):
    x = registered
    result = x.executor.execute(x.case.case_id, T.PAYMENT, Q)
    from credit_harness.registry.authority import can_support
    from credit_harness.identity.models import IdentityMatch
    e = next(e for e in x.evidence.list(x.case.case_id) if e.claim_type == C.PAYMENT_FINALITY)
    rule = next(a for a in payment(x).authority if a.claim_type == C.PAYMENT_FINALITY)
    assert can_support(rule, e, subject_bound=True, identity_match=IdentityMatch.MATCH)
    for status in (IdentityMatch.UNKNOWN, IdentityMatch.MISMATCH):
        assert not can_support(rule, e, subject_bound=True, identity_match=status)
    assert "TEST-CARD" not in result.model_dump_json()


def test_verification_routes_through_registry(registered):
    x = registered
    from credit_harness.evaluation.models import VerificationRequirement as V
    assert verification_tools(x.case, resolver=x.resolver)[V.PAYMENT_FINALITY] == T.PAYMENT
    publish(x, capabilities=[c for c in x.definition.capabilities if c.tool_name != T.PAYMENT])
    assert V.PAYMENT_FINALITY not in verification_tools(x.case, resolver=x.resolver)
    assert verification_tools(x.case)[V.PAYMENT_FINALITY] == T.PAYMENT  # migration-only explicitly selected


def test_recovery_contract_metadata_available(registered):
    x = registered
    c = next(c for c in x.repo.current()[1].capabilities if c.tool_name == T.MESSAGES)
    assert c.recovery.supports_status_lookup
    assert not c.recovery.not_found_proves_no_effect
    assert c.recovery.resolver_contract_version == "1"


def test_effective_protocol_routing(registered):
    x = registered
    assert x.resolver.resolve(x.case, C.MESSAGE_CONSUME_STATUS, x.route).tool_name == T.MESSAGES
    unknown = x.route.model_copy(update=dict(protocol_version=None))
    assert x.resolver.resolve(x.case, C.PROTOCOL_FIELD_TYPE, unknown).tool_name == T.PROTOCOL
    with pytest.raises(RegistryError):
        x.resolver.resolve(x.case, C.MESSAGE_CONSUME_STATUS, unknown)


def test_concurrent_registry_version_activation(registered):
    x = registered
    versions = [x.admin.register(x.definition.model_copy(update=dict(capabilities=tuple(
        c.model_copy(update=dict(status=status)) for c in x.definition.capabilities))), actor="admin")
        for status in (RegistryStatus.ACTIVE, RegistryStatus.DISABLED)]
    # Two distinct newly registered versions, both competing for the same old pointer.
    versions[0] = x.admin.register(x.definition.model_copy(update=dict(capabilities=tuple(
        c.model_copy(update=dict(response_contract_version="2")) for c in x.definition.capabilities))), actor="admin")
    def activate(v):
        try:
            x.admin.activate(v, expected_version=x.version, actor="worker")
            return True
        except RegistryError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(activate, versions)) == [False, True]
    with Session(x.cases.engine) as s:
        rows = s.scalars(select(RegistryAuditRow).where(RegistryAuditRow.event == "ACTIVATED")).all()
        assert len(rows) == 2


def test_old_case_bound_snapshot_revalidation(registered):
    x = registered
    old = x.catalog.project(x.case)[1]
    resolved = payment(x)
    publish(x, capabilities=[c.model_copy(update=dict(adapter_id="replacement-v1"))
                            if c.tool_name == T.PAYMENT else c for c in x.definition.capabilities])
    with pytest.raises(RegistryError, match="STALE_CAPABILITY"):
        x.catalog.revalidate_snapshot(x.case, old)
    with pytest.raises(RegistryError, match="STALE_CAPABILITY"):
        x.resolver.revalidate(x.case, resolved)


def test_dispatch_source_provenance_is_bound_before_observation(registered):
    x = registered
    result = x.executor.execute(x.case.case_id, T.PAYMENT, Q)
    for ref in result.evidence_refs:
        sources = x.cases.registry_guard.evidence_sources(x.cases, ref)
        assert len(sources) == 1 and sources[0].call_id == result.call_id
        assert sources[0].resolved == payment(x)
    with Session(x.cases.engine) as s:
        assert s.get(DispatchSourceRow, result.call_id).tenant_id == x.case.tenant_id


def test_registered_case_cannot_bypass_guard(registered):
    x = registered
    x.cases.registry_guard = None
    with pytest.raises(AgentPreconditionFailed):
        x.executor.execute(x.case.case_id, T.PAYMENT, Q)


def test_registration_cannot_invent_tool_claims(registered):
    x = registered
    c = next(c for c in x.definition.capabilities if c.tool_name == T.FUND)
    changed = c.model_copy(update=dict(produces_claim_types=(*c.produces_claim_types, C.PAYMENT_FINALITY)))
    d = x.definition.model_copy(update=dict(capabilities=tuple(changed if a == c else a for a in x.definition.capabilities),
        authority_rules=(*x.definition.authority_rules, ClaimAuthorityRule(capability_id=c.capability_id,
            claim_type=C.PAYMENT_FINALITY, authority_level=AuthorityLevel.CORROBORATING))))
    with pytest.raises(ValueError):
        x.admin.register(d, actor="admin")
