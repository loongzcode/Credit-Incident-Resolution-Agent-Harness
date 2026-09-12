"""Retrieval admission uses real primary rows; fake embeddings only replace network."""
import json
from datetime import timedelta
from unittest.mock import patch
import pytest
from pydantic import ValidationError
from sqlalchemy import select, func, update, delete, event, text
from sqlalchemy.orm import Session
from tests.test_organizational_memory import factory, context
from credit_harness.memory.skills import general_investigation_skill
from credit_harness.memory.models import SkillScope, IncidentSignature, GuidanceInvariant, GuidanceBuildStatus
from credit_harness.memory.tables import SkillRow, ExperienceRow
from credit_harness.memory.repository import experience_identity
from credit_harness.context.budget import digest
from credit_harness.domain.enums import ToolName, TransportStatus, PaymentFinality
from credit_harness.retrieval.models import *
from credit_harness.retrieval.projection import *
from credit_harness.retrieval.embedding import DeterministicFakeEmbeddingProvider
from credit_harness.retrieval.tables import *
from credit_harness.retrieval.source import MemorySources
from credit_harness.retrieval.repository import PgVectorRepository, ExactVectorTestRepository
from credit_harness.retrieval.indexer import EmbeddingIndexer
from credit_harness.retrieval.service import HybridRetrievalService


def add_skill(x, name="payment-investigation", scope=None, **changes):
    skill = general_investigation_skill().model_copy(update={"skill_id": name, "scope": scope or SkillScope(), **changes})
    x.skills.add(skill)
    x.skills.activate(skill.skill_id, skill.version)
    return x.skills.active()[-1] if len(x.skills.active()) == 1 else next(s for s in x.skills.active() if s.skill_id == name and s.version == skill.version)


def setup(x, *, activate=True, registry=None, provider=None):
    create_retrieval_schema(x.engine)
    sources = MemorySources(x.skills, x.memory, registry)
    repo = (PgVectorRepository if x.engine.dialect.name == "postgresql" else ExactVectorTestRepository)(sources)
    provider = provider or DeterministicFakeEmbeddingProvider()
    space = repo.create_space(provider, x.clock.now)
    worker = EmbeddingIndexer(repo, provider, clock=lambda: x.clock.now)
    worker.reconcile(space)
    while worker.run_one(space.space_id) is not None:
        pass
    if activate:
        repo.activate_space(space.space_id)
        space = repo.active_space()
    return repo, provider, worker, space, HybridRetrievalService(repo, provider)


def search(repo, provider, space, snap, kind=DocumentType.SKILL):
    return repo.search(kind, space, repo.sources.query_scope(snap), snap.case_id,
        provider.embed_query(embedding_text(incident_query(snap))))[0]


def test_hybrid_skill_end_to_end(factory):
    x = factory(); add_skill(x)
    repo, provider, worker, space, retrieval = setup(x)
    result = retrieval.guidance_provider().build_result(context(x))
    assert result.status == GuidanceBuildStatus.AVAILABLE
    assert result.bundle.active_skills[0].skill.skill_id == "payment-investigation"
    assert len(result.bundle.model_dump_json()) <= 12000
    assert set(retrieval.mandatory_safety) == set(GuidanceInvariant)
    assert retrieval.last_telemetry.vector_candidate_count == 1


@pytest.mark.parametrize("field,wrong", [("funding_partner", "OTHER-FUND"), ("asset_partner", "OTHER-ASSET"),
    ("product_code", "OTHER-PRODUCT"), ("guarantee_partner", "OTHER-GUARANTEE"),
    ("environment", "PROD"), ("business_domain", "PERSONAL_CREDIT")])
def test_unknown_scope_cannot_bypass_hard_filter(factory, field, wrong):
    x = factory(); add_skill(x, scope=SkillScope(**{field: wrong}))
    repo, p, _, space, _ = setup(x)
    assert search(repo, p, space, context(x)) == ()


def test_known_protocol_mismatch_excluded(factory):
    x = factory(); x.read()
    add_skill(x, scope=SkillScope(protocol_versions=("9.9",)))
    repo, p, _, space, _ = setup(x)
    assert search(repo, p, space, context(x)) == ()


def test_unknown_protocol_does_not_infer_historical_version(factory):
    x = factory(); add_skill(x, scope=SkillScope(protocol_versions=("2.3",)))
    repo, p, _, space, _ = setup(x)
    assert search(repo, p, space, context(x)) == ()


def test_other_tenant_cannot_be_recalled(factory):
    x = factory(); y = factory("CASE-FOREIGN", "foreign")
    add_skill(x); add_skill(y, "foreign-skill")
    repo, p, worker, space, _ = setup(x, activate=False)
    other = MemorySources(y.skills, y.memory)
    other_repo = type(repo)(other)
    other_worker = EmbeddingIndexer(other_repo, p, clock=lambda: y.clock.now)
    other_worker.reconcile(space)
    while other_worker.run_one(space.space_id): pass
    repo.activate_space(space.space_id)
    assert [c.document_id for c in search(repo, p, repo.active_space(), context(x))] == ["payment-investigation"]


def test_retired_skill_old_vector_cannot_resurrect(factory):
    x = factory(); s = add_skill(x)
    repo, p, _, space, _ = setup(x)
    x.skills.retire(s.skill_id, s.version)
    assert search(repo, p, space, context(x)) == ()


def test_applicable_time_is_hard_filter(factory):
    x = factory(); add_skill(x, scope=SkillScope(applicable_since=x.clock.now + timedelta(days=1)))
    repo, p, _, space, _ = setup(x)
    assert search(repo, p, space, context(x)) == ()


def test_index_is_idempotent_and_unchanged_content_is_not_reembedded(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, _ = setup(x)
    with patch.object(p, "embed_documents", side_effect=AssertionError("must reuse")):
        worker.reconcile(space)
        assert worker.run_one(space.space_id) is None
    with Session(x.engine) as s:
        assert s.scalar(select(func.count()).select_from(SkillVectorRow)) == 1
        assert s.scalar(select(func.count()).select_from(IndexJobRow)) == 1


def test_content_change_requires_new_index(factory):
    x = factory(); old = add_skill(x)
    repo, p, worker, space, service = setup(x)
    add_skill(x, version="2", applies_when=IncidentSignature(request_transport_status="TIMEOUT"))
    assert service.guidance_provider().build_result(context(x)).status == GuidanceBuildStatus.RETRIEVAL_FAILED
    worker.reconcile(space)
    assert worker.run_one(space.space_id) == JobStatus.COMPLETED
    with Session(x.engine) as s:
        assert len(set(s.scalars(select(SkillVectorRow.content_hash)))) == 2


def test_index_rebuild_after_vector_deletion(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, _ = setup(x)
    with Session(x.engine) as s, s.begin(): s.execute(delete(SkillVectorRow))
    worker.reconcile(space)
    assert worker.run_one(space.space_id) == JobStatus.COMPLETED
    assert len(search(repo, p, space, context(x))) == 1


def test_embedding_spaces_cannot_be_mixed(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, service = setup(x)
    wrong = DeterministicFakeEmbeddingProvider(32)
    assert HybridRetrievalService(repo, wrong).guidance_provider().build_result(context(x)).status == GuidanceBuildStatus.RETRIEVAL_FAILED
    with pytest.raises(RetrievalError):
        repo.search(DocumentType.SKILL, space, repo.sources.query_scope(context(x)), x.case.case_id, [1.] * 32)


def test_space_switch_requires_complete_backfill_and_is_atomic(factory):
    x = factory(); add_skill(x)
    repo, p, worker, old, _ = setup(x)
    new_provider = DeterministicFakeEmbeddingProvider(32)
    new = repo.create_space(new_provider, x.clock.now)
    with pytest.raises(RetrievalError): repo.activate_space(new.space_id)
    assert repo.active_space().space_id == old.space_id
    worker = EmbeddingIndexer(repo, new_provider, clock=lambda: x.clock.now)
    worker.reconcile(new)
    assert worker.run_one(new.space_id) == JobStatus.COMPLETED
    repo.activate_space(new.space_id)
    assert repo.active_space().space_id == new.space_id
    assert repo.get_space(old.space_id).status == SpaceStatus.RETIRED
    with pytest.raises(RetrievalError): search(repo, p, old, context(x))


def test_provider_failure_has_durable_backoff_without_sleep(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, _ = setup(x, activate=False)
    with Session(x.engine) as s, s.begin():
        s.execute(delete(SkillVectorRow))
    worker.reconcile(space)
    with patch.object(p, "embed_documents", side_effect=TimeoutError("RAW-SECRET")):
        assert worker.run_one(space.space_id) == JobStatus.FAILED
        assert worker.run_one(space.space_id) is None
    x.clock.now += timedelta(seconds=61)  # second attempt: first built the old index
    assert worker.run_one(space.space_id) == JobStatus.COMPLETED


def test_expired_worker_cannot_publish_vector(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, _ = setup(x, activate=False)
    with Session(x.engine) as s, s.begin(): s.execute(delete(SkillVectorRow))
    worker.reconcile(space)
    job = worker.claim(space.space_id)
    document = repo.sources.get(job.document_type, job.document_id, job.source_version)
    x.clock.now += timedelta(seconds=121)
    assert worker.claim(space.space_id).lease_token != job.lease_token
    with pytest.raises(RetrievalError): worker.complete(job, document, [1.] * 64)


def test_provider_network_outside_business_transaction(factory):
    x = factory(); add_skill(x)
    repo, p, worker, space, _ = setup(x, activate=False)
    with Session(x.engine) as s, s.begin(): s.execute(delete(SkillVectorRow))
    worker.reconcile(space)
    active = set()
    def begin(conn): active.add(id(conn))
    def end(conn): active.discard(id(conn))
    event.listen(x.engine, "begin", begin); event.listen(x.engine, "commit", end); event.listen(x.engine, "rollback", end)
    original = p.embed_documents
    def embed(texts):
        assert not active
        return original(texts)
    try:
        with patch.object(p, "embed_documents", side_effect=embed):
            assert worker.run_one(space.space_id) == JobStatus.COMPLETED
    finally:
        event.remove(x.engine, "begin", begin); event.remove(x.engine, "commit", end); event.remove(x.engine, "rollback", end)


def test_provider_failure_degrades_but_mandatory_safety_remains(factory):
    x = factory(); add_skill(x)
    repo, p, _, _, service = setup(x)
    with patch.object(p, "embed_query", side_effect=TimeoutError("RAW-SECRET")):
        result = service.guidance_provider().build_result(context(x))
    assert result.bundle is None and result.status == GuidanceBuildStatus.RETRIEVAL_FAILED
    assert set(service.mandatory_safety) == set(GuidanceInvariant)
    assert "RAW-SECRET" not in service.last_telemetry.model_dump_json()


def test_query_vector_not_persisted_and_telemetry_minimized(factory):
    x = factory(); add_skill(x)
    repo, p, _, _, service = setup(x)
    with Session(x.engine) as s:
        before = {t.name: s.scalar(select(func.count()).select_from(t)) for t in RetrievalBase.metadata.tables.values()}
    service.guidance_provider().build_result(context(x))
    with Session(x.engine) as s:
        after = {t.name: s.scalar(select(func.count()).select_from(t)) for t in RetrievalBase.metadata.tables.values()}
    assert before == after
    assert not {"query", "text", "embedding", "evidence", "case_id"} & service.last_telemetry.model_dump().keys()


def test_raw_pii_callback_and_external_refs_never_reach_provider(factory):
    x = factory(); x.read(); s = add_skill(x)
    safe = embedding_text(incident_query(context(x))) + embedding_text(skill_document(s))
    for forbidden in (x.case.internal_order_id, "loanNo", "CUS-JD", "raw_callback", "13800138000", "310101199001011234", "6222021234567890123"):
        assert forbidden not in safe
    with pytest.raises(RetrievalError): incident_query({"raw_callback": "13800138000"})
    with pytest.raises((RetrievalError, ValidationError)): embedding_text(SemanticProjection(states=(IncidentSignature(protocol_version="13800138000"),)))
    # Context's structured release syntax is broader than embedding eligibility.
    for external in ("TXN-SECRET", "customer-name", "MSG-13800138000", "credential-api-key"):
        with pytest.raises(RetrievalError): sanitized_state(IncidentSignature(protocol_version=external))


def test_top_four_skills_and_context_budget(factory):
    x = factory()
    for n in range(25): add_skill(x, f"skill-{n:02}")
    _, _, _, _, service = setup(x)
    result = service.guidance_provider().build_result(context(x))
    assert len(result.bundle.active_skills) == 4
    assert service.last_telemetry.vector_candidate_count == 20
    assert len(result.bundle.model_dump_json()) <= 12000
    assert service.guidance_provider().build(context(x)) == result.bundle


@pytest.mark.postgres
def test_pgvector_extension_vector_column_cosine_query_and_hnsw(factory):
    x = factory()
    if x.engine.dialect.name != "postgresql": pytest.skip("actual PostgreSQL pgvector test")
    add_skill(x)
    repo, p, _, space, _ = setup(x)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany): statements.append(statement)
    event.listen(x.engine, "before_cursor_execute", capture)
    try: assert len(search(repo, p, space, context(x))) == 1
    finally: event.remove(x.engine, "before_cursor_execute", capture)
    assert any("<=>" in q and "LIMIT" in q and "organizational_skills" in q for q in statements)
    schema = x.engine.get_execution_options()["schema_translate_map"][None]
    with x.engine.connect() as c:
        assert c.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
        assert c.scalar(text("SELECT udt_name FROM information_schema.columns WHERE table_schema=:s AND table_name='skill_vector_index' AND column_name='embedding'"), {"s": schema}) == "vector"
        indexes = c.scalars(text("SELECT indexdef FROM pg_indexes WHERE schemaname=:s"), {"s": schema}).all()
        assert sum("USING hnsw" in i and "vector_cosine_ops" in i for i in indexes) == 2


def history_setup(factory):
    h = factory("CASE-HISTORY", closed=True)
    experience = h.publisher.publish(h.case.case_id)
    c = factory("CASE-CURRENT"); c.advance(200); c.read(ToolName.TRACE, ToolName.CALLBACK)
    add_skill(c)
    return h, c, experience, setup(c)


def test_hybrid_verified_experience_end_to_end_and_not_current_evidence(factory):
    h, c, e, (_, _, _, _, service) = history_setup(factory)
    snap = context(c); before = snap.model_dump_json()
    result = service.guidance_provider().build_result(snap)
    assert [e.experience_id for e in result.bundle.verified_experiences] == [e.experience_id]
    from credit_harness.planner.renderer import ModelInputRenderer
    rendered = ModelInputRenderer().render(snap, result.bundle)
    assert rendered.historical_guidance.notice == "HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE"
    assert rendered.deterministic_derived.open_evidence_gaps == snap.open_evidence_gaps
    assert snap.model_dump_json() == before


def test_revoked_experience_old_vector_cannot_resurrect(factory):
    h, c, e, (repo, p, _, space, service) = history_setup(factory)
    h.memory.revoke(e.experience_id)
    assert search(repo, p, space, context(c), DocumentType.EXPERIENCE) == ()


def test_current_source_case_excluded(factory):
    h, c, e, (repo, p, _, space, _) = history_setup(factory)
    snap = context(h)
    assert search(repo, p, space, snap, DocumentType.EXPERIENCE) == ()


def test_embedding_failure_cannot_rollback_verified_closure(factory):
    h = factory("CASE-HISTORY", closed=True)
    e = h.publisher.publish(h.case.case_id)
    p = DeterministicFakeEmbeddingProvider()
    with patch.object(p, "embed_documents", side_effect=TimeoutError("raw secret")):
        repo, p, worker, space, _ = setup(h, activate=False, provider=p)
    assert h.cases.get(h.case.case_id).status.value == "CLOSED_VERIFIED"
    assert h.memory.get(e.experience_id) == e
    with Session(h.engine) as s:
        assert s.scalar(select(IndexJobRow.status)) == "FAILED"


def test_primary_revoke_during_embedding_is_rechecked(factory):
    h, c, e, (repo, p, worker, space, _) = history_setup(factory)
    with Session(c.engine) as s, s.begin(): s.execute(delete(ExperienceVectorRow))
    worker.reconcile(space)
    original = p.embed_documents
    def revoke(texts):
        h.memory.revoke(e.experience_id)
        return original(texts)
    with patch.object(p, "embed_documents", side_effect=revoke):
        assert worker.run_one(space.space_id) == JobStatus.FAILED
    with Session(c.engine) as s:
        assert s.scalar(select(func.count()).select_from(ExperienceVectorRow)) == 0


def test_experience_projection_excludes_all_source_identifiers(factory):
    h = factory(closed=True); e = h.publisher.publish(h.case.case_id)
    payload = embedding_text(experience_document(e))
    for value in (e.source_case_id, e.internal_order_ref, e.experience_id, e.closure_id, e.report_id,
        *e.observed_symptoms.evidence_refs, "loanNo", "callback payload", "vault", "approval"):
        assert value not in payload


def test_current_not_executed_overrides_timeout_success_history(factory):
    from credit_harness.evidence.models import ClaimType
    h = factory("CASE-HISTORY", closed=True); h.publisher.publish(h.case.case_id)
    c = factory("CASE-CURRENT"); c.advance(200)
    c.progress(no_disbursement=True); c.read(ToolName.TRACE, ToolName.CALLBACK, ToolName.PAYMENT)
    add_skill(c); _, _, _, _, service = setup(c)
    snap = context(c); before = snap.model_dump_json()
    result = service.guidance_provider().build_result(snap)
    assert result.bundle and result.bundle.verified_experiences
    assert any(f.claim_type == ClaimType.PAYMENT_FINALITY and f.value == PaymentFinality.NOT_EXECUTED for f in snap.current_facts)
    assert before == snap.model_dump_json()
    assert c.cases.get(c.case.case_id).status.value != "CLOSED_VERIFIED"


def test_vector_guidance_cannot_authorize_effect_or_close_case(factory):
    import ast
    from pathlib import Path
    # Architectural regression: retrieval has no mutation/evaluation dependencies.
    banned = ("credit_harness.authorization", "credit_harness.evaluation.evaluator",
        "credit_harness.evaluation.closure", "credit_harness.cases.executor")
    for file in Path("src/credit_harness/retrieval").glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert not any((node.module or "").startswith(p) for p in banned)
    h, c, _, (_, _, _, _, service) = history_setup(factory)
    before_case = c.cases.get(c.case.case_id)
    before_evidence = c.evidence.list(c.case.case_id)
    service.guidance_provider().build_result(context(c))
    assert c.cases.get(c.case.case_id) == before_case
    assert c.evidence.list(c.case.case_id) == before_evidence


def test_exact_protocol_scope_beats_generic_when_other_signals_equal(factory):
    x = factory(); x.read(ToolName.CALLBACK)
    add_skill(x, "generic-investigation")
    add_skill(x, "scoped-investigation", scope=SkillScope(protocol_versions=("2.3",)))
    _, _, _, _, service = setup(x)
    assert service.select(context(x)).skills[0].skill_id == "scoped-investigation"


def test_gap_coverage_reranks_semantic_candidates(factory):
    from credit_harness.memory.models import SkillEvidenceStrategy, StrategyGoal, RationaleCode
    from credit_harness.evidence.models import ClaimType as C
    from credit_harness.hypotheses.models import PriorityClass
    x = factory(); x.read(ToolName.TRACE)
    def strategy(claim):
        return (SkillEvidenceStrategy(goal=StrategyGoal.ESTABLISH_PAYMENT_FINALITY,
            recommended_claim_types=(claim,), priority=PriorityClass.SAFETY_CRITICAL, rationale_code=RationaleCode.MONEY_TRUTH_FIRST),)
    add_skill(x, "accounting-first", evidence_strategy=strategy(C.ACCOUNTING_ENTRY_PRESENT))
    add_skill(x, "payment-first", evidence_strategy=strategy(C.PAYMENT_FINALITY))
    _, _, _, _, service = setup(x)
    assert service.select(context(x)).skills[0].skill_id == "payment-first"


def test_registry_route_fields_are_trusted_filters_not_embedding_text(factory):
    from credit_harness.registry.repository import RegistryRepository, RegistryAdmin
    from credit_harness.registry.models import CaseRouteContext
    from credit_harness.registry.tables import create_registry_schema
    x = factory(); create_registry_schema(x.engine)
    registry = RegistryRepository(x.engine, x.case.tenant_id)
    route = CaseRouteContext(case_id=x.case.case_id, tenant_id=x.case.tenant_id,
        business_domain="PERSONAL_CREDIT", environment="SIMULATOR", funding_partner="SYNTHETIC-FUND",
        asset_partner="SYNTHETIC-ASSET", product_code="SYNTHETIC-PRODUCT", protocol_version="2.3", effective_at=x.clock.now)
    RegistryAdmin(registry).bind_case_route(x.cases, route, actor="SYNTHETIC-ADMIN")
    add_skill(x, "right-partner", scope=SkillScope(funding_partner="SYNTHETIC-FUND", protocol_versions=("2.3",)))
    add_skill(x, "wrong-partner", scope=SkillScope(funding_partner="OTHER-FUND", protocol_versions=("2.3",)))
    _, _, _, _, service = setup(x, registry=registry)
    assert [s.skill_id for s in service.select(context(x)).skills] == ["right-partner"]
    assert "SYNTHETIC-FUND" not in embedding_text(incident_query(context(x)))


def test_top_three_experiences_after_vector_recall(factory):
    h = factory("CASE-HISTORY", closed=True); original = h.publisher.publish(h.case.case_id)
    # Explicit synthetic primary fixtures, not a production publication shortcut.
    for n in range(5):
        e = original.model_copy(update={"closure_id": digest({"synthetic_closure": n})})
        e = e.model_copy(update={"experience_id": experience_identity(e)})
        with Session(h.engine) as s, s.begin(): h.memory._insert(s, e)
    c = factory("CASE-CURRENT"); c.advance(200); c.read(ToolName.CALLBACK)
    _, _, _, _, service = setup(c)
    result = service.guidance_provider().build_result(context(c))
    assert len(result.bundle.verified_experiences) == 3
    assert service.last_telemetry.vector_candidate_count == 6


def test_invalid_selected_skill_cannot_remove_safety(factory):
    x = factory(); s = add_skill(x)
    _, _, _, _, service = setup(x)
    # Content-addressed source tampering is rejected before optional guidance.
    with Session(x.engine) as session, session.begin():
        row = session.get(SkillRow, (x.case.tenant_id, s.skill_id, s.version))
        payload = json.loads(json.dumps(row.payload)); payload["safety_invariants"][0]["enforced"] = False
        row.payload = payload
    result = service.guidance_provider().build_result(context(x))
    assert result.bundle is None
    assert set(service.mandatory_safety) == set(GuidanceInvariant)


def test_protocol_filter_does_not_treat_underscore_as_sql_wildcard(factory):
    x = factory(); add_skill(x, scope=SkillScope(protocol_versions=("2x3",)))
    repo, p, _, space, _ = setup(x)
    snap = context(x)
    scope = repo.sources.query_scope(snap).model_copy(update={"protocol_versions": ("2_3",)})
    candidates, _ = repo.search(DocumentType.SKILL, space, scope, snap.case_id, p.embed_query("payment unknown"))
    assert candidates == ()


def test_corrupt_index_scope_cannot_override_primary_scope(factory):
    x = factory(); add_skill(x, scope=SkillScope(funding_partner="OTHER-FUND"))
    _, _, _, _, service = setup(x)
    # Simulates damaged/rebuilt index metadata, with the main source untouched.
    with Session(x.engine) as s, s.begin():
        s.execute(update(SkillVectorRow).values(funding_partner=None))
    result = service.guidance_provider().build_result(context(x))
    assert result.bundle is None and result.status == GuidanceBuildStatus.RETRIEVAL_FAILED


def test_corrupt_index_case_binding_cannot_recall_current_case(factory):
    h = factory("CASE-HISTORY", closed=True); h.publisher.publish(h.case.case_id)
    _, _, _, _, service = setup(h)
    with Session(h.engine) as s, s.begin():
        s.execute(update(ExperienceVectorRow).values(source_case_id="CASE-FORGED"))
    result = service.guidance_provider().build_result(context(h))
    assert result.bundle is None and result.status == GuidanceBuildStatus.RETRIEVAL_FAILED


@pytest.mark.llm
def test_semantic_paraphrase_retrieves_expected_skill(factory):
    import os
    if os.getenv("RUN_EMBEDDING_TESTS") != "1" or not os.getenv("OPENAI_API_KEY"):
        pytest.skip("real semantic Skill quality needs explicit embedding provider configuration")
    from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
    from credit_harness.memory.models import StrategyGoal, SkillEvidenceStrategy, RationaleCode
    from credit_harness.evidence.models import ClaimType as C
    from credit_harness.hypotheses.models import PriorityClass
    x = factory()
    for name, goal, claim in (("payment-skill", StrategyGoal.ESTABLISH_PAYMENT_FINALITY, C.PAYMENT_FINALITY),
        ("message-skill", StrategyGoal.INVESTIGATE_CALLBACK_CONSUMPTION, C.MESSAGE_CONSUME_STATUS)):
        add_skill(x, name, investigation_goals=(goal,), anti_patterns=(), evidence_strategy=(SkillEvidenceStrategy(
            goal=goal, recommended_claim_types=(claim,), priority=PriorityClass.SAFETY_CRITICAL, rationale_code=RationaleCode.CURRENT_GAP_DRIVEN),))
    repo, p, _, space, _ = setup(x, provider=OpenAIEmbeddingProvider())
    snap = context(x)
    vector = p.embed_query("Find out whether money actually reached the recipient after the lender response expired.")
    candidates, _ = repo.search(DocumentType.SKILL, space, repo.sources.query_scope(snap), snap.case_id, vector)
    assert candidates[0].document_id == "payment-skill"


@pytest.mark.llm
def test_semantic_paraphrase_retrieves_expected_experience(factory):
    import os
    if os.getenv("RUN_EMBEDDING_TESTS") != "1" or not os.getenv("OPENAI_API_KEY"):
        pytest.skip("real semantic Experience quality needs explicit embedding provider configuration")
    from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
    from credit_harness.domain.enums import ScenarioId
    a = factory("CASE-CALLBACK-HISTORY", closed=True)
    expected = a.publisher.publish(a.case.case_id)
    b = factory("CASE-FAILURE-HISTORY", closed=True, scenario=ScenarioId.S3)
    b.publisher.publish(b.case.case_id)
    x = factory("CASE-CURRENT"); x.advance(200); x.read(ToolName.CALLBACK)
    repo, p, _, space, _ = setup(x, provider=OpenAIEmbeddingProvider())
    snap = context(x)
    vector = p.embed_query("Money reached its recipient but an incoming event could not be deserialized because its format changed.")
    candidates, _ = repo.search(DocumentType.EXPERIENCE, space, repo.sources.query_scope(snap), snap.case_id, vector)
    assert candidates[0].document_id == expected.experience_id
