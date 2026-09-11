from credit_harness.context.budget import digest
from credit_harness.planner.models import PlannerModelMetadata
from .models import (RemediationDecision, RejectedRemediationCandidate, RemediationPreview,
                     PreflightStatus as S, RejectReason as R, RemediationUnavailable, RemediationProtocolError)
from .catalog import RemediationActionCatalog
from .state import RemediationEligibilityEvaluator
from .renderer import RemediationInputRenderer
from .model import RemediationModel, parse_draft
from .validator import RemediationCandidateValidator
from .preflight import RemediationPreflightService
from .audit import RemediationAuditRecord, InMemoryRemediationAuditStore


class RemediationPlanner:
    """Trusted independent proposal stage. No executor, tool client or case writer."""
    def __init__(self, reader, model: RemediationModel, *, audit=None):
        self.reader, self.model = reader, model
        self.audit = audit if audit is not None else InMemoryRemediationAuditStore()
        self.validator = RemediationCandidateValidator()
        self.preflight = RemediationPreflightService(reader)

    def plan(self, case_id):
        state = self.reader.read(case_id)
        eligibility = RemediationEligibilityEvaluator().evaluate(state)
        valid, rejected, previews, results = [], [], [], []
        bundle, draft, metadata = None, None, None
        if eligibility.can_plan:
            bundle = RemediationInputRenderer().render(state.snapshot)
            try:
                draft = parse_draft(self.model.plan(bundle))
                metadata = PlannerModelMetadata.model_validate(self.model.metadata.model_dump())
            except RemediationUnavailable:
                raise
            except Exception:
                raise RemediationUnavailable("remediation model unavailable") from None
            if draft.snapshot_id != state.snapshot.snapshot_id:
                raise RemediationProtocolError("draft snapshot mismatch")
            for candidate in draft.candidates:
                checked = self.validator.validate(state, candidate)
                if isinstance(checked, RejectedRemediationCandidate):
                    rejected.append(checked)
                    previews.append(RemediationPreview(candidate=candidate,
                        risk_level=RemediationActionCatalog().get(candidate.action_type).risk_level,
                        status=S.BLOCKED, blocking_reasons=checked.reason_codes))
                else:
                    valid.append(checked)
                    preview = self.preflight.preview(checked)
                    results.append(preview)
                    previews.append(RemediationPreview(candidate=candidate, risk_level=checked.risk_level,
                        status=preview.status, blocking_reasons=preview.reason_codes, target=checked.target))
        # Only choose proposed candidates whose fresh preflight succeeded. No
        # generated fallback. Static selection role precedes scope and risk.
        current = self.reader.read(case_id)
        # A preview may become stale while another candidate is checked. Never
        # return that older READY intent, including in diagnostic/audit results.
        stale_ids = set()
        for i, result in enumerate(results):
            if result.status == S.READY_FOR_FUTURE_AUTHORIZATION and result.fresh_snapshot_id != current.snapshot.snapshot_id:
                stale_ids.add(result.candidate_id)
                updates = dict(status=S.STALE, reason_codes=(R.STALE_SNAPSHOT,), intent=None,
                               fresh_snapshot_id=current.snapshot.snapshot_id)
                fingerprint = digest(dict(previous_preflight=result.preflight_fingerprint,
                    snapshot_id=current.snapshot.snapshot_id, status=S.STALE.value))
                results[i] = result.model_copy(update={**updates, "preflight_fingerprint": fingerprint})
        previews = [p.model_copy(update={"status": S.STALE, "blocking_reasons": (R.STALE_SNAPSHOT,)})
                    if p.candidate.candidate_id in stale_ids else p for p in previews]
        ready = [(v, p) for v in valid for p in results if p.candidate_id == v.candidate.candidate_id
                 and p.status == S.READY_FOR_FUTURE_AUTHORIZATION
                 and p.fresh_snapshot_id == current.snapshot.snapshot_id]
        selected, preflight = min(ready, key=lambda item: self.validator.catalog.selection_key(
            item[0].candidate)) if ready else (None, None)
        intent = preflight.intent if preflight else None
        draft_hash = digest(draft.model_dump(mode="json")) if draft else None
        body = dict(snapshot_id=state.snapshot.snapshot_id, draft_hash=draft_hash, eligibility=eligibility,
            valid_candidates=tuple(valid), rejected_candidates=tuple(rejected), selected_candidate=selected,
            preflight_results=tuple(results), preflight_result=preflight, previews=tuple(previews),
            final_intent=intent, model_metadata=metadata)
        temporary = RemediationDecision(decision_id="0" * 64, **body)
        decision = temporary.model_copy(update={"decision_id": digest(temporary.model_dump(mode="json", exclude={"decision_id"}))})
        self.audit.append(RemediationAuditRecord(decision_id=decision.decision_id, case_id=case_id,
            snapshot_id=decision.snapshot_id, input_hash=digest(bundle.model_dump(mode="json")) if bundle else None,
            draft_hash=draft_hash, selected_candidate=selected.candidate if selected else None,
            rejection_reasons=decision.rejected_candidates, preflight_results=decision.preflight_results,
            intent_id=intent.intent_id if intent else None, model_metadata=metadata))
        return decision  # STOP. No authorization, command creation or dispatch.
