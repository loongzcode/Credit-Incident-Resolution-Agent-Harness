"""Trusted deployment manifest wires existing clients; never loads Oracle state."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from pydantic import BaseModel, ConfigDict, SecretStr, Field
from credit_harness.cases.repository import CaseRepository, utc_now
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.registry.repository import RegistryRepository
from credit_harness.registry.resolver import CapabilityResolver
from credit_harness.registry.adapters import TrustedAdapterResolver
from credit_harness.registry.dispatch import RegistryDispatchGuard
from credit_harness.registry.snapshot import RegistryBackedCatalog
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.tools.client import ToolClient
from .app import production_engine, schema_ready
from .settings import StartupConfigurationError


class AdapterBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    system_id: str
    adapter_id: str
    query_version: str
    response_version: str
    base_url: str
    case_credentials: dict[str, SecretStr] = Field(repr=False)


def create_components(settings, kind):
    engine = production_engine(settings)
    if not schema_ready(engine):
        raise StartupConfigurationError("DATABASE_SCHEMA_NOT_READY")
    cases = CaseRepository(engine, settings.tenant)
    evidence = EvidenceRepository(cases)
    if kind == "embedding":
        from credit_harness.memory.skills import SQLSkillRepository
        from credit_harness.memory.repository import SQLExperienceRepository
        from credit_harness.retrieval.source import MemorySources
        from credit_harness.retrieval.repository import PgVectorRepository
        from credit_harness.retrieval.indexer import EmbeddingIndexer
        from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
        from openai import OpenAI
        provider = OpenAIEmbeddingProvider(model_id=settings.embedding_model, dimension=settings.embedding_dimension,
            client=OpenAI(timeout=settings.http_timeout_seconds, max_retries=0))
        vector = PgVectorRepository(MemorySources(SQLSkillRepository(engine, settings.tenant), SQLExperienceRepository(cases)))
        return SimpleNamespace(engine=engine, vector=vector, indexer=EmbeddingIndexer(vector, provider,
            clock=utc_now, lease_seconds=settings.worker_lease_seconds))
    try:
        manifest = json.loads(Path(os.environ["TOOL_BINDINGS_FILE"]).read_text(encoding="utf-8"))
        bindings = [AdapterBinding.model_validate(item) for item in manifest]
        factories = {}
        for binding in bindings:
            if not binding.base_url.startswith("https://"):
                raise ValueError("TLS tool endpoint required")
            key = (binding.system_id, binding.adapter_id, binding.query_version, binding.response_version)
            if key in factories:
                raise ValueError("duplicate adapter binding")
            def client_for_case(case_id, b=binding):
                if case_id not in b.case_credentials:
                    raise ValueError("case credential unavailable")
                # One request client: explicit timeout; no credential is passed to Planner.
                class ScopedClient:
                    def observe(self, tool, query, *, dispatch_correlation_id):
                        with ToolClient(b.base_url, b.case_credentials[case_id].get_secret_value(),
                                timeout=settings.http_timeout_seconds) as client:
                            return client.observe(tool, query, dispatch_correlation_id=dispatch_correlation_id)
                return ScopedClient()
            factories[key] = client_for_case
    except Exception:
        raise StartupConfigurationError("TRUSTED_TOOL_BINDINGS_INVALID") from None
    resolver = CapabilityResolver(RegistryRepository(engine, settings.tenant))
    cases.registry_guard = RegistryDispatchGuard(resolver, TrustedAdapterResolver(factories))
    catalog = RegistryBackedCatalog(resolver)
    from credit_harness.agent.runtime import InvestigationAgentRuntime
    from credit_harness.planner.service import PlannerService
    from credit_harness.adapters.openai_planner import OpenAIPlannerModel
    from credit_harness.investigation.trace_store import SQLInvestigationTraceStore
    from credit_harness.evaluation.evaluator import IndependentEvaluator
    from credit_harness.evaluation.closure import VerifiedClosureService
    from credit_harness.orchestration.repository import WorkRepository
    from credit_harness.orchestration.service import DurableCaseOrchestrator
    from credit_harness.authorization.store import SQLApprovalStore
    from credit_harness.recovery.repository import RecoveryRepository
    from credit_harness.recovery.models import RecoveryPolicy
    from credit_harness.recovery.service import SideEffectRecoveryCoordinator
    from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
    from openai import OpenAI
    executor = CaseToolExecutor(cases, evidence, lambda _: None)  # guard is mandatory
    trace = SQLInvestigationTraceStore(cases, alias_key=settings.key_bytes("identity_alias_hmac_key"))
    from credit_harness.memory.skills import SQLSkillRepository
    from credit_harness.memory.repository import SQLExperienceRepository
    from credit_harness.retrieval.source import MemorySources
    from credit_harness.retrieval.repository import PgVectorRepository
    from credit_harness.retrieval.service import HybridRetrievalService
    from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
    from credit_harness.investigation.trace_store import TracedGuidanceProvider, SQLSafePlannerAuditStore
    hybrid = HybridRetrievalService(PgVectorRepository(MemorySources(
        SQLSkillRepository(engine, settings.tenant), SQLExperienceRepository(cases), resolver.repository)),
        OpenAIEmbeddingProvider(model_id=settings.embedding_model, dimension=settings.embedding_dimension,
            client=OpenAI(timeout=settings.http_timeout_seconds, max_retries=0)))
    planner = PlannerService(OpenAIPlannerModel(client=OpenAI(timeout=settings.http_timeout_seconds, max_retries=0)),
        audit=SQLSafePlannerAuditStore(trace),
        guidance_provider=TracedGuidanceProvider(hybrid.guidance_provider(), trace, hybrid=hybrid))
    runtime = InvestigationAgentRuntime(cases, evidence, ReasoningContextAssembler(catalog=catalog), planner, executor, trace_store=trace)
    evaluator = IndependentEvaluator(cases, catalog=catalog)
    recovery_policy = RecoveryPolicy(lease_seconds=settings.worker_lease_seconds)
    work = WorkRepository(cases, lease_seconds=settings.worker_lease_seconds, recovery_policy=recovery_policy)
    # Current environment contains synthetic effects only. This resolver performs
    # lookup, has no dispatch method, and never introduces a new money movement.
    effect_resolver = SyntheticEffectStatusResolver(engine, settings.tenant)
    recovery = SideEffectRecoveryCoordinator(RecoveryRepository(SQLApprovalStore(cases), effect_resolver.capability,
        policy=recovery_policy), effect_resolver)
    orchestrator = DurableCaseOrchestrator(work, evidence, executor, runtime, evaluator,
        VerifiedClosureService(evaluator), effect_recovery=recovery)
    return SimpleNamespace(engine=engine, work=work, orchestrator=orchestrator, recovery=recovery, cases=cases)
