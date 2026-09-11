from dataclasses import dataclass

from credit_harness.cases.models import Case, CaseStatus
from credit_harness.context.assembler import ReasoningContextAssembler
from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.hypotheses.models import HypothesisGraphView, PriorityClass
from credit_harness.identity.models import IdentityMatch
from .models import RemediationEligibility, EligibilityStatus as S, RemediationActionType as A, RemediationUnavailable


@dataclass(frozen=True)
class InvestigationState:
    """Trusted deterministic evaluation state; never a model/API input."""
    case: Case
    index: EvidenceIndex
    graph: HypothesisGraphView
    snapshot: ReasoningContextSnapshot


class InvestigationStateReader:
    def __init__(self, cases, evidence, assembler=None):
        if cases.engine is not evidence.engine or cases.tenant_id != evidence.cases.tenant_id:
            raise ValueError("repositories must share case tenant and engine")
        self.cases, self.evidence = cases, evidence
        self.assembler = assembler or ReasoningContextAssembler()

    def read(self, case_id):
        for _ in range(3):
            case = self.cases.get(case_id)
            evidence = self.evidence.list(case_id)
            if self.cases.get(case_id) == case:
                index = EvidenceIndex(case, evidence)
                return InvestigationState(case, index, HypothesisEngine().evaluate(case, evidence),
                                          self.assembler.build(case, evidence))
        raise RemediationUnavailable("case changed during consistent evidence read")


class RemediationEligibilityEvaluator:
    def evaluate(self, state):
        admin = (A.NO_REMEDIATION, A.REQUEST_OPERATOR_REVIEW, A.CREATE_RECONCILIATION_TASK)
        if state.case.status.is_terminal:
            return RemediationEligibility(status=S.NO_REMEDIATION_NEEDED, can_plan=False,
                                          allowed_actions=(A.NO_REMEDIATION,))
        if not state.index.evidence:
            return RemediationEligibility(status=S.INVESTIGATION_INCOMPLETE, can_plan=False, allowed_actions=admin)
        if state.graph.payment_identity.result != IdentityMatch.MATCH:
            return RemediationEligibility(status=S.IDENTITY_UNSAFE, can_plan=True, allowed_actions=admin)
        if any(g.priority_class == PriorityClass.SAFETY_CRITICAL for g in state.graph.open_gaps):
            return RemediationEligibility(status=S.SAFETY_GAP_OPEN, can_plan=True, allowed_actions=admin)
        if state.case.status not in (CaseStatus.INVESTIGATING, CaseStatus.ESCALATED):
            return RemediationEligibility(status=S.INVESTIGATION_INCOMPLETE, can_plan=True, allowed_actions=admin)
        return RemediationEligibility(status=S.ELIGIBLE, can_plan=True, allowed_actions=tuple(A))
