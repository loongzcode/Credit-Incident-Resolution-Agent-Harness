"""Optional append-only sink for existing Agent and retrieval instrumentation.

Only explicit safe projections are persisted here. The UI cannot append.
"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import JSON, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, Session
from credit_harness.persistence.store import Base
from credit_harness.agent.models import AgentRunResult
from credit_harness.retrieval.models import RetrievalTelemetry
from .projection import planner_items, entry, alias
from .models import TraceKind, TraceItem


class InvestigationTraceRow(Base):
    __tablename__ = "investigation_safe_traces"
    trace_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)


class SQLInvestigationTraceStore:
    def __init__(self, cases):
        self.cases = cases
        self._records = []

    @property
    def records(self):
        # Compatibility with the existing trusted in-process inspector only.
        # These objects are never read by the HTTP projection.
        return tuple(self._records)

    def create_schema(self):
        InvestigationTraceRow.__table__.create(self.cases.engine, checkfirst=True)

    def record_guidance(self, snapshot, bundle):
        from credit_harness.memory.guidance import validate_guidance
        bundle = validate_guidance(bundle, snapshot)
        if bundle.tenant_id != self.cases.tenant_id:
            raise ValueError("guidance tenant mismatch")
        items = []
        for skill in bundle.active_skills:
            data = {"skill_id": skill.skill.skill_id, "version": skill.skill.version,
                "strategies": "; ".join(s.goal.value + ': ' + ', '.join(c.value for c in s.recommended_claim_types) for s in skill.evidence_strategy),
                "invariants": ", ".join(i.invariant.value for i in skill.safety_invariants)}
            from credit_harness.memory.tables import SkillRow
            from credit_harness.memory.skills import checked_skill
            from credit_harness.evaluation.snapshot import table_exists
            with Session(self.cases.engine) as session:
                saved = session.get(SkillRow, (self.cases.tenant_id, skill.skill.skill_id, skill.skill.version)) if table_exists(session, SkillRow) else None
                if saved:
                    definition = checked_skill(saved)
                    for name, value in definition.scope.model_dump(mode="json").items():
                        data["scope_" + name] = value
                else:
                    data["applicable_scope"] = "NOT_RECORDED"
            items.append(entry(TraceKind.KNOWLEDGE, bundle.guidance_fingerprint + skill.skill.skill_id,
                "ORGANIZATIONAL_GUIDANCE", data, tuple(data), at=snapshot.assembled_at,
                warning="Organizational guidance; not current Evidence", historical=True))
        for experience in bundle.verified_experiences:
            data = {"experience_alias": alias(experience.experience_id, "EXPERIENCE"),
                "outcome": experience.verified_outcome_path.value,
                "similarity_features": ", ".join(s.value for s in experience.similarity_features),
                "tool_sequence": ", ".join(t.value for t in experience.observed_tool_sequence),
                "lessons": ", ".join(s.value for s in experience.verified_safety_lessons)}
            for key, value in experience.observed_pattern.model_dump(mode="json").items():
                if key not in ('funding_partner', 'asset_partner', 'product_code') and value is not None:
                    data['pattern_' + key] = value
            items.append(entry(TraceKind.KNOWLEDGE, bundle.guidance_fingerprint + experience.experience_id,
                "VERIFIED_HISTORICAL_GUIDANCE", data, tuple(data), at=snapshot.assembled_at,
                warning="HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE", historical=True))
        self._save(snapshot.case_id, items)

    def append(self, result: AgentRunResult):
        result = AgentRunResult.model_validate(result.model_dump())
        self._save(result.case_id, planner_items(result.model_dump(mode="json")))
        self._records.append(result)

    def record_planning(self, snapshot, decision, guidance=None):
        from credit_harness.planner.models import PlannerDecision
        decision = PlannerDecision.model_validate(decision.model_dump())
        if decision.snapshot_id != snapshot.snapshot_id:
            raise ValueError("planner trace snapshot mismatch")
        items = planner_items({"turns": [{"attempts": [{"decision": decision.model_dump(mode="json")}]}]})
        self._save(snapshot.case_id, items)
        if guidance is not None:
            self.record_guidance(snapshot, guidance)

    def record_retrieval(self, *, case_id, snapshot_id, telemetry: RetrievalTelemetry):
        telemetry = RetrievalTelemetry.model_validate(telemetry.model_dump())
        data = telemetry.model_dump()
        data["embedding_space"] = alias(data["embedding_space"], "SPACE") if data["embedding_space"] else "UNAVAILABLE"
        data["selected_experience_ids"] = ", ".join(alias(e, "EXPERIENCE") for e in telemetry.selected_experience_ids)
        data["selected_skill_ids"] = ", ".join(telemetry.selected_skill_ids)
        data["snapshot_id"] = snapshot_id
        self._save(case_id, [entry(TraceKind.KNOWLEDGE, uuid4().hex, "RECORDED", data, tuple(data),
            at=datetime.now(timezone.utc), historical=True,
            warning="HISTORICAL GUIDANCE; NOT CURRENT CASE EVIDENCE")])

    def _save(self, case_id, items):
        with Session(self.cases.engine) as session, session.begin():
            self.cases._row(session, case_id)
            for item in items:
                safe = TraceItem.model_validate(item.model_dump())
                if session.get(InvestigationTraceRow, safe.trace_id) is None:
                    session.add(InvestigationTraceRow(trace_id=safe.trace_id, case_id=case_id,
                        tenant_id=self.cases.tenant_id, payload=safe.model_dump(mode="json")))


class TracedGuidanceProvider:
    """Trusted wiring wrapper: captures actual selected guidance, never retrieves anew."""
    def __init__(self, provider, store, *, hybrid=None):
        self.provider, self.store, self.hybrid = provider, store, hybrid

    def build_result(self, snapshot):
        before = self.hybrid.telemetry_revision if self.hybrid is not None else None
        result = self.provider.build_result(snapshot)
        # Early provider failure may not have performed retrieval at all. Never
        # bind a previous Case's ContextVar telemetry to this snapshot.
        if self.hybrid is not None and self.hybrid.telemetry_revision != before:
            self.store.record_retrieval(case_id=snapshot.case_id, snapshot_id=snapshot.snapshot_id,
                telemetry=self.hybrid.last_telemetry)
        if result.bundle is not None:
            self.store.record_guidance(snapshot, result.bundle)
        return result
