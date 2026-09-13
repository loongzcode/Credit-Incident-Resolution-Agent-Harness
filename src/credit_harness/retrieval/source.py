"""Trusted source adapter. Embedding workers never inspect live evidence/PII."""
from dataclasses import dataclass
from sqlalchemy import select, func, tuple_
from sqlalchemy.orm import Session
from credit_harness.memory.tables import SkillRow, ExperienceRow
from credit_harness.memory.skills import checked_skill
from credit_harness.memory.repository import checked_experience
from credit_harness.memory.retrieval import current_signature
from .models import DocumentType as D, RetrievalScope, RetrievalError
from .projection import skill_document, experience_document, projection_hash

SCOPE_FIELDS = ("business_domain", "environment", "funding_partner", "asset_partner", "guarantee_partner", "product_code")


@dataclass(frozen=True)
class SourceDocument:
    kind: D
    document_id: str
    version: str
    source_hash: str
    source: object
    scope: RetrievalScope
    source_case_id: str | None

    @property
    def projection(self):
        return skill_document(self.source) if self.kind == D.SKILL else experience_document(self.source)

    @property
    def content_hash(self):
        return projection_hash(self.projection)

    def eligible(self, query_scope, case_id):
        """Recheck eligibility from validated primary content, never index hints."""
        scope = self.scope
        if (scope.tenant_id != query_scope.tenant_id or self.source_case_id == case_id
                or scope.applicable_since > query_scope.applicable_since
                or scope.applicable_until is not None and scope.applicable_until <= query_scope.applicable_since):
            return False
        for field in SCOPE_FIELDS:
            expected, current = getattr(scope, field), getattr(query_scope, field)
            if expected != current and (self.kind == D.EXPERIENCE or expected is not None):
                return False
        return not scope.protocol_versions or bool(set(scope.protocol_versions) & set(query_scope.protocol_versions))


class MemorySources:
    def __init__(self, skills, experiences, registry=None):
        if skills.tenant_id != experiences.tenant_id or skills.engine != experiences.engine:
            raise RetrievalError("source tenant or database mismatch")
        if registry is not None and registry.tenant_id != skills.tenant_id:
            raise RetrievalError("registry tenant mismatch")
        self.skills, self.experiences, self.registry = skills, experiences, registry
        self.engine, self.tenant_id = skills.engine, skills.tenant_id

    def query_scope(self, snapshot):
        self.experiences.cases.get(snapshot.case_id)
        signature = current_signature(snapshot)
        values = {n: getattr(signature, n, None) for n in SCOPE_FIELDS}
        protocol = signature.protocol_version
        if self.registry is not None:
            route = self.registry.route(snapshot.case_id)
            if route.tenant_id != self.tenant_id:
                raise RetrievalError("foreign route")
            if snapshot.capability_snapshot is not None:
                from credit_harness.registry.repository import fingerprint
                if snapshot.capability_snapshot.routing_context_fingerprint != fingerprint(route):
                    raise RetrievalError("snapshot route is stale")
            values = {n: getattr(route, n) for n in SCOPE_FIELDS}
            # Registry owns applicability. Never infer it from historical vectors.
            protocol = route.protocol_version
        return RetrievalScope(tenant_id=self.tenant_id, **values,
            protocol_versions=(protocol,) if protocol else (), applicable_since=snapshot.assembled_at)

    def _document(self, row, kind):
        if row is None or row.tenant_id != self.tenant_id or row.status != "ACTIVE":
            raise RetrievalError("inactive or unavailable primary source")
        if kind == D.SKILL:
            source = checked_skill(row)
            values = {n: getattr(source.scope, n) for n in SCOPE_FIELDS}
            scope = RetrievalScope(tenant_id=self.tenant_id, **values, protocol_versions=source.scope.protocol_versions,
                applicable_since=source.scope.applicable_since or source.created_at, applicable_until=source.scope.applicable_until)
            return SourceDocument(kind, source.skill_id, source.version, row.content_hash, source, scope, None)
        source = checked_experience(row, self.tenant_id)
        values = {n: getattr(source.partner_context, n) for n in SCOPE_FIELDS}
        protocols = source.partner_context.protocol_versions or source.protocol_context
        # Historical scope is immutable primary data, not today's mutable route.
        scope = RetrievalScope(tenant_id=self.tenant_id, **values, protocol_versions=protocols,
            applicable_since=source.applicable_since, applicable_until=source.applicable_until)
        return SourceDocument(kind, source.experience_id, source.experience_schema_version, source.experience_id,
            source, scope, source.source_case_id)

    def get(self, kind, document_id, version, session=None):
        if session is None:
            with Session(self.engine) as s:
                return self.get(kind, document_id, version, s)
        if kind == D.SKILL:
            row = session.get(SkillRow, (self.tenant_id, document_id, version))
        else:
            row = session.get(ExperienceRow, document_id)
            if row is not None and row.schema_version != version:
                raise RetrievalError("source version changed")
        return self._document(row, kind)

    def batch_get(self, kind, candidates):
        if not candidates:
            return ()
        kind = D(kind)
        table = SkillRow if kind == D.SKILL else ExperienceRow
        id_col = table.skill_id if kind == D.SKILL else table.experience_id
        version_col = table.version if kind == D.SKILL else table.schema_version
        keys = [(c.document_id, c.source_version) for c in candidates]
        with Session(self.engine) as session:
            rows = session.scalars(select(table).where(table.tenant_id == self.tenant_id,
                tuple_(id_col, version_col).in_(keys))).all()
            by_key = {(getattr(r, id_col.key), getattr(r, version_col.key)): r for r in rows}
            return tuple(self._document(by_key.get(key), kind) for key in keys)

    def active_documents(self, batch_size=250):
        # Administrative backfill only; query path never calls active()/scans JSON.
        for kind, row_type, key in ((D.SKILL, SkillRow, SkillRow.skill_id), (D.EXPERIENCE, ExperienceRow, ExperienceRow.experience_id)):
            with Session(self.engine) as session:
                rows = session.scalars(select(row_type).where(row_type.tenant_id == self.tenant_id,
                    row_type.status == "ACTIVE").order_by(key).execution_options(yield_per=batch_size))
                for row in rows:
                    yield self._document(row, kind)

    def primary_count(self, session, kind):
        row = SkillRow if kind == D.SKILL else ExperienceRow
        return session.scalar(select(func.count()).select_from(row).where(row.tenant_id == self.tenant_id, row.status == "ACTIVE"))
