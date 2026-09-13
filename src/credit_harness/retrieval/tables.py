"""Separate metadata: SQLite explicitly has test JSON vectors, PostgreSQL vector."""
import json
from sqlalchemy import JSON, String, Integer, Float, Index, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dimension=None):
        self.dimension = dimension

    def get_col_spec(self, **kw):
        return "vector" if self.dimension is None else f"vector({self.dimension})"

    def bind_processor(self, dialect):
        return lambda value: json.dumps(value) if value is not None else None

    def result_processor(self, dialect, coltype):
        return lambda value: json.loads(value) if isinstance(value, str) else value


class RetrievalBase(DeclarativeBase):
    pass


class SpaceRow(RetrievalBase):
    __tablename__ = "retrieval_embedding_spaces"
    space_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSON)
    __table_args__ = (Index("uq_retrieval_active_space", "status", unique=True,
        postgresql_where=text("status = 'ACTIVE'"), sqlite_where=text("status = 'ACTIVE'")),)


class SpaceHeadRow(RetrievalBase):
    __tablename__ = "retrieval_space_head"
    singleton: Mapped[int] = mapped_column(Integer, primary_key=True)
    active_space: Mapped[str | None] = mapped_column(String(64))


class VectorColumns:
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    embedding_space_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64))
    source_status: Mapped[str] = mapped_column(String(16))
    source_case_id: Mapped[str | None] = mapped_column(String(128))
    business_domain: Mapped[str | None] = mapped_column(String(64))
    environment: Mapped[str | None] = mapped_column(String(64))
    funding_partner: Mapped[str | None] = mapped_column(String(128))
    asset_partner: Mapped[str | None] = mapped_column(String(128))
    guarantee_partner: Mapped[str | None] = mapped_column(String(128))
    product_code: Mapped[str | None] = mapped_column(String(128))
    protocol_versions: Mapped[str] = mapped_column(String(1024))
    applicable_since: Mapped[str] = mapped_column(String(40))
    applicable_until: Mapped[str | None] = mapped_column(String(40))
    structured_metadata: Mapped[dict] = mapped_column("metadata", JSON)
    embedding: Mapped[list] = mapped_column(JSON().with_variant(Vector(), "postgresql"))
    indexed_at: Mapped[str] = mapped_column(String(40))


class SkillVectorRow(VectorColumns, RetrievalBase):
    __tablename__ = "skill_vector_index"
    __table_args__ = (Index("ix_skill_vector_scope", "tenant_id", "source_status", "embedding_space_id", "business_domain", "funding_partner", "product_code"),)


class ExperienceVectorRow(VectorColumns, RetrievalBase):
    __tablename__ = "experience_vector_index"
    __table_args__ = (Index("ix_experience_vector_scope", "tenant_id", "source_status", "embedding_space_id", "business_domain", "funding_partner", "product_code"),)


class IndexJobRow(RetrievalBase):
    __tablename__ = "retrieval_embedding_index_jobs"
    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    document_type: Mapped[str] = mapped_column(String(16))
    document_id: Mapped[str] = mapped_column(String(80))
    source_version: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_space_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[str | None] = mapped_column(String(40))
    next_eligible_at: Mapped[str] = mapped_column(String(40))
    __table_args__ = (UniqueConstraint("tenant_id", "document_type", "document_id", "source_version", "content_hash", "embedding_space_id"),
        Index("ix_embedding_job_ready", "status", "next_eligible_at", "lease_until"))


def create_retrieval_schema(engine):
    from credit_harness.production.schema import runtime_managed
    if runtime_managed(engine):
        return  # schema version is validated by the production startup gate
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    RetrievalBase.metadata.create_all(engine)
    from sqlalchemy.orm import Session
    with Session(engine) as session, session.begin():
        if session.get(SpaceHeadRow, 1) is None:
            session.add(SpaceHeadRow(singleton=1, active_space=None))
