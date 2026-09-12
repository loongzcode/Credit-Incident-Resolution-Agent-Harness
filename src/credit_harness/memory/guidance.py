from credit_harness.context.budget import digest
from .models import (InvestigationGuidanceBundle, SkillRef, MemoryError, GuidanceInvariant,
                     OrganizationalGuidanceSection, HistoricalGuidanceSection, GuidanceBuildStatus as S, GuidanceDegradation as D)
from .skills import SkillComposer
from .retrieval import current_signature, applicable, symptom_matches

MAX_GUIDANCE_CHARS = 12000
MAX_STRATEGIES = 8


def guidance_identity(bundle):
    return digest(bundle.model_dump(mode="json", exclude={"guidance_fingerprint"}))


def validate_guidance(bundle, snapshot):
    if type(bundle) is not InvestigationGuidanceBundle:
        raise MemoryError("typed guidance required")
    bundle = InvestigationGuidanceBundle.model_validate(bundle.model_dump())
    if (bundle.case_id != snapshot.case_id or bundle.snapshot_id != snapshot.snapshot_id
            or bundle.guidance_fingerprint != guidance_identity(bundle)
            or bundle.skill_versions != tuple(s.skill for s in bundle.active_skills)
            or bundle.experience_ids != tuple(e.experience_id for e in bundle.verified_experiences)
            or len(set(bundle.skill_versions)) != len(bundle.skill_versions)
            or len(set(bundle.experience_ids)) != len(bundle.experience_ids)
            or sum(len(s.evidence_strategy) for s in bundle.active_skills) > MAX_STRATEGIES
            or len(bundle.model_dump_json()) > MAX_GUIDANCE_CHARS):
        raise MemoryError("invalid guidance binding or budget")
    for s in bundle.active_skills:
        if any(not r.enforced for r in s.safety_invariants) or not set(GuidanceInvariant) <= {r.invariant for r in s.safety_invariants}:
            raise MemoryError("unsafe guidance")
    return bundle


class InvestigationGuidanceService:
    """Optional dependency. Any invalid/unavailable guidance becomes no guidance.

    Neither this service nor its consumers publish facts or change business state.
    """
    def __init__(self, skills, retriever):
        self.skills, self.retriever = skills, retriever
        self.last_status = S.EMPTY
        self.last_degradation = D.NONE

    def build(self, snapshot):
        self.last_status = S.INVALID_SKILL
        self.last_degradation = D.NONE
        try:
            tenant = self.retriever.repository.tenant_id
            if self.skills.tenant_id != tenant:
                raise MemoryError("guidance tenant mismatch")
            signature = current_signature(snapshot)
            self.retriever.repository.cases.get(snapshot.case_id)
            matching = [s for s in self.skills.active() if applicable(s.scope, signature)
                        and symptom_matches(s.applies_when, signature)]
            # Validate ALL overlays before compaction so a dropped conflicting
            # skill cannot silently change safety composition.
            composed = SkillComposer().compose(matching)
            selected, remaining = [], MAX_STRATEGIES
            for s in composed[:4]:
                strategies = s.evidence_strategy[:remaining]
                remaining -= len(strategies)
                selected.append(s.model_copy(update={"evidence_strategy": strategies}))
            if sum(len(s.evidence_strategy) for s in selected) < sum(len(s.evidence_strategy) for s in composed):
                self.last_degradation = D.BUDGET_DROPPED
            self.last_status = S.RETRIEVAL_FAILED
            experiences = self.retriever.retrieve(snapshot, signature)
            self.last_status = S.EMPTY
            while True:
                bundle = InvestigationGuidanceBundle(tenant_id=tenant, case_id=snapshot.case_id,
                    snapshot_id=snapshot.snapshot_id, active_skills=tuple(selected), verified_experiences=experiences,
                    guidance_fingerprint="0" * 64, skill_versions=tuple(s.skill for s in selected),
                    experience_ids=tuple(e.experience_id for e in experiences))
                if len(bundle.model_dump_json()) <= MAX_GUIDANCE_CHARS:
                    break
                if experiences:
                    experiences = experiences[:-1]
                    self.last_degradation = D.BUDGET_DROPPED
                elif any(s.evidence_strategy for s in selected):
                    self.last_degradation = D.BUDGET_DROPPED
                    for i in range(len(selected) - 1, -1, -1):
                        if selected[i].evidence_strategy:
                            selected[i] = selected[i].model_copy(update={"evidence_strategy": selected[i].evidence_strategy[:-1]})
                            break
                else:
                    return None  # never remove individual safety invariants
            if not selected and not experiences:
                self.last_status = S.EMPTY
                return None
            bundle = bundle.model_copy(update={"guidance_fingerprint": guidance_identity(bundle)})
            self.last_status = S.INVALID_SKILL
            result = validate_guidance(bundle, snapshot)
            self.last_status = S.AVAILABLE
            return result
        except Exception:
            # Sanitized, no raw historical content in exceptions/logs. Base
            # policy remains available even if DB/skill/retrieval is unavailable.
            return None
