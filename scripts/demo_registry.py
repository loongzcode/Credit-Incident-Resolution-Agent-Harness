"""Offline synthetic source resolution, actual read provenance, missing and ambiguous routes."""
import json
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine
from credit_harness.benchmark.fixture import EvaluationFixture
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.planner.renderer import ModelInputRenderer
from credit_harness.registry.models import RegistryError
from credit_harness.registry.fixtures import synthetic_registry, synthetic_route
from credit_harness.registry.repository import RegistryRepository, RegistryAdmin
from credit_harness.registry.resolver import CapabilityResolver
from credit_harness.registry.snapshot import RegistryBackedCatalog
from credit_harness.registry.adapters import TrustedAdapterResolver
from credit_harness.registry.dispatch import RegistryDispatchGuard
from credit_harness.registry.revisions import RouteUpdater
from credit_harness.tools.contracts import ToolQuery


def run(engine):
    x = EvaluationFixture(engine, ready=False)
    try:
        repo = RegistryRepository(engine, x.case.tenant_id)
        admin = RegistryAdmin(repo, clock=lambda: x.clock.now)
        definition = synthetic_registry(x.case.tenant_id)
        v1 = admin.register(definition, actor="synthetic-demo-admin")
        admin.activate(v1, expected_version=None, actor="synthetic-demo-admin")
        route = synthetic_route(x.case, protocol_version=None)
        admin.bind_case_route(x.cases, route, actor="synthetic-demo-routing")
        resolver = CapabilityResolver(repo)
        catalog = RegistryBackedCatalog(resolver)
        x.cases.registry_guard = RegistryDispatchGuard(resolver, TrustedAdapterResolver({
            (c.system_id, c.adapter_id, "1", "1"): lambda _: x.client for c in definition.capabilities}))
        r1 = repo.route_revision(x.case.case_id)
        before_tools = [t.tool_name.value for t in catalog.for_case(x.case)]
        protocol_read = x.executor.execute(x.case.case_id, T.PROTOCOL,
            ToolQuery(internal_order_id=x.case.internal_order_id, protocol_version="2.3"))
        proof = tuple(e.evidence_id for e in x.evidence.list(x.case.case_id)
            if e.evidence_id in protocol_read.evidence_refs and e.claim_type == C.PROTOCOL_FIELD_TYPE)
        old_snapshot = catalog.project(x.cases.get(x.case.case_id))[1]
        r2 = RouteUpdater(x.cases, resolver, clock=lambda: x.clock.now).promote_protocol(
            x.case.case_id, expected_revision_id=r1.route_revision_id, evidence_refs=proof)
        route = repo.route(x.case.case_id)
        try:
            catalog.revalidate_snapshot(x.cases.get(x.case.case_id), old_snapshot)
        except RegistryError as exc:
            stale_code = exc.code.value
        sources = [resolver.resolve(x.case, claim, route).model_dump(mode="json")
                   for claim in (C.PAYMENT_FINALITY, C.MESSAGE_CONSUME_STATUS, C.FUND_BUSINESS_STATUS, C.ASSET_STATUS)]
        x.read(T.PAYMENT, T.MESSAGES, T.FUND, T.ASSET)
        evidence = x.evidence.list(x.case.case_id)
        snapshot = ReasoningContextAssembler(catalog=catalog).build(x.cases.get(x.case.case_id), evidence)
        model = ModelInputRenderer().render(snapshot)
        # Admin-side demonstrations; no registry mutations or credentials are exposed to the model.
        reduced = definition.model_copy(update=dict(
            capabilities=tuple(c for c in definition.capabilities if c.tool_name != T.ACCOUNTING),
            authority_rules=tuple(r for r in definition.authority_rules if r.capability_id != "cap-accounting")))
        v2 = admin.register(reduced, actor="synthetic-demo-admin")
        admin.activate(v2, expected_version=v1, actor="synthetic-demo-admin")
        try:
            resolver.resolve(x.case, C.ACCOUNTING_ENTRY_PRESENT, route)
        except RegistryError as exc:
            missing = exc.code.value
        c = next(c for c in reduced.capabilities if c.tool_name == T.PAYMENT)
        duplicate = c.model_copy(update=dict(capability_id="duplicate-payment"))
        ambiguous = reduced.model_copy(update=dict(capabilities=(*reduced.capabilities, duplicate),
            authority_rules=(*reduced.authority_rules, *(r.model_copy(update=dict(capability_id=duplicate.capability_id))
                for r in reduced.authority_rules if r.capability_id == c.capability_id))))
        v3 = admin.register(ambiguous, actor="synthetic-demo-admin")
        admin.activate(v3, expected_version=v2, actor="synthetic-demo-admin")
        try:
            resolver.resolve(x.case, C.PAYMENT_FINALITY, route)
        except RegistryError as exc:
            conflict = exc.code.value
        meanings = {s.system_id: s.display_name for s in definition.systems}
        return dict(synthetic=True, case_id=x.case.case_id, runtime_resolved_sources=sources,
            business_meanings={s["tool_name"]: meanings[s["system_id"]] for s in sources},
            route_revisions=[r1.model_dump(mode="json"), r2.model_dump(mode="json")],
            tools_before_protocol_promotion=before_tools, old_snapshot_revalidation=stale_code,
            planner_capability_snapshot=model.trusted_control.capability_snapshot.model_dump(mode="json"),
            observed_evidence_count=len(evidence), observed_claims=sorted({e.claim_type.value for e in evidence}),
            provenance=x.cases.registry_guard.evidence_sources(x.cases, evidence[0].evidence_id)[0].model_dump(mode="json"),
            missing_accounting=missing, ambiguous_payment=conflict, write_authorized=False)
    finally:
        x.close()


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1] / ".local"
    root.mkdir(exist_ok=True)
    engine = create_engine(f"sqlite:///{(root / ('registry-demo-' + uuid4().hex + '.db')).as_posix()}")
    try:
        print(json.dumps(run(engine), ensure_ascii=False, indent=2))
    finally:
        engine.dispose()
