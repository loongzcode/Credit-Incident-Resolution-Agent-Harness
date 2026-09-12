"""Synthetic retrieval experiment. No real company inventory or personal data.

Scale documents are labelled expansions of ONE verified synthetic seed, not a
claim that thousands of independent incidents were evaluated. Uses production
Indexer jobs and PostgreSQL distance SQL. Query vectors are not persisted.
"""
import argparse
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from credit_harness.persistence.store import open_engine
from credit_harness.benchmark.fixture import EvaluationFixture
from credit_harness.memory.tables import create_memory_schema, SkillRow, ExperienceRow
from credit_harness.memory.repository import SQLExperienceRepository, experience_identity
from credit_harness.memory.experience import VerifiedExperiencePublisher
from credit_harness.memory.skills import SQLSkillRepository, general_investigation_skill
from credit_harness.memory.models import IncidentSignature, SkillScope
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.budget import digest
from credit_harness.domain.enums import ToolName as T
from credit_harness.retrieval.source import MemorySources
from credit_harness.retrieval.embedding import DeterministicFakeEmbeddingProvider
from credit_harness.retrieval.repository import PgVectorRepository, ExactVectorTestRepository
from credit_harness.retrieval.tables import create_retrieval_schema, IndexJobRow, SkillVectorRow, ExperienceVectorRow
from credit_harness.retrieval.indexer import EmbeddingIndexer
from credit_harness.retrieval.service import HybridRetrievalService


def semantic_benchmark(provider):
    # Static synthetic paraphrases only; never derived from raw Case content.
    documents = [
        "Request transport timeout. Establish payment finality and payment identity before any further action.",
        "Callback gateway received. Message consume failed with schema mismatch. Compare protocol field types.",
        "Guarantee success. Asset delivery failed. Establish downstream notification convergence.",
    ]
    paraphrases = [
        "The lender might already have sent the funds despite an expired HTTP response; investigate settlement.",
        "The incoming event reached our endpoint but the consumer could not deserialize its changed format.",
        "Our core completed processing, yet the originating platform was not informed of the outcome.",
    ]
    vectors = provider.embed_documents(documents)
    ranks = []
    for expected, query in enumerate(paraphrases):
        vector = provider.embed_query(query)
        ordered = sorted(range(3), key=lambda i: (-sum(a*b for a, b in zip(vector, vectors[i])), i))
        ranks.append(ordered.index(expected)+1)
    return dict(provider=provider.provider, model=provider.model_id,
        evidence_level="ENGINEERING_ONLY_NOT_SEMANTIC_VALIDATION" if provider.provider == "deterministic-fake" else "SYNTHETIC_PROVIDER_SAMPLE_ONLY",
        sample_count=3, expected_ranks=ranks, paraphrase_recall_at_1=sum(r == 1 for r in ranks)/3,
        warning="A three-pair synthetic sample is not a production quality estimate; fake embeddings cannot establish semantic quality.")


def experiment(engine, provider, skills_count, experience_count, queries):
    started = perf_counter()
    instances = []
    try:
        h = EvaluationFixture(engine, case_id="CASE-RETRIEVAL-SEED"); instances.append(h)
        create_memory_schema(engine)
        h.read(); h.progress(); h.read(); h.closure.close(h.evaluate())
        memory = SQLExperienceRepository(h.cases, clock=lambda: h.clock.now)
        seed = VerifiedExperiencePublisher(memory).publish(h.case.case_id)
        # Bounded synthetic load expansion, preserving valid primary content IDs.
        with Session(engine) as s, s.begin():
            for n in range(experience_count-1):
                pattern = seed.incident_signature if n % 4 == 0 else IncidentSignature(message_consume_state="CONSUMED", asset_state="SUCCESS")
                e = seed.model_copy(update={"closure_id": digest({"synthetic_expansion": n}),
                    "incident_signature": pattern,
                    "observed_symptoms": seed.observed_symptoms.model_copy(update={"pattern": pattern}),
                    "partner_context": SkillScope(funding_partner="SYNTHETIC-OTHER") if n % 5 == 0 else SkillScope()})
                e = e.model_copy(update={"experience_id": experience_identity(e)})
                memory._insert(s, e)
                if n % 250 == 0: s.flush()
        c = EvaluationFixture(engine, case_id="CASE-RETRIEVAL-CURRENT"); instances.append(c)
        c.advance(200); c.read(T.TRACE, T.CALLBACK)
        skills = SQLSkillRepository(engine, c.case.tenant_id, clock=lambda: c.clock.now)
        for n in range(skills_count):
            skill = general_investigation_skill().model_copy(update={"skill_id": f"synthetic-skill-{n:05}",
                "scope": SkillScope(protocol_versions=("9.9",)) if n % 5 == 0 else SkillScope(),
                "applies_when": IncidentSignature(request_transport_status="TIMEOUT") if n % 4 == 0 else IncidentSignature()})
            skills.add(skill); skills.activate(skill.skill_id, skill.version)
        sources = MemorySources(skills, SQLExperienceRepository(c.cases))
        create_retrieval_schema(engine)
        repo = (PgVectorRepository if engine.dialect.name == "postgresql" else ExactVectorTestRepository)(sources)
        space = repo.create_space(provider, c.clock.now)
        worker = EmbeddingIndexer(repo, provider, clock=lambda: c.clock.now)
        worker.reconcile(space)
        while worker.run_one(space.space_id) is not None: pass
        repo.activate_space(space.space_id)
        indexed_at = perf_counter()
        service = HybridRetrievalService(repo, provider)
        guidance = service.guidance_provider()
        snap = ReasoningContextAssembler().build(c.cases.get(c.case.case_id), c.evidence.list(c.case.case_id))
        baseline = snap.model_dump_json()
        telemetry, sizes, selected = [], [], []
        for _ in range(queries):
            result = guidance.build_result(snap)
            if result.bundle is None:
                raise RuntimeError("synthetic retrieval experiment degraded")
            telemetry.append(service.last_telemetry)
            sizes.append(len(result.bundle.model_dump_json()))
            selected.append((result.bundle.skill_versions, result.bundle.experience_ids))
        assert baseline == snap.model_dump_json()
        # Current explicit no-disbursement observation must override history.
        c.progress(no_disbursement=True); c.read(T.PAYMENT)
        contrary = ReasoningContextAssembler().build(c.cases.get(c.case.case_id), c.evidence.list(c.case.case_id))
        guidance.build_result(contrary)
        assert any(f.claim_type.value == "PAYMENT_FINALITY" and f.value == "NOT_EXECUTED" for f in contrary.current_facts)
        with Session(engine) as s:
            counts = {row.__tablename__: s.scalar(select(func.count()).select_from(row)) for row in
                (SkillRow, ExperienceRow, SkillVectorRow, ExperienceVectorRow, IndexJobRow)}
        def summary(field):
            values = sorted(getattr(t, field) for t in telemetry)
            return dict(median_ms=round(statistics.median(values), 3), p95_ms=round(values[min(len(values)-1, int(len(values)*.95))], 3))
        return dict(generated_at=datetime.now(timezone.utc).isoformat(), database=engine.dialect.name, embedding_provider=provider.provider, model=provider.model_id,
            synthetic_expansions=True, independent_verified_seed_cases=1, counts=counts,
            corpus_ratio=f"{skills_count}/1000 skills; {experience_count}/100000 experiences",
            indexing_seconds=round(indexed_at-started, 3), query_count=queries,
            vector_recall=summary("vector_latency_ms"), rerank=summary("rerank_latency_ms"), total_retrieval=summary("latency_ms"),
            guidance_context_chars_max=max(sizes), stable_selection=all(v == selected[0] for v in selected),
            current_not_executed_preserved=True, latest_telemetry=telemetry[-1].model_dump(mode="json"),
            semantic_benchmark=semantic_benchmark(provider))
    finally:
        for fixture in reversed(instances): fixture.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=os.getenv("TEST_POSTGRES_URL"))
    parser.add_argument("--skills", type=int, default=10)
    parser.add_argument("--experiences", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--output", type=Path, default=Path(".local/retrieval-benchmark.json"))
    args = parser.parse_args()
    if min(args.skills, args.experiences, args.queries) < 1: parser.error("counts must be positive")
    os.environ.setdefault("CAPABILITY_SIGNING_SECRET", "synthetic-retrieval-benchmark-only-secret-0001")
    if args.provider == "openai":
        from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
        provider = OpenAIEmbeddingProvider()
    else: provider = DeterministicFakeEmbeddingProvider()
    base = open_engine(args.database_url or f"sqlite:///.local/retrieval-benchmark-{uuid4().hex}.db")
    schema = "retrieval_benchmark_" + uuid4().hex
    engine = base
    if base.dialect.name == "postgresql":
        with base.begin() as conn: conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        engine = base.execution_options(schema_translate_map={None: schema})
    try:
        report = experiment(engine, provider, args.skills, args.experiences, args.queries)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        if base.dialect.name == "postgresql":
            # Only the fresh random schema created by this invocation is dropped.
            with base.begin() as conn: conn.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        base.dispose()


if __name__ == "__main__": main()
