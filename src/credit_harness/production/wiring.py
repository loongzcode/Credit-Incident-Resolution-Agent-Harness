"""Trusted deployment manifest wires existing clients; never loads Oracle state."""
import json
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


def read_catalog(settings, engine, cases):
    try:
        manifest = json.loads(Path(settings.tool_bindings_file).read_text(encoding="utf-8"))
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
    return RegistryBackedCatalog(resolver)


class UnavailableGuidance:
    def build_result(self, snapshot):
        from credit_harness.memory.models import GuidanceBuildResult, GuidanceBuildStatus, GuidanceDegradation
        return GuidanceBuildResult(bundle=None, status=GuidanceBuildStatus.RETRIEVAL_FAILED,
            degradation=GuidanceDegradation.NONE)


def embedding_provider(config, timeout):
    from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
    from openai import OpenAI
    return OpenAIEmbeddingProvider(model_id=config.embedding_model, dimension=config.embedding_dimension,
        client=OpenAI(api_key=config.openai_api_key.get_secret_value(), timeout=timeout, max_retries=0))


def vector_repository(engine, cases, registry=None):
    from credit_harness.memory.skills import SQLSkillRepository
    from credit_harness.memory.repository import SQLExperienceRepository
    from credit_harness.retrieval.source import MemorySources
    from credit_harness.retrieval.repository import PgVectorRepository
    return PgVectorRepository(MemorySources(SQLSkillRepository(engine, cases.tenant_id),
        SQLExperienceRepository(cases), registry))


def optional_guidance(settings, engine, cases, catalog, trace):
    # Parse optional embedding separately: invalid dimension/model/provider never
    # invalidates the required Planner configuration or fabricates guidance.
    try:
        from .settings import EmbeddingProviderSettings
        from credit_harness.retrieval.service import HybridRetrievalService
        from credit_harness.investigation.trace_store import TracedGuidanceProvider
        config = EmbeddingProviderSettings.from_env()
        hybrid = HybridRetrievalService(vector_repository(engine, cases, catalog.resolver.repository),
            embedding_provider(config, settings.http_timeout_seconds))
        return TracedGuidanceProvider(hybrid.guidance_provider(), trace, hybrid=hybrid)
    except Exception:
        return UnavailableGuidance()


def create_components(settings, kind):
    from .settings import WORKER_SETTINGS
    if kind not in WORKER_SETTINGS or type(settings) is not WORKER_SETTINGS[kind]:
        raise StartupConfigurationError("PROCESS_CONFIGURATION_MISMATCH")
    engine = production_engine(settings)
    if not schema_ready(engine):
        raise StartupConfigurationError("DATABASE_SCHEMA_NOT_READY")
    cases = CaseRepository(engine, settings.tenant)
    if kind == "embedding":
        from credit_harness.retrieval.indexer import EmbeddingIndexer
        provider = embedding_provider(settings, settings.http_timeout_seconds)
        vector = vector_repository(engine, cases)
        return SimpleNamespace(engine=engine, vector=vector, indexer=EmbeddingIndexer(vector, provider,
            clock=utc_now, lease_seconds=settings.worker_lease_seconds))
    from credit_harness.evaluation.evaluator import IndependentEvaluator
    from credit_harness.evaluation.closure import VerifiedClosureService
    from credit_harness.orchestration.repository import WorkRepository
    from credit_harness.orchestration.service import DurableCaseOrchestrator
    from credit_harness.orchestration.models import WorkType as T
    from credit_harness.recovery.models import RecoveryPolicy
    evidence = EvidenceRepository(cases)
    recovery_policy = RecoveryPolicy(lease_seconds=settings.worker_lease_seconds)
    work = WorkRepository(cases, lease_seconds=settings.worker_lease_seconds, recovery_policy=recovery_policy)
    if kind == "recovery":
        from credit_harness.authorization.store import SQLApprovalStore
        from credit_harness.recovery.repository import RecoveryRepository
        from credit_harness.recovery.service import SideEffectRecoveryCoordinator
        from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
        effect_resolver = SyntheticEffectStatusResolver(engine, settings.tenant)
        recovery = SideEffectRecoveryCoordinator(RecoveryRepository(SQLApprovalStore(cases), effect_resolver.capability,
            policy=recovery_policy), effect_resolver)
        # Evaluator computes progress only; no read dispatch or planner exists.
        orchestrator = DurableCaseOrchestrator(work, evidence, None, None, IndependentEvaluator(cases), None,
            effect_recovery=recovery, allowed_work_types={T.RECOVERY_RECHECK})
        return SimpleNamespace(engine=engine, cases=cases, work=work, recovery=recovery, orchestrator=orchestrator)
    catalog = read_catalog(settings, engine, cases)
    executor = CaseToolExecutor(cases, evidence, lambda _: None)  # registry guard mandatory
    evaluator = IndependentEvaluator(cases, catalog=catalog)
    runtime = None
    if kind == "agent":
        from credit_harness.agent.runtime import InvestigationAgentRuntime
        from credit_harness.planner.service import PlannerService
        from credit_harness.adapters.openai_planner import OpenAIPlannerModel
        from credit_harness.investigation.trace_store import SQLInvestigationTraceStore, SQLSafePlannerAuditStore
        from openai import OpenAI
        trace = SQLInvestigationTraceStore(cases, alias_key=settings.key_bytes("identity_alias_hmac_key"))
        planner = PlannerService(OpenAIPlannerModel(model_name=settings.planner_model,
            client=OpenAI(api_key=settings.openai_api_key.get_secret_value(), timeout=settings.http_timeout_seconds, max_retries=0)),
            audit=SQLSafePlannerAuditStore(trace),
            guidance_provider=optional_guidance(settings, engine, cases, catalog, trace))
        runtime = InvestigationAgentRuntime(cases, evidence, ReasoningContextAssembler(catalog=catalog),
            planner, executor, trace_store=trace)
    allowed = {T.INVESTIGATION_RESUME, T.OPERATOR_FOLLOWUP} if kind == "agent" else {T.VERIFICATION_REQUIRED}
    orchestrator = DurableCaseOrchestrator(work, evidence, executor, runtime, evaluator,
        VerifiedClosureService(evaluator), allowed_work_types=allowed)
    return SimpleNamespace(engine=engine, work=work, orchestrator=orchestrator, cases=cases)
