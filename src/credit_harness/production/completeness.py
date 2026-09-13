"""O(1) admission against database-triggered generations; reconcile owns scans."""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.retrieval.models import DocumentType, RetrievalError
from credit_harness.retrieval.indexer import insert_for
from .tables import SourceGenerationRow, IndexCompletenessRow


def require_ready(session, tenant, space):
    for kind in DocumentType:
        generation = session.get(SourceGenerationRow, (tenant, kind.value))
        state = session.get(IndexCompletenessRow, (tenant, kind.value, space))
        if not state or state.indexed_generation != (generation.generation if generation else 0):
            raise RetrievalError("index generation not ready")


def reconcile_completeness(repository, space):
    engine, tenant = repository.engine, repository.sources.tenant_id
    with engine.connect() as connection:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(isolation_level="REPEATABLE READ")
        with connection.begin(), Session(connection) as session:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("BEGIN")
            generations = {r.document_type: r.generation for r in session.scalars(
                select(SourceGenerationRow).where(SourceGenerationRow.tenant_id == tenant))}
            repository.assert_complete(session, space)
            for kind in DocumentType:
                values = dict(tenant_id=tenant, document_type=kind.value, space_id=space.space_id,
                    indexed_generation=generations.get(kind.value, 0),
                    last_reconciled_at=datetime.now(timezone.utc).timestamp())
                session.execute(insert_for(engine, IndexCompletenessRow).values(**values).on_conflict_do_update(
                    index_elements=["tenant_id", "document_type", "space_id"], set_={
                        k: v for k, v in values.items() if k in ("indexed_generation", "last_reconciled_at")}))
