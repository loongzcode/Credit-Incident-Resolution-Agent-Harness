"""Post-commit reconciliation and durable, fenced embedding jobs."""
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update, or_, and_
from sqlalchemy.orm import Session
from credit_harness.context.budget import digest
from .models import DocumentType as D, EmbeddingIndexJob, JobStatus, RetrievalError, SpaceStatus
from .tables import IndexJobRow
from .repository import timestamp, vector_table
from .source import SCOPE_FIELDS
from .projection import embedding_text
from .embedding import normalize


def insert_for(engine, table):
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table)


def job_model(row):
    return EmbeddingIndexJob(**{name: getattr(row, name) for name in EmbeddingIndexJob.model_fields})


class EmbeddingIndexer:
    def __init__(self, repository, provider, *, clock):
        self.repository, self.provider, self.clock = repository, provider, clock
        self.sources, self.engine = repository.sources, repository.engine

    def enqueue(self, document, space):
        if document.scope.tenant_id != self.sources.tenant_id:
            raise RetrievalError("foreign indexing source")
        self.repository.validate_provider(space, self.provider)
        if space.status == SpaceStatus.RETIRED:
            raise RetrievalError("retired embedding space")
        # Source was loaded from a separate, committed primary transaction.
        fields = dict(tenant_id=document.scope.tenant_id, document_type=document.kind.value,
            document_id=document.document_id, source_version=document.version,
            content_hash=document.content_hash, embedding_space_id=space.space_id)
        identity = digest(fields)
        with Session(self.engine) as session, session.begin():
            session.execute(insert_for(self.engine, IndexJobRow).values(job_id=identity, **fields,
                status="PENDING", attempt=0, next_eligible_at=timestamp(self.clock())).on_conflict_do_nothing())
            # A lost/rebuilt index can be restored without altering business rows.
            key = (document.scope.tenant_id, document.document_id, document.version, space.space_id)
            row = session.get(vector_table(document.kind), key)
            if row is None or row.content_hash != document.content_hash or row.source_hash != document.source_hash:
                session.execute(update(IndexJobRow).where(IndexJobRow.job_id == identity,
                    IndexJobRow.status == "COMPLETED").values(status="PENDING", next_eligible_at=timestamp(self.clock())))
        return identity

    def reconcile(self, space):
        # Restartable scan repairs the post-commit enqueue window. No embedding
        # network call here and no network is added to Publisher's transaction.
        return sum(1 for document in self.sources.active_documents() if self.enqueue(document, space))

    def claim(self, space_id):
        now = self.clock()
        eligible = and_(IndexJobRow.tenant_id == self.sources.tenant_id,
            IndexJobRow.embedding_space_id == space_id,
            or_(and_(IndexJobRow.status.in_(["PENDING", "FAILED"]), IndexJobRow.next_eligible_at <= timestamp(now)),
                and_(IndexJobRow.status == "CLAIMED", IndexJobRow.lease_until <= timestamp(now))))
        with Session(self.engine) as session, session.begin():
            job_id = session.scalar(select(IndexJobRow.job_id).where(eligible).order_by(IndexJobRow.job_id).limit(1))
            if job_id is None:
                return None
            token = uuid4().hex
            result = session.execute(update(IndexJobRow).where(IndexJobRow.job_id == job_id, eligible).values(
                status="CLAIMED", attempt=IndexJobRow.attempt + 1, lease_token=token,
                lease_until=timestamp(now + timedelta(seconds=120))))
            if result.rowcount != 1:
                return None
            return job_model(session.get(IndexJobRow, job_id))

    def run_one(self, space_id):
        job = self.claim(space_id)
        if job is None:
            return None
        try:
            space = self.repository.get_space(space_id)
            self.repository.validate_provider(space, self.provider)
            if space.status == SpaceStatus.RETIRED:
                raise RetrievalError("retired space")
            document = self.sources.get(job.document_type, job.document_id, job.source_version)
            if document.content_hash != job.content_hash:
                raise RetrievalError("source projection changed")
            safe_text = embedding_text(document.projection)
            # NO database session/transaction remains open across this call.
            result = self.provider.embed_documents([safe_text])
            if len(result) != 1:
                raise RetrievalError("invalid embedding batch")
            vector = normalize(result[0], space.dimension)
            self.complete(job, document, vector)
            return JobStatus.COMPLETED
        except Exception:
            self.fail(job)
            return JobStatus.FAILED

    def complete(self, job, document, vector):
        space = self.repository.get_space(job.embedding_space_id)
        self.repository.validate_provider(space, self.provider)
        if space.status == SpaceStatus.RETIRED or job.tenant_id != self.sources.tenant_id:
            raise RetrievalError("ineligible embedding completion")
        vector = normalize(vector, space.dimension)
        now = self.clock()
        with Session(self.engine) as session, session.begin():
            # Fence stale workers BEFORE replacing the vector. Completion and
            # vector commit are atomic, so crash/reclaim cannot publish twice.
            fenced = session.execute(update(IndexJobRow).where(IndexJobRow.job_id == job.job_id,
                IndexJobRow.status == "CLAIMED", IndexJobRow.lease_token == job.lease_token,
                IndexJobRow.lease_until > timestamp(now)).values(status="COMPLETED", lease_token=None, lease_until=None))
            if fenced.rowcount != 1:
                raise RetrievalError("embedding lease lost")
            current = self.sources.get(job.document_type, job.document_id, job.source_version, session)
            if current.content_hash != job.content_hash or current.source_hash != document.source_hash:
                raise RetrievalError("source changed while embedding")
            scope = current.scope
            fields = dict(tenant_id=scope.tenant_id, document_id=current.document_id, source_version=current.version,
                embedding_space_id=job.embedding_space_id, content_hash=job.content_hash, source_hash=current.source_hash,
                source_status="ACTIVE", source_case_id=current.source_case_id,
                **{n: str(getattr(scope, n)) if getattr(scope, n) is not None else None for n in SCOPE_FIELDS},
                protocol_versions="".join(f"|{v}|" for v in scope.protocol_versions),
                applicable_since=timestamp(scope.applicable_since),
                applicable_until=timestamp(scope.applicable_until) if scope.applicable_until else None,
                metadata=scope.model_dump(mode="json"), embedding=vector, indexed_at=timestamp(now))
            table = vector_table(job.document_type).__table__
            statement = insert_for(self.engine, table).values(**fields)
            session.execute(statement.on_conflict_do_update(index_elements=["tenant_id", "document_id", "source_version", "embedding_space_id"],
                set_={k: v for k, v in fields.items() if k not in ("tenant_id", "document_id", "source_version", "embedding_space_id")}))

    def fail(self, job):
        with Session(self.engine) as session, session.begin():
            session.execute(update(IndexJobRow).where(IndexJobRow.job_id == job.job_id,
                IndexJobRow.status == "CLAIMED", IndexJobRow.lease_token == job.lease_token).values(
                status="FAILED", lease_token=None, lease_until=None,
                next_eligible_at=timestamp(self.clock() + timedelta(seconds=min(3600, 30 * 2**min(job.attempt-1, 7))))))
