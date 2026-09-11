from credit_harness.hypotheses.models import HypothesisId as H, HypothesisStatus as S
from credit_harness.remediation.model import FakeRemediationModel
from credit_harness.remediation.models import RemediationActionType as A, RemediationCandidate, RemediationDraft


def propose_from_context(bundle):
    state = bundle.deterministic_derived
    refs = tuple(sorted({r for f in bundle.untrusted_external_data.current_facts for r in f.evidence_refs}
                        | {r for h in state.hypotheses for r in h.decisive_evidence_refs}
                        | set(state.financial_identity.evidence_refs)))
    def candidate(name, action, problems):
        return RemediationCandidate(candidate_id=name, action_type=action,
            target_order_id=bundle.trusted_control.internal_order_id, target_problem_ids=problems,
            evidence_refs=refs, reason_summary="建议仅基于当前可见事实；兼容性和副作用前置条件交由 Harness 校验。")
    confirmed = {h.hypothesis_id for h in state.hypotheses if h.status == S.CONFIRMED}
    problems = tuple(g.gap_id for g in state.open_gaps) or tuple(h.value for h in confirmed)
    primary = (candidate("replay", A.REPLAY_CALLBACK_CONSUMPTION, (H.H6.value,)) if H.H6 in confirmed
               else candidate("none", A.NO_REMEDIATION, problems))
    review = candidate("review", A.REQUEST_OPERATOR_REVIEW, problems)
    return RemediationDraft(snapshot_id=bundle.snapshot_id, candidates=(primary, review))


def remediation_fake_model():
    return FakeRemediationModel(propose_from_context)
