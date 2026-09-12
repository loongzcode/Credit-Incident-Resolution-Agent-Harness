from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.context.budget import digest
from .renderer import ModelInputRenderer
from .model import PlannerModel, parse_draft
from .models import (
    PlannerDecision, PlannerProtocolError, PlannerUnavailable, RejectedCandidate,
    ValidatedActionProposal, PlannerModelMetadata, PLANNER_POLICY_VERSION, ACTION_RANKING_VERSION,
)
from .validator import CandidateValidator
from .policy import HardPolicyFilter
from .ranking import DeterministicActionRanker
from .audit import InMemoryPlannerAuditStore, PlannerAuditRecord
from credit_harness.memory.models import GuidanceBuildStatus as GS, GuidanceDegradation as GD


class PlannerService:
    def __init__(self, model: PlannerModel, *, audit=None, guidance_provider=None):
        self.model = model
        self.guidance_provider = guidance_provider
        self.audit = audit if audit is not None else InMemoryPlannerAuditStore()

    def plan(self, snapshot: ReasoningContextSnapshot, *, guidance=None) -> PlannerDecision:
        renderer = ModelInputRenderer()
        build_status = GS.EMPTY
        degradation = GD.NONE
        if guidance is None and self.guidance_provider is not None:
            try:
                result = self.guidance_provider.build_result(snapshot)
                guidance, build_status, degradation = result.bundle, result.status, result.degradation
            except Exception:
                guidance = None
                build_status = GS.RETRIEVAL_FAILED
        bundle = renderer.render(snapshot, guidance)
        if bundle.guidance_fingerprint:
            build_status = GS.AVAILABLE
        elif guidance is not None:
            build_status = GS.INVALID_SKILL
        guidance_refs = dict(guidance_fingerprint=bundle.guidance_fingerprint,
            guidance_build_status=build_status, guidance_degradation=degradation,
            skill_refs=tuple(s.skill for s in bundle.organizational_guidance.active_skills) if bundle.organizational_guidance else (),
            experience_refs=tuple(e.experience_id for e in bundle.historical_guidance.verified_experiences) if bundle.historical_guidance else ())
        input_hash = digest(bundle.model_dump(mode="json"))
        try:
            draft = parse_draft(self.model.plan(bundle))
            metadata = PlannerModelMetadata.model_validate(self.model.metadata.model_dump())
        except PlannerUnavailable:
            raise
        except Exception:
            # Never echo provider exceptions (which may contain credentials/data).
            raise PlannerUnavailable("planner provider unavailable") from None
        if draft.snapshot_id != snapshot.snapshot_id:
            raise PlannerProtocolError("draft snapshot_id does not match input")
        valid, rejected = [], []
        for candidate in draft.candidates:
            reasons = CandidateValidator().validate(snapshot, candidate)
            if not reasons:
                reasons = HardPolicyFilter().filter(snapshot, candidate)
            if reasons:
                rejected.append(RejectedCandidate(candidate=candidate, reason_codes=reasons))
            else:
                valid.append(ValidatedActionProposal(snapshot_id=snapshot.snapshot_id, candidate=candidate))
        ranked, details = DeterministicActionRanker().rank(snapshot, valid)
        output_hash = digest(draft.model_dump(mode="json"))
        decision_id = digest({"snapshot": snapshot.snapshot_id, "input": input_hash, "output": output_hash,
                              "policy": PLANNER_POLICY_VERSION, "ranking": ACTION_RANKING_VERSION,
                              "provider": metadata.model_provider, "model": metadata.model_name})
        reason = ("No candidate passed validation and hard policy; no action authorized." if not ranked else
                  "Lexicographic priority, novel requirement coverage, claim coverage, hypothesis discrimination, "
                  "cost/latency, repeat penalty, stable candidate ID; hard policy passed. Proposal only.")
        decision = PlannerDecision(decision_id=decision_id, snapshot_id=snapshot.snapshot_id,
            planner_draft_hash=output_hash, valid_candidates=tuple(valid), rejected_candidates=tuple(rejected),
            selected_action=ranked[0] if ranked else None, selection_reason=reason, ranking_details=details,
            planner_model_metadata=metadata, created_at=snapshot.assembled_at, **guidance_refs)
        self.audit.append(PlannerAuditRecord(decision_id=decision_id, case_id=snapshot.case_id,
            snapshot_id=snapshot.snapshot_id, planner_schema_version=decision.planner_schema_version,
            policy_version=decision.policy_version, ranking_version=decision.ranking_version,
            model_input_schema_version=decision.model_input_schema_version,
            model_provider=metadata.model_provider, model_name=metadata.model_name,
            input_hash=input_hash, output_hash=output_hash, validated_at=decision.created_at,
            selected_action=decision.selected_action, rejection_summary=decision.rejected_candidates,
            usage_metadata=metadata, **guidance_refs))
        # Intentional STOP. No execution client, callback, credential, or dispatcher.
        return decision
