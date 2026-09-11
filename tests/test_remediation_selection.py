from itertools import permutations

import pytest
from pydantic import ValidationError

from tests.test_remediation import prepared, harness, candidate, business_state, CASE
from tests.support.remediation_fixture import install_deployment_observation
from credit_harness.domain.enums import ScenarioId
from credit_harness.remediation.models import (
    RemediationActionType as A, SelectionRole, PreflightStatus as S, RejectReason as R,
    RemediationDraft, RemediationCandidate, RejectedRemediationCandidate,
)
from credit_harness.remediation.model import FakeRemediationModel
from credit_harness.remediation.catalog import RemediationActionCatalog
from credit_harness.remediation.validator import RemediationCandidateValidator
from credit_harness.remediation.preflight import RemediationPreflightService
from credit_harness.remediation.service import RemediationPlanner


def proposals(fixture, actions):
    state = fixture.reader.read(CASE)
    items = []
    for action in actions:
        extra = {}
        if action == A.REQUEST_OPERATOR_REVIEW:
            extra['target_problem_ids'] = tuple(g.gap_id for g in state.graph.open_gaps) or tuple(h.value for h in state.graph.confirmed)
        items.append(candidate(state, action, candidate_id=action.value, **extra))
    return tuple(items)


def plan(fixture, items):
    before = business_state(fixture)
    model = FakeRemediationModel(lambda bundle: RemediationDraft(snapshot_id=bundle.snapshot_id, candidates=items))
    decision = RemediationPlanner(fixture.reader, model).plan(CASE)
    assert business_state(fixture) == before
    assert decision.final_intent is None or decision.final_intent.status == 'PROPOSED'
    return decision


@pytest.mark.parametrize('direct,scenario', [(A.REPLAY_CALLBACK_CONSUMPTION, ScenarioId.S6),
                                           (A.REDELIVER_ASSET_NOTIFICATION, ScenarioId.S7)])
def test_ready_direct_remediation_is_not_starved_by_review(prepared, monkeypatch, direct, scenario):
    install_deployment_observation(monkeypatch)
    f = prepared(scenario)
    decision = plan(f, proposals(f, (direct, A.REQUEST_OPERATOR_REVIEW)))
    assert all(p.status == S.READY_FOR_FUTURE_AUTHORIZATION for p in decision.preflight_results)
    assert decision.final_intent.action_type == direct


def test_ready_direct_remediation_is_not_starved_by_no_remediation(prepared, monkeypatch):
    install_deployment_observation(monkeypatch)
    f = prepared()
    decision = plan(f, proposals(f, (A.NO_REMEDIATION, A.REPLAY_CALLBACK_CONSUMPTION)))
    assert decision.final_intent.action_type == A.REPLAY_CALLBACK_CONSUMPTION


def test_blocked_direct_remediation_falls_back_to_review(prepared):
    f = prepared()
    decision = plan(f, proposals(f, (A.REPLAY_CALLBACK_CONSUMPTION, A.NO_REMEDIATION, A.REQUEST_OPERATOR_REVIEW)))
    assert decision.preflight_results[0].reason_codes == (R.DEPLOYMENT_STATE_UNKNOWN,)
    assert decision.final_intent.action_type == A.REQUEST_OPERATOR_REVIEW


def test_no_remediation_selected_only_when_no_better_ready_candidate(prepared):
    f = prepared(ScenarioId.S8)
    decision = plan(f, proposals(f, (A.REQUEST_OPERATOR_REVIEW, A.NO_REMEDIATION)))
    assert decision.final_intent.action_type == A.REQUEST_OPERATOR_REVIEW
    decision = plan(f, proposals(f, (A.REPLAY_CALLBACK_CONSUMPTION, A.NO_REMEDIATION)))
    assert decision.final_intent.action_type == A.NO_REMEDIATION
    assert R.IDENTITY_UNKNOWN in decision.rejected_candidates[0].reason_codes


def test_harness_does_not_synthesize_direct_remediation(prepared, monkeypatch):
    install_deployment_observation(monkeypatch)
    f = prepared()
    decision = plan(f, proposals(f, (A.NO_REMEDIATION, A.REQUEST_OPERATOR_REVIEW)))
    assert decision.final_intent.action_type == A.REQUEST_OPERATOR_REVIEW
    assert {v.candidate.action_type for v in decision.valid_candidates} == {A.NO_REMEDIATION, A.REQUEST_OPERATOR_REVIEW}


def test_candidate_order_does_not_change_remediation_selection(prepared, monkeypatch):
    install_deployment_observation(monkeypatch)
    f = prepared()
    items = proposals(f, (A.NO_REMEDIATION, A.REQUEST_OPERATOR_REVIEW, A.REPLAY_CALLBACK_CONSUMPTION))
    decisions = [plan(f, ordering) for ordering in permutations(items)]
    assert {d.final_intent.action_type for d in decisions} == {A.REPLAY_CALLBACK_CONSUMPTION}
    assert len({d.final_intent.intent_id for d in decisions}) == 1


def test_administrative_remediation_precedes_fallbacks(prepared):
    f = prepared(ScenarioId.S7)
    decision = plan(f, proposals(f, (A.REQUEST_OPERATOR_REVIEW, A.CREATE_RECONCILIATION_TASK, A.NO_REMEDIATION)))
    assert all(p.status == S.READY_FOR_FUTURE_AUTHORIZATION for p in decision.preflight_results)
    assert decision.final_intent.action_type == A.CREATE_RECONCILIATION_TASK


def test_direct_remediation_precedes_administrative_remediation(prepared):
    f = prepared(ScenarioId.S7)
    decision = plan(f, proposals(f, (A.CREATE_RECONCILIATION_TASK, A.REDELIVER_ASSET_NOTIFICATION)))
    assert decision.final_intent.action_type == A.REDELIVER_ASSET_NOTIFICATION


def test_every_catalog_action_has_explicit_validator_handler():
    handlers = RemediationCandidateValidator.VALIDATOR_HANDLERS
    assert {c.action_type for c in RemediationActionCatalog.entries} == set(handlers) == set(A)
    assert all(callable(handler) for handler in handlers.values())


def test_every_catalog_action_has_explicit_preflight_handler():
    handlers = RemediationPreflightService.PREFLIGHT_HANDLERS
    assert {c.action_type for c in RemediationActionCatalog.entries} == set(handlers) == set(A)
    assert all(callable(handler) for handler in handlers.values())


@pytest.mark.parametrize('phase', ['validator', 'preflight'])
@pytest.mark.parametrize('action', list(A))
def test_unhandled_catalog_action_fails_closed(prepared, monkeypatch, action, phase):
    install_deployment_observation(monkeypatch)
    f = prepared(ScenarioId.S7 if action in (A.REDELIVER_ASSET_NOTIFICATION, A.CREATE_RECONCILIATION_TASK) else ScenarioId.S6)
    c, = proposals(f, (action,))
    checked = RemediationCandidateValidator().validate(f.reader.read(CASE), c)
    assert not isinstance(checked, RejectedRemediationCandidate)
    cls, name = ((RemediationCandidateValidator, 'VALIDATOR_HANDLERS') if phase == 'validator'
                 else (RemediationPreflightService, 'PREFLIGHT_HANDLERS'))
    monkeypatch.setattr(cls, name, {k: v for k, v in getattr(cls, name).items() if k != action})
    if phase == 'validator':
        rejected = RemediationCandidateValidator().validate(f.reader.read(CASE), c)
        assert R.ACTION_NOT_ALLOWED in rejected.reason_codes
    result = RemediationPreflightService(f.reader).preview(checked)
    assert result.status == S.BLOCKED and result.reason_codes == (R.ACTION_NOT_ALLOWED,)
    assert result.intent is None
    decision = plan(f, (c,))
    assert decision.final_intent is None and decision.selected_candidate is None
    assert decision.previews[0].blocking_reasons == (R.ACTION_NOT_ALLOWED,)


def test_selection_role_is_static_catalog_metadata():
    catalog = RemediationActionCatalog()
    assert catalog.get(A.REPLAY_CALLBACK_CONSUMPTION).selection_role == SelectionRole.DIRECT_REMEDIATION
    assert catalog.get(A.REDELIVER_ASSET_NOTIFICATION).selection_role == SelectionRole.DIRECT_REMEDIATION
    assert catalog.get(A.CREATE_RECONCILIATION_TASK).selection_role == SelectionRole.ADMINISTRATIVE_REMEDIATION
    assert catalog.get(A.REQUEST_OPERATOR_REVIEW).selection_role == SelectionRole.HUMAN_FALLBACK
    assert catalog.get(A.NO_REMEDIATION).selection_role == SelectionRole.NO_ACTION
    with pytest.raises(ValidationError):
        RemediationCandidate(candidate_id='fake', action_type=A.NO_REMEDIATION, target_order_id='ORDER-1',
            target_problem_ids=(), evidence_refs=(), reason_summary='', selection_role=SelectionRole.DIRECT_REMEDIATION)
