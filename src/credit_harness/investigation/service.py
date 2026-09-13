"""One database read snapshot, followed by a fresh watermark check.

Pages are slices of a short-lived immutable safe frame, never fresh independent
queries. No lock or database transaction survives the request.
"""
from collections import OrderedDict
from datetime import datetime, timezone
from hashlib import sha256
from hmac import digest as mac, compare_digest
from secrets import token_bytes
from threading import RLock
from time import monotonic, time
from .privacy import alias_scope

from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.api.ui import UICase, UIEvidence, UIHypothesisGraph, UIHypothesisState, UIGap
from credit_harness.cases.repository import hydrate
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.budget import digest
from credit_harness.context.compaction import fact
from credit_harness.context.eligibility import ContextEligibilityError
from credit_harness.context.identity_projection import IdentityContextProjector
from credit_harness.evidence.models import ClaimType as C
from credit_harness.evidence.verification import read_verified_evidence
from credit_harness.evidence.tables import EvidenceOriginRow
from credit_harness.cases.tables import CaseCallRow
from credit_harness.persistence.store import ObservationRow
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.evaluation.snapshot import table_exists, row_data
from credit_harness.evaluation.tables import EvaluationReportRow, CaseClosureRow
from credit_harness.authorization.tables import EffectRow, ApprovalRow, AuthorizedIntentRow
from credit_harness.orchestration.tables import WorkItemRow, ResumedRunRow
from credit_harness.recovery.tables import (EffectRecoveryStateRow, AgentCheckpointRow,
    EffectRecoveryAttemptRow, ReadDispatchRecoveryRow)
from credit_harness.registry.tables import (RegistryHeadRow, RegistryVersionRow, RouteHeadRow,
    RouteRevisionRow, RouteContextRow, DispatchSourceRow)
from .models import (InvestigationFrame, EvidencePage, TracePage, FrameStale,
    FinancialTruth, GapCapability, TraceKind as K, TraceItem)
from .projection import entry, alias, planner_items, display, fields
from .trace_store import InvestigationTraceRow

SECTIONS = {"timeline": "timeline", "planner-runs": "planner_trace", "tools": "tool_trace",
    "sources": "registry_source_trace", "routes": "route_trace", "knowledge": "knowledge_retrieval_trace",
    "work": "work_trace", "effects": "side_effect_trace", "recovery": "recovery_trace",
    "evaluations": "evaluation_trace", "closure": "closure_trace"}
TRUTH = (C.PAYMENT_FINALITY, C.FUND_BUSINESS_STATUS, C.GUARANTEE_STATUS, C.ASSET_STATUS,
    C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS, C.ACCOUNTING_ENTRY_PRESENT)
REF_CLAIMS = {C.PAYMENT_TRANSACTION_ID, C.TRANSACTION_FUND_REQUEST_ID, C.LOAN_NOTE_REFERENCE}


class InvestigationFrameService:
    def __init__(self, repository, *, assembler=None, after_read=None, ttl=300, max_frames=32, cache=None, cursor_key=None, previous_cursor_key=None, alias_key=None):
        self.repository = repository
        self.cache, self.alias_key = cache, alias_key
        self.previous_cursor_key = previous_cursor_key
        if any(key is not None and len(key) < 32 for key in (cursor_key, previous_cursor_key, alias_key)):
            raise ValueError("frame keys require at least 32 bytes")
        self.assembler = assembler or ReasoningContextAssembler()
        self.after_read = after_read  # deterministic race testing, trusted constructor only
        self.ttl, self.max_frames = ttl, max_frames
        self._cache, self._lock, self._secret = OrderedDict(), RLock(), cursor_key or token_bytes(32)

    def _read(self, case_id):
        engine = self.repository.engine
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                if engine.dialect.name == "sqlite":
                    # sqlite's legacy transaction control doesn't BEGIN for SELECT.
                    connection.exec_driver_sql("BEGIN")
                with Session(bind=connection) as session:
                    row = self.repository.cases._row(session, case_id)
                    case = hydrate(row)
                    verified = read_verified_evidence(session, row)
                    if not verified.provenance_valid:
                        raise ContextEligibilityError("invalid evidence provenance")
                    data = {}
                    data["origins"] = [(r.call_id, r.evidence_id) for r in session.scalars(
                        select(EvidenceOriginRow).join(CaseCallRow).where(CaseCallRow.case_id == case_id)
                        .order_by(EvidenceOriginRow.call_id, EvidenceOriginRow.evidence_id))]
                    data["observations"] = {}
                    for call in verified.calls:
                        observation = session.get(ObservationRow, call["observation_id"]) if call["observation_id"] else None
                        if observation:
                            data["observations"][observation.id] = {key: observation.observation[key]
                                for key in ("status", "observed_at")}
                    tables = (EvaluationReportRow, CaseClosureRow, EffectRow, WorkItemRow,
                        EffectRecoveryStateRow, AgentCheckpointRow, RouteRevisionRow, DispatchSourceRow,
                        AuthorizedIntentRow, InvestigationTraceRow, ReadDispatchRecoveryRow)
                    for table in tables:
                        data[table.__tablename__] = [row_data(r) for r in session.scalars(
                            select(table).where(table.case_id == case_id).order_by(*table.__table__.primary_key.columns))] if table_exists(session, table) else []
                    work_ids = [r["work_item_id"] for r in data[WorkItemRow.__tablename__]]
                    effect_ids = [r["effect_id"] for r in data[EffectRow.__tablename__]]
                    data["recovery_attempts"] = [row_data(r) for r in session.scalars(select(EffectRecoveryAttemptRow)
                        .where(EffectRecoveryAttemptRow.effect_id.in_(effect_ids)).order_by(EffectRecoveryAttemptRow.recovery_id))] if table_exists(session, EffectRecoveryAttemptRow) else []
                    data["runs"] = [r.payload for r in session.scalars(select(ResumedRunRow).where(
                        ResumedRunRow.work_item_id.in_(work_ids)))] if table_exists(session, ResumedRunRow) else []
                    intent_ids = [r["intent_id"] for r in data[AuthorizedIntentRow.__tablename__]]
                    data["approvals"] = [row_data(r) for r in session.scalars(select(ApprovalRow).where(
                        ApprovalRow.intent_id.in_(intent_ids)))] if table_exists(session, ApprovalRow) else []
                    head = session.get(RegistryHeadRow, case.tenant_id) if table_exists(session, RegistryHeadRow) else None
                    version = session.get(RegistryVersionRow, head.version) if head and head.version else None
                    data["registry"] = version.payload if version else None
                    data["registry_version"] = head.version if head else None
                    route_head = session.get(RouteHeadRow, case_id) if table_exists(session, RouteHeadRow) else None
                    route = session.get(RouteContextRow, case_id) if table_exists(session, RouteContextRow) else None
                    data["route_revision"] = route_head.route_revision_id if route_head else None
                    data["route"] = route.payload if route else None
                    if route_head:
                        revision = session.get(RouteRevisionRow, route_head.route_revision_id)
                        data["route"] = revision.payload["route_context"]
                    # Resolve each actual dispatch against its original version.
                    data["source_versions"] = {}
                    for source in data[DispatchSourceRow.__tablename__]:
                        v = source["payload"]["resolved"]["registry_version"]
                        old = session.get(RegistryVersionRow, v)
                        if old and old.tenant_id == case.tenant_id:
                            data["source_versions"][v] = old.payload
                    watermark = digest(dict(case=case.model_dump(mode="json"), evidence=verified.fingerprint,
                        calls=verified.calls, traces=data))
                    return case, verified, data, watermark

    def build(self, case_id):
        for _ in range(3):
            case, read, data, watermark = self._read(case_id)
            if self.after_read:
                self.after_read()
            with alias_scope(case.tenant_id, self.alias_key):
                frame, pages = self._project(case, read, data, watermark)
            if self._read(case_id)[3] != watermark:
                continue
            if self.cache is not None:
                return self.cache.put(case.tenant_id, frame, pages, self.ttl)[0]
            with self._lock:
                self._prune()
                if frame.frame_id in self._cache:
                    return self._cache[frame.frame_id][2]
                self._cache[frame.frame_id] = (monotonic(), case_id, frame, pages)
                while len(self._cache) > self.max_frames:
                    self._cache.popitem(last=False)
            return frame
        raise FrameStale("FRAME_STALE")

    def _prune(self):
        for key, value in list(self._cache.items()):
            if monotonic() - value[0] > self.ttl:
                del self._cache[key]

    def _cursor(self, frame_id, section, offset, key=None):
        body = f"{frame_id}:{section}:{offset}"
        return str(offset) + "." + mac(key or self._secret, body.encode(), "sha256").hex()

    def _page(self, frame_id, section, items, offset=0, limit=40, denied=0):
        end = min(offset + limit, len(items))
        cursor = self._cursor(frame_id, section, end) if end < len(items) else None
        if section == "evidence":
            return EvidencePage(frame_id=frame_id, items=tuple(items[offset:end]), total=len(items),
                eligibility_denied_count=denied, next_cursor=cursor)
        return TracePage(frame_id=frame_id, items=tuple(items[offset:end]), total=len(items), next_cursor=cursor)

    def page(self, case_id, frame_id, section, cursor=None, limit=40):
        if section not in (*SECTIONS, "evidence") or not 1 <= limit <= 100:
            raise FrameStale("INVALID_CURSOR")
        self.repository.cases.get(case_id)  # reauthorize even for a cached page
        with self._lock:
            self._prune()
            cached = self._cached(case_id, frame_id)
            if not cached or cached[1] != case_id:
                raise FrameStale("FRAME_EXPIRED")
            offset = 0
            if cursor:
                try:
                    offset = int(cursor.split(".")[0])
                    if offset < 0 or not any(compare_digest(cursor, self._cursor(frame_id, section, offset, key)) for key in (self._secret, self.previous_cursor_key) if key):
                        raise ValueError()
                except ValueError:
                    raise FrameStale("INVALID_CURSOR") from None
            return self._page(frame_id, section, cached[3][section], offset, limit,
                denied=cached[2].evidence.eligibility_denied_count)

    def _cached(self, case_id, frame_id):
        if self.cache is None:
            return self._cache.get(frame_id)
        frame, pages = self.cache.get(self.repository.cases.tenant_id, case_id, frame_id)
        return (0, case_id, frame, pages)

    def evidence_detail(self, case_id, frame_id, evidence_id):
        self.repository.cases.get(case_id)
        with self._lock:
            self._prune()
            cached = self._cached(case_id, frame_id)
            if not cached or cached[1] != case_id:
                raise FrameStale("FRAME_EXPIRED")
            item = next((e for e in cached[3]["evidence"] if e.evidence_id == evidence_id), None)
            if item is None:
                from credit_harness.cases.models import CaseAccessError
                raise CaseAccessError("evidence unavailable")
            return item

    def _project(self, case, read, data, watermark):
        policy = self.assembler.eligibility
        policy.validate_case(case)
        index = EvidenceIndex(case, read.evidence)
        graph = HypothesisEngine().evaluate(case, read.evidence)
        eligible = {e.evidence_id: e for e in read.evidence if policy.allows(e)}
        required = {r for h in graph.hypotheses for r in (*h.supporting_evidence_refs,
            *h.contradicting_evidence_refs, *h.decisive_evidence_refs)} | set(graph.payment_identity.evidence_refs)
        if not required <= eligible.keys():
            raise ContextEligibilityError("graph references ineligible evidence")
        def safe_fact(e):
            item = fact(e)
            subject = item.subject.model_copy(update={"identifier": alias(item.subject.identifier, "SUBJECT"),
                "internal_order_id": alias(case.internal_order_id, "ORDER")})
            token_prefix = {C.PAYMENT_CUSTOMER_REF:"CUS", C.PAYMENT_BENEFICIARY_REF:"BEN", C.PAYMENT_ACCOUNT_REF:"ACC"}.get(e.claim_type)
            value = (alias(item.value, token_prefix) if token_prefix else alias(item.value, "EXTERNAL")
                if e.claim_type in REF_CLAIMS else display(item.value) if isinstance(item.value, str) else item.value)
            return item.model_copy(update={"subject": subject, "value": value})
        current = tuple(safe_fact(e) for c in C if c != C.SOURCE_LOOKUP_STATUS for e in index.current(c) if e.evidence_id in eligible)
        evidence = tuple(UIEvidence(**safe_fact(e).model_dump(), evidence_id=e.evidence_id,
            observation_id=e.observation_id, event_time=e.event_time, strength=e.strength,
            extractor_version=e.metadata.extractor_version) for e in sorted(eligible.values(), key=lambda e: (e.observed_at, e.evidence_id), reverse=True))
        identity = IdentityContextProjector().project(graph.payment_identity, index)
        identity = identity.model_copy(update={"transaction_ref_preview": tuple(alias(r, "TRANSACTION") for r in identity.transaction_ref_preview)})
        truths = []
        for c in TRUTH:
            facts = [f for f in current if f.claim_type == c]
            values = {(type(f.value).__name__, f.value) for f in facts}
            truths.append(FinancialTruth(claim_type=c, value=facts[0].value if len(values) == 1 else "UNKNOWN",
                evidence_refs=tuple(r for f in facts for r in f.evidence_refs),
                status="OBSERVED" if len(values) == 1 else "CONFLICT" if values else "UNKNOWN"))
        pages = {section: [] for section in SECTIONS}
        pages["evidence"] = evidence
        def add(section, item):
            pages[section].append(item)
            if section != "timeline": pages["timeline"].append(item)
        add("timeline", entry(K.CASE_CREATED, case.case_id, "CREATED", {}, at=case.created_at))
        by_observation = {}
        for e in evidence:
            by_observation.setdefault(e.observation_id, []).append(e)
            add("timeline", entry(K.EVIDENCE, e.evidence_id, "PUBLISHED", {"claim_type": e.claim_type},
                ("claim_type",), at=e.observed_at, refs=(e.evidence_id,)))
        visible_by_id = {e.evidence_id: e for e in evidence}
        for call in read.calls:
            produced = [visible_by_id[ref] for origin, ref in data["origins"]
                if origin == call["call_id"] and ref in visible_by_id]
            receipt = data["observations"].get(call["observation_id"], {})
            values = {"tool": call["tool"], "sequence": call["sequence"],
                "observation_status": receipt.get("status", "UNAVAILABLE"),
                "observed_at": receipt.get("observed_at"),
                "claim_types": ", ".join(sorted({e.claim_type.value for e in produced}))}
            add("tools", entry(K.TOOL, call["call_id"], call["state"], values, tuple(values),
                at=None, refs=tuple(e.evidence_id for e in produced),
                related=(alias(call["observation_id"], "OBSERVATION"),) if call["observation_id"] else (),
                warning="Dispatch timestamp was not recorded"))
            if receipt:
                add("timeline", entry(K.OBSERVATION, call["observation_id"], receipt["status"],
                    {"tool":call["tool"]}, ("tool",), at=receipt["observed_at"], refs=tuple(e.evidence_id for e in produced),
                    related=(alias(call["call_id"], K.TOOL.value.upper()),)))
        for raw in data[DispatchSourceRow.__tablename__]:
            r = raw["payload"]["resolved"]
            if raw["tenant_id"] != case.tenant_id or r["case_id"] != case.case_id:
                raise ContextEligibilityError("source trace scope mismatch")
            version = data["source_versions"].get(r["registry_version"], {})
            system = next((s for s in version.get("systems", ()) if s["system_id"] == r["system_id"]), {})
            values = {k: system.get(k) for k in ("company_system_code", "display_name", "source_channel")}
            values.update({k: r[k] for k in ("system_id", "capability_id", "registry_version", "tool_name")})
            values["authority"] = ", ".join(a["claim_type"] + ":" + a["authority_level"] for a in r["authority"])
            refs = next((t.evidence_refs for t in pages["tools"] if t.trace_id == alias(raw["call_id"], K.TOOL.value.upper())), ())
            add("sources", entry(K.SOURCE, raw["call_id"], "ROUTED", values, tuple(values), refs=refs,
                related=(alias(raw["call_id"], K.TOOL.value.upper()),)))
        for r in data[RouteRevisionRow.__tablename__]:
            p = r["payload"]
            values = {"route_revision": r["route_revision_id"], "parent_revision": p["parent_revision_id"],
                "protocol_version": p["route_context"]["protocol_version"], "change_reason": p["change_reason"]}
            add("routes", entry(K.ROUTE, r["route_revision_id"], p["status"], values, tuple(values),
                at=p["created_at"], refs=tuple(p["supporting_evidence_refs"])))
        # Registry-aware capability descriptions computed entirely from this read.
        tools = self.assembler.catalog.for_case(case) if not hasattr(self.assembler.catalog, "project") else ()
        if data["registry"]:
            from credit_harness.registry.models import RegistryDefinition, CaseRouteContext, AccessMode
            from credit_harness.registry.routing import eligible as route_eligible
            from credit_harness.context.tool_capabilities import ToolCapabilityCatalog
            definition = RegistryDefinition.model_validate(data["registry"])
            route = CaseRouteContext.model_validate(data["route"]) if data["route"] else None
            systems = {s.system_id: s for s in definition.systems}
            candidates = [c for c in definition.capabilities if route and c.read_or_write == AccessMode.READ
                and c.supports_lookup and route_eligible(c, case, route) and route_eligible(systems[c.system_id], case, route)]
            bound = {c.tool_name: c for c in candidates if sum(x.tool_name == c.tool_name for x in candidates) == 1}
            tools = tuple(t.model_copy(update={"produces_claim_types": bound[t.tool_name].produces_claim_types,
                "contributes_requirements": bound[t.tool_name].contributes_requirements})
                for t in ToolCapabilityCatalog().for_case(case) if t.tool_name in bound)
        gap_caps = tuple(GapCapability(gap_id=g.gap_id, available_tools=tuple(t.tool_name for t in tools
            if set(g.required_claim_types) & (set(t.produces_claim_types) | set(t.contributes_requirements))),
            unavailable_reason=None if any(set(g.required_claim_types) & (set(t.produces_claim_types) | set(t.contributes_requirements)) for t in tools)
            else "NO_AVAILABLE_CAPABILITY_FOR_REQUIRED_FACT") for g in graph.open_gaps)
        for run in data["runs"]:
            if run.get("case_id") != case.case_id: continue
            for item in planner_items(run):
                add("knowledge" if item.kind == K.KNOWLEDGE else "planner-runs", item)
        for r in data[InvestigationTraceRow.__tablename__]:
            if r["tenant_id"] == case.tenant_id:
                item = TraceItem.model_validate(r["payload"])
                add("knowledge" if item.kind == K.KNOWLEDGE else "planner-runs", item)
        for r in data[WorkItemRow.__tablename__]:
            p = r["payload"]
            add("work", entry(K.WORK, r["work_item_id"], r["status"], p,
                ("work_type", "reason_code", "trigger", "not_before", "attempt_count", "verification_requirements"), at=p["created_at"]))
        for r in data[AuthorizedIntentRow.__tablename__]:
            p = r["payload"]
            add("timeline", entry(K.REMEDIATION, r["intent_id"], "PROPOSED", p, ("action_type", "risk_level"),
                at=p.get("created_at"), warning="Intent ≠ Authorization"))
        for r in data["approvals"]:
            add("timeline", entry(K.APPROVAL, r["approval_id"], r["status"], {}, at=r["payload"].get("created_at")))
        for r in data[EffectRow.__tablename__]:
            p = r["payload"]
            approval = next((a["status"] for a in data["approvals"] if a["approval_id"] == p.get("approval_id")), "NOT_RECORDED")
            add("effects", entry(K.EFFECT, r["effect_id"], r["status"], {**p, "approval_state": approval},
                ("action_type", "approval_state"), at=p.get("updated_at"),
                warning="结果未知，不允许盲目重试" if r["status"] == "UNKNOWN" else "APPLIED ≠ VERIFIED"))
        for r in data[EffectRecoveryStateRow.__tablename__]:
            add("recovery", entry(K.RECOVERY, r["effect_id"], r["status"], r,
                ("attempt_count", "next_eligible_at", "requires_escalation"),
                at=datetime.fromtimestamp(r["ledger_updated_at"], timezone.utc),
                warning="结果未知，不允许盲目重试" if r["status"] == "UNKNOWN" else None))
        for r in data["recovery_attempts"]:
            add("recovery", entry(K.RECOVERY, r["recovery_id"], r["result_status"] or "IN_PROGRESS", r,
                ("sequence", "source_status", "result_status", "failure_code"),
                at=datetime.fromtimestamp(r["started_at"], timezone.utc), related=(alias(r["effect_id"], "EFFECT"),)))
        for r in data[ReadDispatchRecoveryRow.__tablename__]:
            add("recovery", entry(K.RECOVERY, r["call_id"], r["status"], {},
                at=datetime.fromtimestamp(r["attempted_at"], timezone.utc), refs=tuple(r["recovered_evidence_refs"]),
                related=(alias(r["call_id"], K.TOOL.value.upper()),)))
        from .closure import checked_closure
        from .models import TraceTrustClass as Trust
        closure_reports = set()
        for closure_row in data[CaseClosureRow.__tablename__]:
            report_row = next((r for r in data[EvaluationReportRow.__tablename__]
                if r["evaluation_run_id"] == closure_row["evaluation_run_id"]), None)
            if report_row is None:
                raise ContextEligibilityError("closure report missing")
            record, closing_report = checked_closure(case, closure_row, report_row, read.fingerprint,
                digest(data[EffectRow.__tablename__]), digest(read.calls))
            closure_reports.add(closing_report.report_id)
        for r in data[EvaluationReportRow.__tablename__]:
            p = r["payload"]
            from credit_harness.evaluation.models import EvaluationReport
            from credit_harness.evaluation.evaluator import report_identity
            report = EvaluationReport.model_validate(p)
            if (report_identity(report) != r["report_id"] or report.case_id != case.case_id
                    or report.snapshot.tenant_id != case.tenant_id):
                raise ContextEligibilityError("evaluation binding invalid")
            is_current = report.report_id in closure_reports or (p["snapshot"]["evidence_fingerprint"] == read.fingerprint
                and datetime.fromisoformat(p["snapshot"]["case_revision"]) == case.updated_at
                and p["snapshot"]["side_effect_ledger_fingerprint"] == digest(data[EffectRow.__tablename__])
                and p["snapshot"]["call_history_fingerprint"] == digest(read.calls)
                and p["snapshot"]["recovery_fingerprint"] == digest(data[EffectRecoveryStateRow.__tablename__]
                    + data["recovery_attempts"] + data[ReadDispatchRecoveryRow.__tablename__]))
            for dim in p["dimensions"]:
                values = {**dim, "report_ref": alias(p["report_id"], "REPORT"), "overall_verdict": p["overall_verdict"],
                    "requirements": ", ".join(u["requirement"] for u in p["unresolved_requirements"] if u["dimension"] == dim["dimension"])}
                add("evaluations", entry(K.EVALUATION, r["evaluation_run_id"] + dim["dimension"], dim["status"], values,
                    ("dimension", "report_ref", "overall_verdict", "reason_codes", "required_claims_missing", "requirements"),
                    at=p["created_at"], refs=tuple(dim["supporting_evidence_refs"]), historical=not is_current,
                    trust=Trust.CLOSURE_BOUND_CURRENT_EVALUATION if report.report_id in closure_reports else None,
                    warning=None if is_current else "Historical evaluation; current state requires re-verification"))
        closed = False
        for r in data[CaseClosureRow.__tablename__]:
            p = r["payload"]
            report = next((e["payload"] for e in data[EvaluationReportRow.__tablename__] if e["evaluation_run_id"] == r["evaluation_run_id"]), None)
            if (case.status.value == "CLOSED_VERIFIED" and report and report["overall_verdict"] == "PASS"
                    and report["report_id"] in closure_reports):
                closed = True
                add("closure", entry(K.CLOSURE, r["closure_id"], "CLOSED_VERIFIED", {"outcome_path": report["outcome_path"]},
                    ("outcome_path",), at=p["closed_at"], related=(alias(report["report_id"], "REPORT"),),
                    warning="IndependentEvaluator PASS + VerifiedClosure CAS"))
        summary = UICase(**{key: getattr(case, key) for key in UICase.model_fields})
        summary = summary.model_copy(update={"internal_order_id": alias(case.internal_order_id, "ORDER")})
        if summary.financial_subject:
            subject = summary.financial_subject
            summary = summary.model_copy(update={"financial_subject": subject.model_copy(update={
                "customer_ref": alias(subject.customer_ref, "CUS"),
                "expected_beneficiary_ref": alias(subject.expected_beneficiary_ref, "BEN"),
                "expected_account_ref": alias(subject.expected_account_ref, "ACC")})})
        if case.status.value == "CLOSED_VERIFIED" and not closed:
            raise ContextEligibilityError("verified closure binding missing")
        epoch = str(int(time() // self.ttl)) if self.cache is not None else "local"
        frame_id = digest(dict(tenant=case.tenant_id, case=case.case_id, watermark=watermark, epoch=epoch,
            projection="17.1", alias_domain=alias("frame-domain")))
        for section in SECTIONS:
            pages[section] = tuple(sorted({i.trace_id: i for i in pages[section]}.values(),
                key=lambda i: (i.occurred_at or datetime.min.replace(tzinfo=timezone.utc), i.trace_id), reverse=True))
        runs = data[AgentCheckpointRow.__tablename__]
        latest = max(runs, key=lambda r: r["updated_at"])["run_id"] if runs else None
        frame = InvestigationFrame(frame_id=frame_id, case_id=case.case_id, case_revision=case.updated_at,
            evidence_fingerprint=read.fingerprint, registry_version=data["registry_version"], route_revision=data["route_revision"],
            latest_agent_run=alias(latest, "RUN") if latest else None, assembled_at=datetime.now(timezone.utc),
            route_summary=fields(data["route"] or {}, ("funding_partner", "asset_partner", "product_code", "protocol_version")),
            investigation_allowed=case.status.value in ("NEW", "INVESTIGATING") and case.budget.used_tool_calls < case.budget.max_tool_calls,
            current_evidence_total=len(current),
            financial_identity_dimensions=fields({d: "MISMATCH" if d in identity.mismatch_dimensions
                else "UNKNOWN" if d in identity.unknown_dimensions or not graph.payment_identity.witnesses else "MATCH"
                for d in ("AMOUNT", "CURRENCY", "REQUEST", "CUSTOMER", "BENEFICIARY", "ACCOUNT")},
                ("AMOUNT", "CURRENCY", "REQUEST", "CUSTOMER", "BENEFICIARY", "ACCOUNT")),
            case_summary=summary, financial_truth=tuple(truths), financial_identity=identity, current_evidence=current[:100],
            hypotheses=UIHypothesisGraph(case_id=case.case_id, rule_version=graph.rule_version, definitions=graph.definitions,
                hypotheses=tuple(UIHypothesisState(**h.model_dump()) for h in graph.hypotheses), open_gaps=tuple(UIGap(**g.model_dump()) for g in graph.open_gaps)),
            gap_capabilities=gap_caps, evidence=self._page(frame_id, "evidence", evidence, denied=len(read.evidence)-len(evidence)),
            **{name: self._page(frame_id, section, pages[section], limit=10 if section == "timeline" else 40)
                for section, name in SECTIONS.items()})
        return frame, pages
