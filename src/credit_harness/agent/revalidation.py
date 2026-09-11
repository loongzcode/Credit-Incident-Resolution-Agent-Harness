from pydantic import ValidationError

from credit_harness.cases.models import Case, CaseStatus, CasePolicyError
from credit_harness.cases.preconditions import AgentExecutionPrecondition
from credit_harness.context.budget import case_payload, digest
from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.planner import models as planner_models
from credit_harness.planner.models import PlannerDecision, CallToolCandidate
from credit_harness.planner.validator import CandidateValidator
from credit_harness.planner.policy import HardPolicyFilter
from .models import RuntimeRevalidationResult, RevalidationStatus as R
from .query import build_tool_query


def execution_precondition(case, snapshot):
    return AgentExecutionPrecondition(case_id=case.case_id, tenant_id=case.tenant_id,
        expected_case_status=case.status, expected_used_tool_calls=case.budget.used_tool_calls,
        expected_case_updated_at=case.updated_at, expected_snapshot_id=snapshot.snapshot_id,
        planner_policy_version=planner_models.PLANNER_POLICY_VERSION)


class RuntimeActionRevalidator:
    """Pure verification; no repository, dispatch, status propagation or ranking."""

    def validate(self, fresh_case: Case, fresh_snapshot: ReasoningContextSnapshot,
                 decision: PlannerDecision) -> RuntimeRevalidationResult:
        try:
            decision = PlannerDecision.model_validate(decision.model_dump())
        except (ValidationError, AttributeError):
            return RuntimeRevalidationResult(status=R.CANDIDATE_REJECTED)
        selected = decision.selected_action
        if selected is None:
            return RuntimeRevalidationResult(status=R.NO_SELECTED_ACTION)
        if decision.snapshot_id != selected.snapshot_id or selected.snapshot_id != fresh_snapshot.snapshot_id:
            return RuntimeRevalidationResult(status=R.STALE_SNAPSHOT)
        if (decision.policy_version != planner_models.PLANNER_POLICY_VERSION
                or selected.policy_version != planner_models.PLANNER_POLICY_VERSION):
            return RuntimeRevalidationResult(status=R.STALE_POLICY)
        if (fresh_case.case_id != fresh_snapshot.case_id
                or digest(case_payload(fresh_case)) != fresh_snapshot.case_fingerprint
                or fresh_case.status not in (CaseStatus.NEW, CaseStatus.INVESTIGATING)):
            return RuntimeRevalidationResult(status=R.CASE_PRECONDITION_FAILED)
        # valid_candidates is an audit artifact, never an authorization cache.
        candidate = selected.candidate
        reasons = tuple(dict.fromkeys((*CandidateValidator().validate(fresh_snapshot, candidate),
                                      *HardPolicyFilter().filter(fresh_snapshot, candidate))))
        if reasons:
            return RuntimeRevalidationResult(status=R.CANDIDATE_REJECTED, rejection_codes=reasons)
        if isinstance(candidate, CallToolCandidate):
            try:
                build_tool_query(fresh_case, candidate)
            except (ValueError, CasePolicyError):
                return RuntimeRevalidationResult(status=R.QUERY_REBUILD_FAILED)
        return RuntimeRevalidationResult(status=R.VALID)
