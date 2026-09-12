"""Investigation policies. Intentionally no benchmark labels/admin/fixture imports."""
from typing import Annotated
from pydantic import Field
from credit_harness.domain.models import Model
from credit_harness.cases.models import CaseStatus
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.planner.renderer import ModelInputRenderer
from credit_harness.tools.contracts import ToolQuery


class ReadStep(Model):
    tool: T
    protocol_version: str | None = None


class ChecklistDraft(Model):
    snapshot_id: str
    steps: Annotated[tuple[ReadStep, ...], Field(min_length=1, max_length=24)]


class InvestigationPorts:
    """Restricted services handed to baselines, never the synthetic fixture itself."""
    def __init__(self, case_id, cases, evidence, executor):
        self.case_id, self.cases, self.evidence, self.executor = case_id, cases, evidence, executor

    def snapshot(self):
        return ReasoningContextAssembler().build(self.cases.get(self.case_id), self.evidence.list(self.case_id))

    def read(self, tool, version=None):
        case = self.cases.get(self.case_id)
        if tool not in case.scope.allowed_tools:
            raise ValueError("tool outside investigation scope")
        if tool != T.PROTOCOL and version is not None:
            raise ValueError("unsupported query parameter")
        return self.executor.execute(self.case_id, tool, ToolQuery(internal_order_id=case.internal_order_id,
            protocol_version=version))


def values(snapshot, claim):
    return [f.value for f in snapshot.current_facts if f.claim_type == claim]


class FixedSOPInvestigator:
    """Conditional production-support SOP: prove money first, then investigate downstream.

    Unknown sources get at most two bounded reads. Mismatched identity stops
    before repair investigation. No read is selected using a private label.
    """
    def run(self, ports):
        ports.read(T.PAYMENT)
        if not values(ports.snapshot(), C.PAYMENT_FINALITY):
            ports.read(T.FUND)
            ports.read(T.PAYMENT)
        state = ports.snapshot()
        finality = values(state, C.PAYMENT_FINALITY)
        if not finality or "UNKNOWN" in finality:
            ports.cases.pause(ports.case_id, CaseStatus.WAITING)
            return "WAIT_UNKNOWN"
        ports.read(T.GUARANTEE)
        ports.read(T.FUND)
        state = ports.snapshot()
        if state.financial_identity.result.value == "MISMATCH":
            ports.cases.pause(ports.case_id, CaseStatus.ESCALATED)
            return "ESCALATE_IDENTITY_MISMATCH"
        if "NOT_EXECUTED" in finality:
            for tool in (T.ASSET, T.ACCOUNTING):
                ports.read(tool)
            return "NO_DISBURSEMENT_VERIFICATION_REQUIRED"
        if state.financial_identity.result.value != "MATCH":
            ports.cases.pause(ports.case_id, CaseStatus.ESCALATED)
            return "ESCALATE_IDENTITY_UNKNOWN"
        ports.read(T.CALLBACK)
        state = ports.snapshot()
        if True not in values(state, C.CALLBACK_GATEWAY_RECEIVED):
            ports.cases.pause(ports.case_id, CaseStatus.ESCALATED)
            return "ESCALATE_CALLBACK_ABSENT"
        ports.read(T.MESSAGES)
        if values(ports.snapshot(), C.MESSAGE_ERROR_CODE):
            versions = sorted({f.protocol_version for f in ports.snapshot().current_facts if f.protocol_version})
            for version in versions:
                ports.read(T.PROTOCOL, version)
        for tool in (T.ASSET, T.ASSET_DELIVERY, T.ACCOUNTING):
            ports.read(tool)
        return "INVESTIGATION_COMPLETE"


class ChecklistPlanner:
    """Exactly one planning call; changes after Tool results cannot alter this list."""
    def __init__(self, guidance_provider, model=None):
        self.guidance_provider, self.model = guidance_provider, model
        self.last_guidance = None
        self.last_guidance_result = None
        self.last_draft = None

    def plan(self, snapshot: ReasoningContextSnapshot):
        if type(snapshot) is not ReasoningContextSnapshot:
            raise TypeError("snapshot required")
        self.last_guidance_result = self.guidance_provider.build_result(snapshot) if self.guidance_provider else None
        self.last_guidance = self.last_guidance_result.bundle if self.last_guidance_result else None
        bundle = ModelInputRenderer().render(snapshot, self.last_guidance)
        if self.model:
            draft = ChecklistDraft.model_validate(self.model.plan_checklist(bundle))
        else:
            # Same current facts + retrieved guidance boundary as the live adapter.
            order = [T.PAYMENT, T.GUARANTEE, T.FUND, T.CALLBACK, T.MESSAGES, T.ASSET, T.ASSET_DELIVERY, T.ACCOUNTING]
            if bundle.historical_guidance and bundle.historical_guidance.verified_experiences:
                historical = bundle.historical_guidance.verified_experiences[0].observed_tool_sequence
                # Guidance is only an ordering hint; always retain current safety reads.
                order = [T.PAYMENT] + [t for t in historical if t in order and t != T.PAYMENT] + order
            allowed = {t.tool_name for t in bundle.trusted_control.available_tools}
            steps = [ReadStep(tool=t) for t in dict.fromkeys(order) if t in allowed]
            if T.PROTOCOL in allowed:
                versions = sorted({f.protocol_version for f in snapshot.current_facts if f.protocol_version})
                steps += [ReadStep(tool=T.PROTOCOL, protocol_version=v) for v in versions]
            draft = ChecklistDraft(snapshot_id=snapshot.snapshot_id, steps=tuple(steps))
        if draft.snapshot_id != snapshot.snapshot_id:
            raise ValueError("checklist snapshot mismatch")
        self.last_draft = draft
        return draft

    def run(self, ports):
        draft = self.plan(ports.snapshot())
        for step in draft.steps:
            # No replanning. Real Case executor still enforces lifecycle/scope/budget.
            ports.read(step.tool, step.protocol_version)
        return "CHECKLIST_COMPLETE"
