"""Shared cache stores only validated, human-safe projections, never runtime rows."""
from datetime import datetime, timezone
from sqlalchemy import JSON, Float, String, delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, Session
from credit_harness.persistence.store import Base
from credit_harness.context.budget import digest
from credit_harness.domain.models import Model
from credit_harness.api.ui import UIEvidence
from .models import InvestigationFrame, TraceItem, FrameStale


class SafeFramePayload(Model):
    version: str = "1"
    frame: InvestigationFrame
    evidence: tuple[UIEvidence, ...]
    trace_pages: dict[str, tuple[TraceItem, ...]]


class InvestigationFrameRow(Base):
    __tablename__ = "investigation_frames"
    frame_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    case_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float] = mapped_column(Float, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), "postgresql"))


class SQLFrameCache:
    def __init__(self, engine, *, clock=lambda: datetime.now(timezone.utc).timestamp()):
        self.engine, self.clock = engine, clock

    def put(self, tenant_id, frame, pages, ttl):
        safe = SafeFramePayload(frame=frame, evidence=tuple(pages["evidence"]),
            trace_pages={k: tuple(v) for k, v in pages.items() if k != "evidence"})
        payload = safe.model_dump(mode="json")
        now = self.clock()
        # First committer owns assembled_at and the immutable projection. A
        # duplicate build never overwrites or renews an existing frame's TTL.
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        with Session(self.engine) as session, session.begin():
            session.execute(insert(InvestigationFrameRow).values(frame_id=frame.frame_id,
                tenant_id=tenant_id, case_id=frame.case_id, created_at=now, expires_at=now+ttl,
                content_hash=digest(payload), payload=payload).on_conflict_do_nothing(index_elements=["frame_id"]))
        return self.get(tenant_id, frame.case_id, frame.frame_id)

    def get(self, tenant_id, case_id, frame_id):
        with Session(self.engine) as session:
            row = session.get(InvestigationFrameRow, frame_id)
            if not row or row.tenant_id != tenant_id or row.case_id != case_id or row.expires_at <= self.clock():
                raise FrameStale("FRAME_EXPIRED")
            if digest(row.payload) != row.content_hash:
                raise FrameStale("FRAME_CORRUPT")
            safe = SafeFramePayload.model_validate(row.payload)
            if safe.frame.frame_id != frame_id or safe.frame.case_id != case_id:
                raise FrameStale("FRAME_CORRUPT")
            return safe.frame, {**safe.trace_pages, "evidence": safe.evidence}

    def prune(self):
        with self.engine.begin() as connection:
            return connection.execute(delete(InvestigationFrameRow).where(
                InvestigationFrameRow.expires_at <= self.clock())).rowcount
