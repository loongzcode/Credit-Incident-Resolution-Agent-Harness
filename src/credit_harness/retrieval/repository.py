from datetime import datetime, timezone
from sqlalchemy import select, update, func, or_, and_, cast, bindparam, Float, Index
from sqlalchemy.orm import Session
from credit_harness.memory.tables import SkillRow, ExperienceRow
from credit_harness.context.budget import digest
from .tables import SpaceRow, SpaceHeadRow, SkillVectorRow, ExperienceVectorRow, Vector
from .models import (EmbeddingSpace, SpaceStatus, DocumentType as D, RetrievalError, VectorCandidate,
    PROJECTION_VERSION, NORMALIZATION_VERSION, EMBEDDING_CONTRACT_VERSION)
from .embedding import normalize
from .source import SCOPE_FIELDS


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat()


def vector_table(kind):
    return SkillVectorRow if D(kind) == D.SKILL else ExperienceVectorRow


def primary_join(kind):
    v = vector_table(kind)
    if kind == D.SKILL:
        return SkillRow, and_(v.tenant_id == SkillRow.tenant_id, v.document_id == SkillRow.skill_id,
            v.source_version == SkillRow.version, v.source_hash == SkillRow.content_hash)
    return ExperienceRow, and_(v.tenant_id == ExperienceRow.tenant_id, v.document_id == ExperienceRow.experience_id,
        v.source_version == ExperienceRow.schema_version, v.source_hash == ExperienceRow.experience_id,
        v.source_case_id == ExperienceRow.source_case_id)


def scope_filters(row, scope, case_id):
    filters = [row.tenant_id == scope.tenant_id, row.source_status == "ACTIVE",
        row.applicable_since <= timestamp(scope.applicable_since),
        or_(row.applicable_until.is_(None), row.applicable_until > timestamp(scope.applicable_since)),
        or_(row.source_case_id.is_(None), row.source_case_id != case_id)]
    for field in SCOPE_FIELDS:
        column, value = getattr(row, field), getattr(scope, field)
        # Skills may explicitly be generic. Historical scope missing a known
        # route dimension is not evidence of cross-partner applicability.
        filters.append(column.is_(None) if value is None else or_(
            and_(row.source_case_id.is_(None), column.is_(None)), column == str(value)))
    # Scoped documents do not qualify if the current protocol is unknown.
    filters.append(or_(row.protocol_versions == "", *[
        row.protocol_versions.contains(f"|{version}|", autoescape=True) for version in scope.protocol_versions]))
    return filters


class VectorRepository:
    """Shared durable index lifecycle. Production distance implementation below."""
    def __init__(self, sources):
        self.sources, self.engine = sources, sources.engine

    def create_space(self, provider, now):
        fields = dict(provider=provider.provider, model_id=provider.model_id, dimension=provider.dimension,
            projection_version=PROJECTION_VERSION, normalization_version=NORMALIZATION_VERSION,
            embedding_contract_version=provider.embedding_contract_version)
        space = EmbeddingSpace(space_id=digest(fields), created_at=now, **fields)
        with Session(self.engine) as session, session.begin():
            old = session.get(SpaceRow, space.space_id)
            if old:
                space = self.read_space(old)
            else:
                session.add(SpaceRow(space_id=space.space_id, status=space.status.value, payload=space.model_dump(mode="json")))
        if self.engine.dialect.name == "postgresql":
            for kind in D:
                row = vector_table(kind)
                index = Index(f"ix_{kind.value.lower()}_hnsw_{space.space_id[:20]}",
                    cast(row.embedding, Vector(space.dimension)).label("embedding"),
                    postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"},
                    postgresql_where=row.embedding_space_id == space.space_id)
                try:
                    index.create(self.engine, checkfirst=True)
                finally:
                    # Dynamic per-space DDL must not pollute global test metadata.
                    row.__table__.indexes.discard(index)
        return space

    @staticmethod
    def read_space(row):
        if row is None:
            raise RetrievalError("embedding space unavailable")
        space = EmbeddingSpace.model_validate(row.payload).model_copy(update={"status": SpaceStatus(row.status)})
        identity = space.model_dump(mode="json", exclude={"space_id", "status", "created_at"})
        if digest(identity) != space.space_id or row.space_id != space.space_id:
            raise RetrievalError("embedding space integrity failed")
        return space

    def get_space(self, space_id):
        with Session(self.engine) as session:
            return self.read_space(session.get(SpaceRow, space_id))

    def active_space(self):
        with Session(self.engine) as session:
            head = session.get(SpaceHeadRow, 1)
            space = self.read_space(session.get(SpaceRow, head.active_space)) if head and head.active_space else None
            if space is None or space.status != SpaceStatus.ACTIVE:
                raise RetrievalError("no active embedding space")
            return space

    def assert_complete(self, session, space, *, all_tenants=False):
        for kind in D:
            vector = vector_table(kind)
            primary, binding = primary_join(kind)
            source_query = select(func.count()).select_from(primary).where(primary.status == "ACTIVE")
            indexed_query = select(func.count()).select_from(vector).join(primary, binding).where(
                primary.status == "ACTIVE", vector.source_status == "ACTIVE", vector.embedding_space_id == space.space_id)
            if not all_tenants:
                source_query = source_query.where(primary.tenant_id == self.sources.tenant_id)
                indexed_query = indexed_query.where(vector.tenant_id == self.sources.tenant_id)
            if session.scalar(source_query) != session.scalar(indexed_query):
                raise RetrievalError("index stale or backfill incomplete")

    def activate_space(self, space_id):
        # One global space pointer. Serialized switch; no provider call inside.
        with Session(self.engine) as session, session.begin():
            session.execute(update(SpaceHeadRow).where(SpaceHeadRow.singleton == 1).values(singleton=1))
            head = session.get(SpaceHeadRow, 1)
            row = session.get(SpaceRow, space_id)
            space = self.read_space(row)
            if space.status != SpaceStatus.BUILDING:
                raise RetrievalError("only a building space can be activated")
            self.assert_complete(session, space, all_tenants=True)
            session.execute(update(SpaceRow).where(SpaceRow.status == "ACTIVE").values(status="RETIRED"))
            row.status = "ACTIVE"
            head.active_space = space_id

    @staticmethod
    def validate_provider(space, provider):
        if (space.provider, space.model_id, space.dimension, space.embedding_contract_version,
                space.projection_version, space.normalization_version) != (
                provider.provider, provider.model_id, provider.dimension, provider.embedding_contract_version,
                PROJECTION_VERSION, NORMALIZATION_VERSION):
            raise RetrievalError("embedding space/provider mismatch")

    def _eligible(self, kind, space, scope, case_id):
        if scope.tenant_id != self.sources.tenant_id:
            raise RetrievalError("foreign retrieval scope")
        row = vector_table(kind)
        primary, binding = primary_join(kind)
        return select(row).join(primary, binding).where(primary.status == "ACTIVE",
            row.embedding_space_id == space.space_id, *scope_filters(row, scope, case_id))

    def _candidate(self, kind, row, distance):
        return VectorCandidate(document_type=kind, document_id=row.document_id, source_version=row.source_version,
            content_hash=row.content_hash, distance=max(0., min(2., float(distance))),
            specificity=sum(getattr(row, n) is not None for n in SCOPE_FIELDS) + bool(row.protocol_versions))

    def _admit(self, session, space, vector, top_k):
        current = self.read_space(session.get(SpaceRow, space.space_id))
        if current.status != SpaceStatus.ACTIVE or current != space or not 1 <= top_k <= 20:
            raise RetrievalError("invalid recall space or budget")
        self.assert_complete(session, space)
        return normalize(vector, space.dimension)


class PgVectorRepository(VectorRepository):
    def __init__(self, sources):
        super().__init__(sources)
        if self.engine.dialect.name != "postgresql":
            raise RetrievalError("PostgreSQL is required for pgvector")

    def search(self, kind, space, scope, case_id, vector, top_k=20):
        row = vector_table(kind)
        with Session(self.engine) as session:
            vector = self._admit(session, space, vector, top_k)
            # Iterative scans keep filtered ANN recall useful. SQL eligibility is
            # applied before returned candidates; final primary checks run again.
            session.connection().exec_driver_sql("SET LOCAL hnsw.iterative_scan = 'strict_order'")
            session.connection().exec_driver_sql("SET LOCAL hnsw.ef_search = 100")
            eligible = self._eligible(kind, space, scope, case_id)
            count = session.scalar(select(func.count()).select_from(eligible.subquery()))
            distance = cast(row.embedding, Vector(space.dimension)).op("<=>", return_type=Float)(
                bindparam("query_vector", vector, type_=Vector(space.dimension)))
            query = eligible.add_columns(distance.label("distance")).order_by(distance, row.document_id, row.source_version).limit(top_k)
            return tuple(self._candidate(kind, r, d) for r, d in session.execute(query)), count


class ExactVectorTestRepository(VectorRepository):
    """SQLite-only exact cosine test double, never a production fallback."""
    def __init__(self, sources):
        super().__init__(sources)
        if self.engine.dialect.name != "sqlite":
            raise RetrievalError("exact vector repository is SQLite test-only")

    def search(self, kind, space, scope, case_id, vector, top_k=20):
        with Session(self.engine) as session:
            vector = self._admit(session, space, vector, top_k)
            rows = session.scalars(self._eligible(kind, space, scope, case_id)).all()
            results = [self._candidate(kind, r, 1 - sum(a*b for a, b in zip(vector, normalize(r.embedding, space.dimension)))) for r in rows]
            return tuple(sorted(results, key=lambda r: (r.distance, r.document_id, r.source_version))[:top_k]), len(rows)
