"""Trusted administration CLI, never registered as an Agent tool."""
import argparse
import json
import os
from datetime import datetime, timezone
from credit_harness.persistence.store import open_engine
from credit_harness.cases.repository import CaseRepository
from credit_harness.memory.skills import SQLSkillRepository
from credit_harness.memory.repository import SQLExperienceRepository
from credit_harness.retrieval.source import MemorySources
from credit_harness.retrieval.tables import create_retrieval_schema
from credit_harness.retrieval.repository import PgVectorRepository
from credit_harness.retrieval.embedding import DeterministicFakeEmbeddingProvider
from credit_harness.retrieval.indexer import EmbeddingIndexer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "work", "activate", "rollback"))
    parser.add_argument("--production", action="store_true", help="require migrated DB and index administration identity")
    parser.add_argument("--actor", help="audited administrator identifier for rollback")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--space-id")
    parser.add_argument("--provider", choices=("fake", "openai"), default="fake")
    parser.add_argument("--max-jobs", type=int, default=100)
    args = parser.parse_args()
    if args.command != "prepare" and not args.space_id: parser.error("--space-id required")
    if args.max_jobs < 1: parser.error("--max-jobs must be positive")
    if args.command == 'rollback' and not args.actor: parser.error('--actor required for rollback')
    engine = open_engine(os.environ['MIGRATION_DATABASE_URL' if args.production else 'SIM_DATABASE_URL'])
    try:
        sources = MemorySources(SQLSkillRepository(engine, args.tenant), SQLExperienceRepository(CaseRepository(engine, args.tenant)))
        repo = PgVectorRepository(sources)
        if args.production:
            from credit_harness.production.app import schema_ready
            if not schema_ready(engine): raise RuntimeError('DATABASE_SCHEMA_NOT_READY')
        else:
            create_retrieval_schema(engine)
        # Activation is deterministic administration and needs no Provider key.
        if args.command == "activate":
            repo.activate_space(args.space_id)
            print(json.dumps({"space_id": args.space_id, "status": "ACTIVE"}))
            return
        if args.provider == "openai":
            from credit_harness.adapters.openai_embedding import OpenAIEmbeddingProvider
            provider = OpenAIEmbeddingProvider()
        else:
            provider = DeterministicFakeEmbeddingProvider()
        now = lambda: datetime.now(timezone.utc)
        if args.command == 'rollback':
            repo.rollback_space(args.space_id, provider, actor=args.actor, now=now())
            print(json.dumps({'space_id': args.space_id, 'status':'ACTIVE', 'action':'ROLLBACK'}))
            return
        worker = EmbeddingIndexer(repo, provider, clock=now)
        if args.command == "prepare":
            space = repo.create_space(provider, now())
            count = worker.reconcile(space)
            print(json.dumps({"space_id": space.space_id, "state": space.status, "active_sources_scanned": count}))
        else:
            outcomes = []
            for _ in range(args.max_jobs):
                result = worker.run_one(args.space_id)
                if result is None: break
                outcomes.append(result.value)
            print(json.dumps({"processed": len(outcomes), "completed": outcomes.count("COMPLETED"), "failed": outcomes.count("FAILED")}))
    finally:
        engine.dispose()


if __name__ == "__main__": main()
