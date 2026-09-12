from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_case_evidence import harness, Q
from credit_harness.registry.models import *
from credit_harness.registry.repository import RegistryRepository, RegistryAdmin, fingerprint
from credit_harness.registry.fixtures import synthetic_registry, synthetic_route
from credit_harness.registry.resolver import CapabilityResolver
from credit_harness.registry.snapshot import RegistryBackedCatalog
from credit_harness.registry.adapters import TrustedAdapterResolver
from credit_harness.registry.dispatch import RegistryDispatchGuard
from credit_harness.registry.revisions import RouteUpdater
from credit_harness.registry.tables import (RouteContextRow, RouteRevisionRow, RouteHeadRow, DispatchSourceRow,
                                          RegistryVersionRow, RegistryHeadRow, SystemRow)
from credit_harness.domain.enums import ToolName as T, Freshness, Completeness
from credit_harness.evidence.models import ClaimType as C
from credit_harness.tools.contracts import ToolQuery
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.agent.revalidation import execution_precondition
from credit_harness.cases.preconditions import AgentPreconditionFailed


@pytest.fixture
def routed(harness):
    def create(case_id="CASE-ROUTE", tenant="demo"):
        cases, evidence, executor, *_ = harness(case_id=case_id, tenant=tenant)
        case = cases.get(case_id)
        repo = RegistryRepository(cases.engine, tenant)
        admin = RegistryAdmin(repo)
        definition = synthetic_registry(tenant)
        version = admin.register(definition, actor="fixture-admin")
        try:
            repo.current()
        except RegistryError:
            admin.activate(version, expected_version=None, actor="fixture-admin")
        admin.bind_case_route(cases, synthetic_route(case, protocol_version=None), actor="initial-routing")
        resolver = CapabilityResolver(repo)
        cases.registry_guard = RegistryDispatchGuard(resolver, TrustedAdapterResolver({
            (c.system_id, c.adapter_id, "1", "1"): executor._client_for_case for c in definition.capabilities}))
        return SimpleNamespace(cases=cases, case=case, evidence=evidence, executor=executor, repo=repo,
            admin=admin, resolver=resolver, definition=definition, version=version,
            catalog=RegistryBackedCatalog(resolver), updater=RouteUpdater(cases, resolver))
    return create


def protocol(x, version="2.3"):
    result = x.executor.execute(x.case.case_id, T.PROTOCOL,
        ToolQuery(internal_order_id=x.case.internal_order_id, protocol_version=version))
    return tuple(e.evidence_id for e in x.evidence.list(x.case.case_id)
                 if e.evidence_id in result.evidence_refs and e.claim_type == C.PROTOCOL_FIELD_TYPE)


def promote(x, refs, expected=None):
    return x.updater.promote_protocol(x.case.case_id, evidence_refs=refs,
        expected_revision_id=expected or x.repo.route_revision(x.case.case_id).route_revision_id)


def test_unknown_protocol_can_query_protocol_source(routed):
    x = routed()
    assert x.repo.route(x.case.case_id).protocol_version is None
    assert T.PROTOCOL in {t.tool_name for t in x.catalog.for_case(x.case)}
    assert protocol(x)


def test_verified_protocol_evidence_creates_route_revision(routed):
    x = routed(); initial = x.repo.route_revision(x.case.case_id)
    refs = protocol(x)
    before_case = x.cases.get(x.case.case_id)
    result = promote(x, refs)
    assert result.parent_revision_id == initial.route_revision_id
    assert result.route_context.protocol_version == "2.3"
    assert result.supporting_evidence_refs == tuple(sorted(refs))
    assert x.repo.route_history(x.case.case_id) == (initial, result)
    assert x.cases.get(x.case.case_id) == before_case  # no budget/lifecycle/Evidence mutation
    assert promote(x, refs) == result  # verified same fact is idempotent
    with Session(x.cases.engine) as s:
        assert s.get(RouteContextRow, x.case.case_id).payload["protocol_version"] is None


def test_route_revision_unlocks_message_capability(routed):
    x = routed()
    assert T.MESSAGES not in {t.tool_name for t in x.catalog.for_case(x.case)}
    promote(x, protocol(x))
    snapshot = ReasoningContextAssembler(catalog=x.catalog).build(x.cases.get(x.case.case_id), x.evidence.list(x.case.case_id))
    assert T.MESSAGES in {t.tool_name for t in snapshot.available_tools}
    assert x.executor.execute(x.case.case_id, T.MESSAGES, Q).evidence_refs


def test_old_snapshot_stales_after_route_revision(routed):
    x = routed(); refs = protocol(x); case = x.cases.get(x.case.case_id)
    old = ReasoningContextAssembler(catalog=x.catalog).build(case, x.evidence.list(case.case_id))
    promote(x, refs)
    with pytest.raises(AgentPreconditionFailed, match="STALE_CAPABILITY"):
        x.executor.execute_if_current(case.case_id, T.PAYMENT, Q, precondition=execution_precondition(case, old))
    assert x.cases.get(case.case_id) == case
    with pytest.raises(RegistryError, match="STALE_CAPABILITY"):
        x.catalog.revalidate_snapshot(case, old.capability_snapshot)


@pytest.mark.parametrize("payload", ["protocol=2.3", {"protocol_version": "2.3"}, ("2.3",)])
def test_llm_cannot_mutate_route(routed, payload):
    x = routed(); before = x.repo.route_revision(x.case.case_id)
    with pytest.raises(RegistryError, match="INVALID_ROUTING_PROOF"):
        promote(x, payload)
    assert x.repo.route_revision(x.case.case_id) == before


def test_skill_cannot_mutate_route(routed):
    x = routed()
    with pytest.raises(RegistryError):
        promote(x, ("SKILL-PROTOCOL-23",))


def test_historical_experience_cannot_mutate_route(routed):
    x = routed()
    with pytest.raises(RegistryError):
        promote(x, ("EXPERIENCE-PROTOCOL-23",))


@pytest.mark.parametrize("tenant", ["demo", "foreign"])
def test_foreign_evidence_cannot_promote_route(routed, tenant):
    x, other = routed(), routed(case_id="CASE-OTHER", tenant=tenant)
    with pytest.raises(RegistryError, match="INVALID_ROUTING_PROOF"):
        promote(x, protocol(other))
    assert x.repo.route(x.case.case_id).protocol_version is None


def observe_with_metadata(monkeypatch, **changes):
    from credit_harness.simulator import service
    original = service.Observation
    def observed(**fields):
        return original(**{**fields, **changes})
    monkeypatch.setattr(service, "Observation", observed)


def test_stale_evidence_cannot_promote_route(routed, monkeypatch):
    x = routed(); observe_with_metadata(monkeypatch, freshness=Freshness.STALE)
    with pytest.raises(RegistryError, match="INVALID_ROUTING_PROOF"):
        promote(x, protocol(x))


def test_incomplete_evidence_cannot_promote_route(routed, monkeypatch):
    x = routed(); observe_with_metadata(monkeypatch, completeness=Completeness.PARTIAL)
    with pytest.raises(RegistryError, match="INVALID_ROUTING_PROOF"):
        promote(x, protocol(x))


def test_conflicting_protocol_does_not_overwrite(routed):
    x = routed(); known = promote(x, protocol(x))
    with pytest.raises(RegistryError, match="ROUTING_FACT_CONFLICT"):
        promote(x, protocol(x, "2.2"))
    assert x.repo.route_revision(x.case.case_id) == known
    assert len(x.repo.route_history(x.case.case_id)) == 2


def test_concurrent_route_revision_cas(routed):
    x = routed(); initial = x.repo.route_revision(x.case.case_id)
    proof_a, proof_b = protocol(x), protocol(x, "2.2")
    def advance(refs):
        try:
            return promote(x, refs, initial.route_revision_id).route_context.protocol_version
        except RegistryError as exc:
            return exc.code.value
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(advance, (proof_a, proof_b)))
    assert results.count("STALE_ROUTE_REVISION") == 1
    assert x.repo.route(x.case.case_id).protocol_version in ("2.2", "2.3")
    assert len(x.repo.route_history(x.case.case_id)) == 2


def test_route_revision_and_head_are_atomic(routed, monkeypatch):
    x = routed(); refs = protocol(x); old = x.repo.route_revision(x.case.case_id)
    from credit_harness.registry import revisions
    original = revisions.append_revision
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic crash after append")
    monkeypatch.setattr(revisions, "append_revision", crash)
    with pytest.raises(RuntimeError):
        promote(x, refs)
    assert x.repo.route_history(x.case.case_id) == (old,)


def test_protocol_proof_requires_registered_dispatch_provenance(routed):
    x = routed(); refs = protocol(x)
    with Session(x.cases.engine) as s, s.begin():
        row = s.scalar(select(DispatchSourceRow).where(DispatchSourceRow.case_id == x.case.case_id))
        row.payload = {**row.payload, "case_id": "OTHER"}
    with pytest.raises(RegistryError):
        promote(x, refs)


def test_disabled_protocol_source_cannot_promote_route(routed):
    x = routed(); refs = protocol(x)
    definition = x.definition.model_copy(update=dict(capabilities=tuple(c.model_copy(update=dict(status=RegistryStatus.DISABLED))
        if c.tool_name == T.PROTOCOL else c for c in x.definition.capabilities)))
    v = x.admin.register(definition, actor="disable-admin")
    x.admin.activate(v, expected_version=x.version, actor="disable-admin")
    with pytest.raises(RegistryError):
        promote(x, refs)


def test_initial_bind_cannot_overwrite_promoted_route(routed):
    x = routed(); known = promote(x, protocol(x))
    with pytest.raises(ValueError):
        x.admin.bind_case_route(x.cases, known.route_context.model_copy(update=dict(funding_partner="OTHER")), actor="admin")
    assert x.repo.route_revision(x.case.case_id) == known


def test_funding_integration_and_source_channels(routed):
    x = routed()
    systems = {s.system_id: s for s in x.definition.systems}
    for c in x.definition.capabilities:
        s = systems[c.system_id]
        if c.tool_name in (T.FUND, T.LOAN_NOTE):
            assert s.system_type == SystemType.FUNDING_INTEGRATION
            assert s.source_channel == SourceChannel.PARTNER_OFFICIAL_API
        if c.tool_name == T.PAYMENT:
            assert s.system_type == SystemType.PAYMENT_STATUS_SOURCE
        if c.tool_name in (T.ASSET, T.ASSET_DELIVERY):
            assert s.scope.partner_role == PartnerRole.ASSET and s.scope.partner_id == "JD"
        if c.tool_name in (T.GUARANTEE, T.ACCOUNTING, T.TRACE, T.CALLBACK, T.CALLBACK_RAW, T.MESSAGES, T.PROTOCOL):
            assert s.scope.partner_role is None and s.scope.partner_id is None


def test_internal_tools_do_not_depend_on_funding_partner(routed):
    x = routed(); route = x.repo.route(x.case.case_id).model_copy(update=dict(funding_partner=None))
    assert x.resolver.resolve_tool(x.case, T.ACCOUNTING, route).tool_name == T.ACCOUNTING
    assert x.resolver.resolve_tool(x.case, T.ASSET, route).system_id == "sim-asset-jd"
    with pytest.raises(RegistryError):
        x.resolver.resolve_tool(x.case, T.PAYMENT, route)


def test_funding_integration_cannot_be_payment_finality_authority(routed):
    x = routed()
    definition = x.definition.model_copy(update=dict(systems=tuple(s.model_copy(update=dict(system_type=SystemType.FUNDING_INTEGRATION))
        if s.system_type == SystemType.PAYMENT_STATUS_SOURCE else s for s in x.definition.systems)))
    with pytest.raises(ValueError, match="payment status source"):
        x.admin.register(definition, actor="admin")


def test_legacy_registry_body_keeps_original_hash(routed):
    x = routed(); legacy = x.definition.model_dump(mode="json")
    for s in legacy["systems"]:
        s.pop("source_channel")
        s["system_type"] = {"FUNDING_INTEGRATION": "FUNDING_CORE", "PAYMENT_STATUS_SOURCE": "PAYMENT_LEDGER"}.get(s["system_type"], s["system_type"])
    version = fingerprint(legacy)
    with Session(x.cases.engine) as s, s.begin():
        s.add(RegistryVersionRow(version=version, tenant_id=x.case.tenant_id, payload=legacy))
        s.get(RegistryHeadRow, x.case.tenant_id).version = version
    assert x.repo.current()[0] == version
    assert any(s.system_type == SystemType.FUNDING_INTEGRATION for s in x.repo.current()[1].systems)
    with Session(x.cases.engine) as s:
        assert s.get(RegistryVersionRow, version).payload == legacy


def test_legacy_case_route_import_preserves_old_body(routed):
    x = routed()
    with Session(x.cases.engine) as s, s.begin():
        original = dict(s.get(RouteContextRow, x.case.case_id).payload)
        s.delete(s.get(RouteHeadRow, x.case.case_id)); s.flush()
        for row in s.scalars(select(RouteRevisionRow).where(RouteRevisionRow.case_id == x.case.case_id)).all():
            s.delete(row)
    assert x.repo.route(x.case.case_id).protocol_version is None
    initial = x.updater.initialize_legacy(x.case.case_id)
    assert initial.change_reason == RouteChangeReason.LEGACY_IMPORT
    assert x.updater.initialize_legacy(x.case.case_id) == initial
    promote(x, protocol(x))
    with Session(x.cases.engine) as s:
        assert s.get(RouteContextRow, x.case.case_id).payload == original


def test_missing_head_cannot_reset_known_protocol(routed):
    x = routed(); promote(x, protocol(x))
    with Session(x.cases.engine) as s, s.begin():
        s.delete(s.get(RouteHeadRow, x.case.case_id))
    with pytest.raises(RegistryError, match="INVALID_ROUTE_CONTEXT"):
        x.repo.route(x.case.case_id)
    with pytest.raises(RegistryError, match="INVALID_ROUTE_CONTEXT"):
        x.updater.initialize_legacy(x.case.case_id)


@pytest.mark.parametrize("field,value", [("subject_binding_required", False), ("identity_binding_required", False),
    ("freshness_requirement", Freshness.STALE), ("completeness_requirement", Completeness.PARTIAL)])
def test_authoritative_payment_source_requires_full_binding(routed, field, value):
    x = routed()
    definition = x.definition.model_copy(update=dict(authority_rules=tuple(r.model_copy(update={field: value})
        if r.claim_type == C.PAYMENT_FINALITY else r for r in x.definition.authority_rules)))
    with pytest.raises(ValueError, match="subject, identity, current and complete"):
        x.admin.register(definition, actor="admin")
