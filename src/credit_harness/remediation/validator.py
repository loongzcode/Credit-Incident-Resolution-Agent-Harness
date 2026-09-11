from types import MappingProxyType
from credit_harness.context.budget import digest
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.models import HypothesisId as H, HypothesisStatus as S, PriorityClass
from credit_harness.hypotheses.rules import RuleFacts
from credit_harness.identity.models import IdentityMatch
from .catalog import RemediationActionCatalog, ForbiddenRemediationPolicy
from .models import (
    RemediationCandidate, RemediationTarget, ValidatedRemediationCandidate, RejectedRemediationCandidate,
    RemediationActionType as A, ActionRiskLevel as L, RejectReason as R,
)


def evidence_hash(index, refs):
    return digest([e.model_dump(mode="json") for e in index.evidence if e.evidence_id in set(refs)])


def same_record(left, right):
    return (left.subject == right.subject and left.observation_id == right.observation_id
            and left.event_time == right.event_time and left.content_hash == right.content_hash)


class RemediationCandidateValidator:
    def __init__(self, catalog=None):
        self.catalog = catalog or RemediationActionCatalog()

    def validate(self, state, candidate: RemediationCandidate):
        candidate = RemediationCandidate.model_validate(candidate.model_dump())
        entry = self.catalog.get(candidate.action_type)
        reasons = list(ForbiddenRemediationPolicy().check(entry))
        case, index, graph, snapshot = state.case, state.index, state.graph, state.snapshot
        cited = set(candidate.evidence_refs)
        by_id = {e.evidence_id: e for e in index.evidence}
        problems = {h.value for h in H} | {g.gap_id for g in graph.gaps}
        if candidate.target_order_id != case.internal_order_id or candidate.target_order_id not in case.scope.allowed_order_ids:
            reasons.append(R.FOREIGN_ORDER)
        if not set(candidate.target_problem_ids) <= problems:
            reasons.append(R.UNKNOWN_PROBLEM)
        if not cited <= by_id.keys():
            reasons.append(R.EVIDENCE_NOT_FOUND)
        handler = self.VALIDATOR_HANDLERS.get(candidate.action_type)
        if not callable(handler):
            reasons.append(R.ACTION_NOT_ALLOWED)
        if entry is None or not callable(handler):
            return RejectedRemediationCandidate(candidate=candidate, reason_codes=tuple(dict.fromkeys(reasons)))
        if case.status not in entry.required_case_state:
            reasons.append(R.CASE_NOT_ACTIONABLE)
        target = RemediationTarget(case_id=case.case_id, tenant_id=case.tenant_id, internal_order_id=case.internal_order_id)
        l2 = entry.risk_level == L.L2_SINGLE_ORDER_SIDE_EFFECT
        if l2:
            if graph.payment_identity.result != IdentityMatch.MATCH:
                reasons.append(R.IDENTITY_MISMATCH if graph.payment_identity.result == IdentityMatch.MISMATCH else R.IDENTITY_UNKNOWN)
            if any(g.priority_class == PriorityClass.SAFETY_CRITICAL for g in graph.open_gaps):
                reasons.append(R.UNRESOLVED_SAFETY_GAP)
            current = {e.evidence_id for claim in C for e in index.current(claim)}
            if not cited <= current:
                reasons.append(R.EVIDENCE_NOT_CURRENT)
            if not set(graph.payment_identity.evidence_refs) <= cited:
                reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        target, support, action_reasons = handler(self, state, candidate, target)
        reasons.extend(action_reasons)
        if reasons:
            return RejectedRemediationCandidate(candidate=candidate, reason_codes=tuple(dict.fromkeys(reasons)))
        refs = index.refs(support)
        return ValidatedRemediationCandidate(candidate=candidate, target=target, risk_level=entry.risk_level,
            supporting_evidence_refs=refs, snapshot_id=snapshot.snapshot_id, evidence_hash=evidence_hash(index, refs))

    def _validate_replay(self, state, candidate, target):
        case, index, graph = state.case, state.index, state.graph
        cited = set(candidate.evidence_refs)
        by_id = {e.evidence_id: e for e in index.evidence}
        facts = RuleFacts(index)
        statuses = {h.hypothesis_id: h.status for h in graph.hypotheses}
        reasons, support = [], ()
        if statuses[H.H6] != S.CONFIRMED:
            reasons.append(R.HYPOTHESIS_NOT_CONFIRMED)
        if not any(p in {H.H6.value, H.H6_SCHEMA_MISMATCH.value, f"{case.case_id}:CALLBACK_CONSUMPTION"}
                   for p in candidate.target_problem_ids):
            reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        groups = []
        for gateway, failed in facts.callback_pairs("FAILED"):
            signatures = tuple(e for e in facts.facts(C.CALLBACK_SIGNATURE_VERIFIED, True, {T.CALLBACK, T.CALLBACK_RAW})
                               if same_record(e, gateway))
            items = (gateway, failed, *signatures)
            mismatch = next((g for g in facts.mismatch_groups() if failed in g), ())
            schema_error = any(e.value == "CALLBACK_SCHEMA_MISMATCH" and e.subject == failed.subject
                               and e.metadata.callback_event_id == failed.metadata.callback_event_id
                               for e in index.current(C.MESSAGE_ERROR_CODE))
            if schema_error and not mismatch:
                reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
                continue
            # A schema failure must cite the complete technical failure witness.
            if mismatch:
                items += mismatch
                if statuses[H.H6_SCHEMA_MISMATCH] != S.CONFIRMED:
                    reasons.append(R.HYPOTHESIS_NOT_CONFIRMED)
            if signatures and set(index.refs(items)) <= cited:
                groups.append((gateway, failed, items))
        addresses = {(g.subject.identifier, m.subject.identifier) for g, m, _ in groups}
        if len(addresses) > 1:
            reasons.append(R.AMBIGUOUS_TARGET)
        elif groups:
            gateway, failed, items = groups[0]
            target = target.model_copy(update={"callback_event_ref": gateway.subject.identifier,
                                                "message_ref": failed.subject.identifier})
            support = (*items, *(by_id[r] for r in graph.payment_identity.evidence_refs))
        else:
            reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        return target, support, reasons

    def _validate_redelivery(self, state, candidate, target):
        index, graph = state.index, state.graph
        case = state.case
        cited = set(candidate.evidence_refs)
        by_id = {e.evidence_id: e for e in index.evidence}
        facts = RuleFacts(index)
        reasons, support = [], ()
        if not any(p in (H.H7.value, f"{case.case_id}:ASSET_CONVERGENCE") for p in candidate.target_problem_ids):
            reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        local = facts.facts(C.GUARANTEE_STATUS, "SUCCESS", {T.GUARANTEE})
        asset = facts.facts(C.ASSET_STATUS, "PROCESSING", {T.ASSET})
        delivery = facts.facts(C.ASSET_DELIVERY_STATUS, "FAILED", {T.ASSET_DELIVERY})
        refs = tuple(e for e in index.current(C.ASSET_DELIVERY_EVENT_REF)
                     if any(same_record(e, d) for d in delivery) and e.tool == T.ASSET_DELIVERY)
        if facts.facts(C.ASSET_STATUS, "SUCCESS", {T.ASSET}) or facts.facts(C.ASSET_DELIVERY_STATUS, "DELIVERED", {T.ASSET_DELIVERY}):
            reasons.append(R.ACTION_ALREADY_SATISFIED)
        if not local or not asset or not delivery or len({e.value for e in refs}) != 1:
            reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        else:
            support = (*local, *asset, *delivery, *refs, *(by_id[r] for r in graph.payment_identity.evidence_refs))
            target = target.model_copy(update={"delivery_ref": refs[0].value})
            if not set(index.refs(support)) <= cited:
                reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        return target, support, reasons

    def _validate_reconciliation(self, state, candidate, target):
        index = state.index
        cited = set(candidate.evidence_refs)
        facts = RuleFacts(index)
        reasons, support = [], ()
        local = tuple(e for value in ("SUCCESS", "PROCESSING", "FAILED")
                      for e in facts.facts(C.GUARANTEE_STATUS, value, {T.GUARANTEE}) if e.evidence_id in cited)
        asset = tuple(e for value in ("SUCCESS", "PROCESSING", "FAILED")
                      for e in facts.facts(C.ASSET_STATUS, value, {T.ASSET}) if e.evidence_id in cited)
        divergence = tuple(e for g in local for a in asset if g.value != a.value for e in (g, a))
        if not divergence:
            divergence = tuple(e for e in facts.facts(C.GUARANTEE_STATUS, "PROCESSING", {T.GUARANTEE})
                               if e.evidence_id in cited) if facts.settled_witness() else ()
            if divergence:
                divergence += facts.settled_witness()
        if not divergence or not set(index.refs(divergence)) <= cited or not candidate.target_problem_ids:
            reasons.append(R.EVIDENCE_DOES_NOT_SUPPORT_ACTION)
        support = divergence
        return target, support, reasons

    def _validate_review(self, state, candidate, target):
        index, graph = state.index, state.graph
        cited = set(candidate.evidence_refs)
        by_id = {e.evidence_id: e for e in index.evidence}
        reasons, support = [], ()
        active = {h.hypothesis_id.value for h in graph.hypotheses if h.status in (S.CONFIRMED, S.SUPPORTED)}
        active |= {g.gap_id for g in graph.open_gaps}
        if not set(candidate.target_problem_ids) & active:
            reasons.append(R.ACTION_NOT_NEEDED)
        support = tuple(by_id[r] for r in sorted(cited & by_id.keys()))
        return target, support, reasons

    def _validate_no_action(self, state, candidate, target):
        index = state.index
        cited = set(candidate.evidence_refs)
        by_id = {e.evidence_id: e for e in index.evidence}
        reasons, support = [], ()
        support = tuple(by_id[r] for r in sorted(cited & by_id.keys()))
        return target, support, reasons

    # No implicit default: a new catalog action must have a real handler.
    VALIDATOR_HANDLERS = MappingProxyType({
        A.REPLAY_CALLBACK_CONSUMPTION: _validate_replay,
        A.REDELIVER_ASSET_NOTIFICATION: _validate_redelivery,
        A.CREATE_RECONCILIATION_TASK: _validate_reconciliation,
        A.REQUEST_OPERATOR_REVIEW: _validate_review,
        A.NO_REMEDIATION: _validate_no_action,
    })
