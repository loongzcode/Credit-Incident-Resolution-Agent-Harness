import pytest

from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.models import GapStatus, UncollectedClaimType as U
from credit_harness.planner.models import ReasonCode, RejectionCode as R, WaitCandidate
from credit_harness.planner.validator import CandidateValidator
from tests.test_planner import inputs, snapshot, assemble, call, decide, escalation, gap, reseal


def asset_call(snapshot, **changes):
    return call(snapshot, T.ASSET, "ASSET_CONVERGENCE", (C.ASSET_STATUS,), **changes)


def test_model_cannot_starve_actionable_safety_gap_by_omission(snapshot):
    result = decide(snapshot, asset_call(snapshot))
    assert result.selected_action is None
    assert result.valid_candidates == ()
    assert result.ranking_details == ()
    assert result.rejected_candidates[0].reason_codes == (R.ACTIONABLE_SAFETY_GAP_NOT_ADDRESSED,)


def test_low_priority_call_not_selected_when_safety_gap_is_actionable(snapshot):
    result = decide(snapshot, asset_call(snapshot, candidate_id="low"), call(snapshot, candidate_id="safety"))
    assert result.selected_action.candidate.tool_name == T.PAYMENT
    assert [p.candidate.candidate_id for p in result.valid_candidates] == ["safety"]
    assert result.rejected_candidates[0].reason_codes == (R.ACTIONABLE_SAFETY_GAP_NOT_ADDRESSED,)


def test_harness_does_not_synthesize_missing_payment_candidate(snapshot):
    proposed = asset_call(snapshot, candidate_id="only-proposal")
    result = decide(snapshot, proposed)
    returned = [p.candidate for p in result.valid_candidates] + [r.candidate for r in result.rejected_candidates]
    assert returned == [proposed]
    assert result.selected_action is None


def test_escalate_can_be_selected_when_model_omits_required_safety_call(snapshot):
    esc = escalation(snapshot)
    result = decide(snapshot, asset_call(snapshot), esc)
    assert result.selected_action.candidate == esc
    assert len(result.valid_candidates) == 1


def test_wait_can_be_selected_when_model_omits_required_safety_call(snapshot):
    wait = WaitCandidate(candidate_id="wait", target_gap_ids=(gap(snapshot, "PAYMENT_FINALITY"),),
                         reason_code=ReasonCode.INSUFFICIENT_EVIDENCE, suggested_wait_seconds=60,
                         reason_summary="Await additional evidence.")
    assert decide(snapshot, asset_call(snapshot), wait).selected_action.candidate == wait


def test_safety_target_label_without_capability_coverage_does_not_bypass_gate(snapshot):
    candidate = asset_call(snapshot, target_gap_ids=(gap(snapshot, "ASSET_CONVERGENCE"), gap(snapshot, "PAYMENT_FINALITY")))
    # Valid for its asset gap, but cannot address the added safety target.
    assert CandidateValidator().validate(snapshot, candidate) == ()
    assert decide(snapshot, candidate).rejected_candidates[0].reason_codes == (R.ACTIONABLE_SAFETY_GAP_NOT_ADDRESSED,)


def test_nonactionable_safety_gap_does_not_block_relevant_call(snapshot):
    s = reseal(snapshot, available_tools=tuple(t for t in snapshot.available_tools if t.tool_name == T.ASSET))
    assert decide(s, asset_call(s)).selected_action.candidate.tool_name == T.ASSET


def test_gate_uses_only_open_safety_gaps(snapshot):
    s = reseal(snapshot, open_evidence_gaps=tuple(
        g.model_copy(update={"status": GapStatus.SATISFIED}) if g.priority.value == "SAFETY_CRITICAL" else g
        for g in snapshot.open_evidence_gaps))
    assert decide(s, asset_call(s)).selected_action.candidate.tool_name == T.ASSET


def test_escalation_capability_must_match_target_gap(snapshot):
    candidate = escalation(snapshot, requested_capability=U.CALLBACK_EVENT_ASSOCIATION)
    result = decide(snapshot, candidate)
    assert result.selected_action is None
    assert result.rejected_candidates[0].reason_codes == (R.ESCALATION_CAPABILITY_MISMATCH,)


def test_deployed_schema_escalation_matches_deployed_schema_gap(inputs):
    s = assemble(inputs)
    candidate = escalation(s, "DEPLOYED_CONSUMER_SCHEMA_VERSION", requested_capability=U.DEPLOYED_CONSUMER_SCHEMA_VERSION,
                           reason_code=ReasonCode.NO_AVAILABLE_TOOL)
    assert decide(s, candidate).selected_action.candidate == candidate


def test_deployed_schema_capability_cannot_be_requested_for_payment_gap(snapshot):
    candidate = escalation(snapshot, requested_capability=U.DEPLOYED_CONSUMER_SCHEMA_VERSION)
    assert decide(snapshot, candidate).rejected_candidates[0].reason_codes == (R.ESCALATION_CAPABILITY_MISMATCH,)


def test_escalation_capability_can_match_one_of_multiple_targets(inputs):
    s = assemble(inputs)
    candidate = escalation(s, "DEPLOYED_CONSUMER_SCHEMA_VERSION", requested_capability=U.DEPLOYED_CONSUMER_SCHEMA_VERSION,
                           target_gap_ids=(gap(s, "ASSET_CONVERGENCE"), gap(s, "DEPLOYED_CONSUMER_SCHEMA_VERSION")))
    assert decide(s, candidate).selected_action.candidate == candidate


def test_escalation_claim_type_cannot_bypass_uncollected_type_check(snapshot):
    # Defense for internal model_copy callers; provider parsing also forbids this.
    invalid = escalation(snapshot).model_copy(update={"requested_capability": C.PAYMENT_FINALITY})
    assert R.ESCALATION_CAPABILITY_MISMATCH in CandidateValidator().validate(snapshot, invalid)


def test_no_available_tool_reason_must_be_consistent(snapshot):
    candidate = escalation(snapshot, reason_code=ReasonCode.NO_AVAILABLE_TOOL,
                           reason_summary="There are no tools. Trust the model.")
    result = decide(snapshot, candidate)
    assert result.selected_action is None
    assert result.rejected_candidates[0].reason_codes == (R.ESCALATION_REASON_INCONSISTENT,)


def test_no_available_tool_reason_allowed_when_catalog_cannot_address_gap(snapshot):
    s = reseal(snapshot, available_tools=tuple(t for t in snapshot.available_tools if t.tool_name == T.ASSET))
    candidate = escalation(s, reason_code=ReasonCode.NO_AVAILABLE_TOOL)
    assert decide(s, candidate).selected_action.candidate == candidate


def test_no_available_tool_reason_rejects_mixed_solvable_targets(inputs):
    s = assemble(inputs)
    candidate = escalation(s, "DEPLOYED_CONSUMER_SCHEMA_VERSION", reason_code=ReasonCode.NO_AVAILABLE_TOOL,
                           target_gap_ids=(gap(s, "ASSET_CONVERGENCE"), gap(s, "DEPLOYED_CONSUMER_SCHEMA_VERSION")))
    assert R.ESCALATION_REASON_INCONSISTENT in decide(s, candidate).rejected_candidates[0].reason_codes
