import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.test_orchestration import factory, create, due
from credit_harness.registry.models import RegistryStatus
from credit_harness.registry.fixtures import synthetic_registry, synthetic_route
from credit_harness.registry.repository import RegistryRepository, RegistryAdmin
from credit_harness.registry.resolver import CapabilityResolver
from credit_harness.registry.snapshot import RegistryBackedCatalog
from credit_harness.registry.adapters import TrustedAdapterResolver
from credit_harness.registry.dispatch import RegistryDispatchGuard
from credit_harness.registry.tables import DispatchSourceRow
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.domain.enums import ToolName as T, ScenarioId
from credit_harness.evaluation.models import VerificationRequirement as Q
from credit_harness.orchestration.models import WorkStatus, WorkReason
from credit_harness.planner.model import FakePlannerModel
from credit_harness.planner.service import PlannerService
from scripts.demo_orchestration import payment_then_wait


def wire(x, *, disabled=()):
    repo = RegistryRepository(x.engine, x.case.tenant_id)
    admin = RegistryAdmin(repo)
    definition = synthetic_registry(x.case.tenant_id)
    definition = definition.model_copy(update=dict(capabilities=tuple(c.model_copy(
        update=dict(status=RegistryStatus.DISABLED)) if c.tool_name in disabled else c for c in definition.capabilities)))
    version = admin.register(definition, actor="runtime-fixture")
    admin.activate(version, expected_version=None, actor="runtime-fixture")
    admin.bind_case_route(x.cases, synthetic_route(x.case), actor="runtime-fixture")
    resolver = CapabilityResolver(repo)
    x.cases.registry_guard = RegistryDispatchGuard(resolver, TrustedAdapterResolver({
        (c.system_id, c.adapter_id, "1", "1"): lambda _: x.client for c in definition.capabilities}))
    x.agent.assembler = ReasoningContextAssembler(catalog=RegistryBackedCatalog(resolver))
    return repo


def test_verification_worker_executes_registered_source(factory):
    x = factory(ScenarioId.S6)
    wire(x)
    item = create(x, requirement=Q.PAYMENT_FINALITY)
    x.worker.process(due(x, item))
    with Session(x.engine) as s:
        sources = s.scalars(select(DispatchSourceRow).where(DispatchSourceRow.case_id == x.case.case_id)).all()
        assert len(sources) == 1
        assert sources[0].payload["resolved"]["system_id"] == "sim-payment-sushang"
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 1


def test_verification_worker_does_not_fallback_when_registry_disables_source(factory):
    x = factory()
    wire(x, disabled=(T.PAYMENT,))
    item = create(x, requirement=Q.PAYMENT_FINALITY)
    x.worker.process(due(x, item))
    assert x.work.get(item.work_item_id).status == WorkStatus.BLOCKED
    assert any(w.reason_code == WorkReason.NO_TOOL for w in x.work.list(x.case.case_id))
    assert x.cases.get(x.case.case_id).budget.used_tool_calls == 0


def test_investigation_runtime_uses_registry_snapshot_and_dispatch(factory):
    x = factory(model=payment_then_wait)
    wire(x)
    run = x.agent.run(x.case.case_id)
    assert run.tool_calls_dispatched == 1
    assert x.evidence.list(x.case.case_id)
    with Session(x.engine) as s:
        assert len(s.scalars(select(DispatchSourceRow).where(DispatchSourceRow.case_id == x.case.case_id)).all()) == 1
    # S8 timeout remains observational UNKNOWN, registry authority never upgrades it.
    from credit_harness.evidence.models import ClaimType
    assert not any(e.claim_type == ClaimType.PAYMENT_FINALITY for e in x.evidence.list(x.case.case_id))
