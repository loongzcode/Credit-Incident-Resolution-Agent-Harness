from enum import StrEnum
from typing import Literal
from pydantic import AwareDatetime
from credit_harness.domain.models import Model
from credit_harness.context.models import FactCapsule, PaymentIdentityContext
from credit_harness.domain.enums import ToolName
from credit_harness.evidence.models import ClaimType
from credit_harness.api.ui import UICase, UIEvidenceList, UIHypothesisGraph


class InvestigationPermission(StrEnum):
    CASE_VIEW = "CASE_VIEW"
    CASE_TRACE_VIEW = "CASE_TRACE_VIEW"
    CASE_FINANCIAL_VIEW = "CASE_FINANCIAL_VIEW"


class TraceKind(StrEnum):
    CASE_CREATED = "CaseCreated"
    CONTEXT = "ContextSnapshot"
    PLANNER = "PlannerDecision"
    TOOL = "CALL_TOOL"
    OBSERVATION = "Observation"
    EVIDENCE = "EvidencePublished"
    WORK = "ResumeWork"
    REMEDIATION = "RemediationProposed"
    APPROVAL = "Approval"
    EFFECT = "Effect"
    RECOVERY = "Recovery"
    EVALUATION = "Evaluation"
    CLOSURE = "Closed"
    ROUTE = "RouteRevision"
    SOURCE = "RegistrySource"
    KNOWLEDGE = "KnowledgeRetrieval"
    WAIT = "WAIT"
    ESCALATE = "ESCALATE"


class TraceField(Model):
    name: str
    value: str


class TraceItem(Model):
    trace_id: str
    kind: TraceKind
    occurred_at: AwareDatetime | None = None
    status: str
    fields: tuple[TraceField, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    related_refs: tuple[str, ...] = ()
    warning: str | None = None
    historical: bool = False


class TracePage(Model):
    frame_id: str
    items: tuple[TraceItem, ...]
    total: int
    next_cursor: str | None = None


class EvidencePage(UIEvidenceList):
    frame_id: str
    next_cursor: str | None = None


class FinancialTruth(Model):
    claim_type: ClaimType
    value: str | int | bool
    evidence_refs: tuple[str, ...]
    status: Literal["OBSERVED", "UNKNOWN", "CONFLICT"]


class GapCapability(Model):
    gap_id: str
    available_tools: tuple[ToolName, ...]
    unavailable_reason: str | None = None


class InvestigationFrame(Model):
    frame_id: str
    case_id: str
    case_revision: AwareDatetime
    evidence_fingerprint: str
    registry_version: str | None
    route_revision: str | None
    latest_agent_run: str | None
    assembled_at: AwareDatetime
    route_summary: tuple[TraceField, ...] = ()
    investigation_allowed: bool
    current_evidence_total: int
    case_summary: UICase
    financial_truth: tuple[FinancialTruth, ...]
    financial_identity: PaymentIdentityContext
    financial_identity_dimensions: tuple[TraceField, ...]
    current_evidence: tuple[FactCapsule, ...]
    hypotheses: UIHypothesisGraph
    gap_capabilities: tuple[GapCapability, ...]
    evidence: EvidencePage
    timeline: TracePage
    planner_trace: TracePage
    tool_trace: TracePage
    registry_source_trace: TracePage
    route_trace: TracePage
    knowledge_retrieval_trace: TracePage
    work_trace: TracePage
    side_effect_trace: TracePage
    recovery_trace: TracePage
    evaluation_trace: TracePage
    closure_trace: TracePage
    notice: str = "READ ONLY · APPLIED ≠ VERIFIED · Historical guidance is not current Evidence"


class FrameStale(RuntimeError):
    pass
