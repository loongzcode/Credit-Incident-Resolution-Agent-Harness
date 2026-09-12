from contextvars import ContextVar
from credit_harness.context.budget import digest
from .models import (InvestigationGuidanceBundle, SkillRef, MemoryError, GuidanceInvariant,
                     OrganizationalGuidanceSection, HistoricalGuidanceSection, GuidanceBuildStatus as S, GuidanceDegradation as D, GuidanceBuildResult)
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
    def __init__(self, skills, retriever, *, selection_provider=None):
        self.skills, self.retriever = skills, retriever
        self.selection_provider = selection_provider
        self._legacy_result = ContextVar("guidance_result", default=GuidanceBuildResult(bundle=None, status=S.EMPTY, degradation=D.NONE))

    @property
    def last_status(self):
        return self._legacy_result.get().status

    @property
    def last_degradation(self):
        return self._legacy_result.get().degradation

    def build(self, snapshot):
        result = self.build_result(snapshot)
        self._legacy_result.set(result)
        return result.bundle

    def build_result(self, snapshot) -> GuidanceBuildResult:
        status = S.INVALID_SKILL
        degradation = D.NONE
        try:
            tenant = self.retriever.repository.tenant_id
            if self.skills.tenant_id != tenant:
                raise MemoryError("guidance tenant mismatch")
            signature = current_signature(snapshot)
            self.retriever.repository.cases.get(snapshot.case_id)
            if self.selection_provider is not None:
                status = S.RETRIEVAL_FAILED
                selection = self.selection_provider.select(snapshot)
                matching = selection.skills
                status = S.INVALID_SKILL
            else:
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
                degradation = D.BUDGET_DROPPED
            status = S.RETRIEVAL_FAILED
            experiences = selection.experiences if self.selection_provider is not None else self.retriever.retrieve(snapshot, signature)
            status = S.EMPTY
            while True:
                bundle = InvestigationGuidanceBundle(tenant_id=tenant, case_id=snapshot.case_id,
                    snapshot_id=snapshot.snapshot_id, active_skills=tuple(selected), verified_experiences=experiences,
                    guidance_fingerprint="0" * 64, skill_versions=tuple(s.skill for s in selected),
                    experience_ids=tuple(e.experience_id for e in experiences))
                if len(bundle.model_dump_json()) <= MAX_GUIDANCE_CHARS:
                    break
                if experiences:
                    experiences = experiences[:-1]
                    degradation = D.BUDGET_DROPPED
                elif any(s.evidence_strategy for s in selected):
                    degradation = D.BUDGET_DROPPED
                    for i in range(len(selected) - 1, -1, -1):
                        if selected[i].evidence_strategy:
                            selected[i] = selected[i].model_copy(update={"evidence_strategy": selected[i].evidence_strategy[:-1]})
                            break
                else:
                    return GuidanceBuildResult(bundle=None, status=status, degradation=degradation)  # never remove safety
            if not selected and not experiences:
                status = S.EMPTY
                return GuidanceBuildResult(bundle=None, status=status, degradation=degradation)
            bundle = bundle.model_copy(update={"guidance_fingerprint": guidance_identity(bundle)})
            status = S.INVALID_SKILL
            result = validate_guidance(bundle, snapshot)
            status = S.AVAILABLE
            return GuidanceBuildResult(bundle=result, status=status, degradation=degradation)
        except Exception:
            # Sanitized, no raw historical content in exceptions/logs. Base
            # policy remains available even if DB/skill/retrieval is unavailable.
            return GuidanceBuildResult(bundle=None, status=status, degradation=degradation)
