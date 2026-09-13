"""Optional append-only sink for existing Agent and retrieval instrumentation.

Only explicit safe projections are persisted here. The UI cannot append.
"""
from datetime import datetime, timezone
from functools import wraps
from credit_harness.context.budget import digest
from .privacy import alias_scope
from credit_harness.production.tables import RetrievalMetricRow
from sqlalchemy import JSON, String, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, Session
from credit_harness.persistence.store import Base
from credit_harness.agent.models import AgentRunResult
from credit_harness.retrieval.models import RetrievalTelemetry
from .projection import planner_items, entry, alias
from .models import TraceKind, TraceItem, TraceTrustClass


class InvestigationTraceRow(Base):
    __tablename__ = "investigation_safe_traces"
    __table_args__ = (Index('ix_safe_trace_projection','projection_version'),)
    trace_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSON)
    projection_version: Mapped[str] = mapped_column(String(16), server_default="2")


def tenant_projection(method):
    @wraps(method)
    def scoped(self, *args, **kwargs):
        with alias_scope(self.cases.tenant_id, self.alias_key):
            return method(self, *args, **kwargs)
    return scoped


class SQLInvestigationTraceStore:
    def __init__(self, cases, *, alias_key=None):
        self.cases = cases
        self.alias_key = alias_key
        self._records = []

    @property
    def records(self):
        # Compatibility with the existing trusted in-process inspector only.
        # These objects are never read by the HTTP projection.
        return tuple(self._records)

    def create_schema(self):
        from credit_harness.production.schema import runtime_managed
        if runtime_managed(self.cases.engine):
            return
        InvestigationTraceRow.__table__.create(self.cases.engine, checkfirst=True)
        RetrievalMetricRow.__table__.create(self.cases.engine, checkfirst=True)

    @tenant_projection
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

    @tenant_projection
    def append(self, result: AgentRunResult):
        result = AgentRunResult.model_validate(result.model_dump())
        self._save(result.case_id, planner_items(result.model_dump(mode="json")))
        from credit_harness.production.schema import runtime_managed
        if not runtime_managed(self.cases.engine):
            self._records.append(result)

    @tenant_projection
    def record_planning(self, snapshot, decision, guidance=None):
        from credit_harness.planner.models import PlannerDecision
        decision = PlannerDecision.model_validate(decision.model_dump())
        if decision.snapshot_id != snapshot.snapshot_id:
            raise ValueError("planner trace snapshot mismatch")
        items = planner_items({"turns": [{"attempts": [{"decision": decision.model_dump(mode="json")}]}]})
        self._save(snapshot.case_id, items)
        if guidance is not None:
            self.record_guidance(snapshot, guidance)

    @tenant_projection
    def record_retrieval(self, *, case_id, snapshot_id, telemetry: RetrievalTelemetry):
        telemetry = RetrievalTelemetry.model_validate(telemetry.model_dump())
        import math
        if not math.isfinite(telemetry.latency_ms) or telemetry.latency_ms < 0:
            raise ValueError("invalid retrieval latency")
        data = telemetry.model_dump()
        data["embedding_space"] = alias(data["embedding_space"], "SPACE") if data["embedding_space"] else "UNAVAILABLE"
        data["selected_experience_ids"] = ", ".join(alias(e, "EXPERIENCE") for e in telemetry.selected_experience_ids)
        data["selected_skill_ids"] = ", ".join(telemetry.selected_skill_ids)
        data["snapshot_id"] = snapshot_id
        self._save(case_id, [entry(TraceKind.KNOWLEDGE, digest(dict(case=case_id, snapshot=snapshot_id, telemetry=data)), "RECORDED", data, tuple(data),
            at=datetime.now(timezone.utc), historical=True, trust=TraceTrustClass.CURRENT_OPERATIONAL,
            warning="Retrieval telemetry; not current Evidence")], latency_seconds=telemetry.latency_ms/1000)

    @tenant_projection
    def record_turn(self, case_id, run_id, turn, session=None):
        items = planner_items({"run_id": run_id, "turns": [turn.model_dump(mode="json")]})
        self._save(case_id, items, session=session)

    def _save(self, case_id, items, session=None, *, latency_seconds=None):
        if session is None:
            with Session(self.cases.engine) as current, current.begin():
                self._save(case_id, items, current, latency_seconds=latency_seconds)
            return
        self.cases._row(session, case_id)
        from credit_harness.retrieval.indexer import insert_for
        for item in items:
            safe = TraceItem.model_validate(item.model_dump())
            # Case isolation is part of storage identity, even if two Cases
            # retrieve exactly the same organizational guidance.
            identity = digest(dict(tenant=self.cases.tenant_id, case=case_id, trace=safe.trace_id))
            inserted = session.execute(insert_for(self.cases.engine, InvestigationTraceRow).values(
                trace_id=identity, case_id=case_id, tenant_id=self.cases.tenant_id,
                projection_version='2', payload=safe.model_dump(mode="json")).on_conflict_do_nothing(index_elements=["trace_id"]).returning(InvestigationTraceRow.trace_id)).scalar_one_or_none()
            if inserted is not None and latency_seconds is not None:
                session.execute(insert_for(self.cases.engine, RetrievalMetricRow).values(
                    tenant_id=self.cases.tenant_id, latency_count=1, latency_sum_seconds=latency_seconds
                ).on_conflict_do_update(index_elements=["tenant_id"], set_={
                    "latency_count": RetrievalMetricRow.latency_count+1,
                    "latency_sum_seconds": RetrievalMetricRow.latency_sum_seconds+latency_seconds}))


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


class SQLSafePlannerAuditStore:
    """Durable allowlist audit, without retaining full proposals in process memory."""
    def __init__(self, traces):
        self.traces = traces

    def append(self, record):
        from credit_harness.planner.audit import PlannerAuditRecord
        record = PlannerAuditRecord.model_validate(record.model_dump())
        names = ('snapshot_id','planner_schema_version','policy_version','ranking_version',
            'model_input_schema_version','model_provider','model_name','input_hash','output_hash')
        data = {name:getattr(record,name) for name in names}
        candidate = record.selected_action.candidate if record.selected_action else None
        data['selected_action'] = candidate.action_type.value if candidate else 'NONE'
        data['rejection_codes'] = ', '.join(sorted({code.value for item in record.rejection_summary for code in item.reason_codes}))
        # Counts only; provider responses/hidden reasoning are never persisted.
        for name in ('input_tokens','output_tokens'):
            data[name] = getattr(record.usage_metadata,name,None)
        with alias_scope(self.traces.cases.tenant_id,self.traces.alias_key):
            self.traces._save(record.case_id,[entry(TraceKind.PLANNER,'audit:'+record.decision_id,
                'AUDITED',data,tuple(data),at=record.validated_at)])
