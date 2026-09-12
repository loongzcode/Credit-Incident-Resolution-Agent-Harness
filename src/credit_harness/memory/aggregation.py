from collections import Counter, defaultdict
from statistics import median
from credit_harness.domain.models import Model
from credit_harness.domain.enums import ToolName
from credit_harness.evaluation.models import OutcomePath
from credit_harness.context.models import Hash
from credit_harness.hypotheses.models import PriorityClass
from .models import SkillImprovementProposal, EvidencePriorityChange


class ToolYield(Model):
    tool: ToolName
    calls: int
    calls_with_new_evidence: int
    new_evidence_count: int
    median_position: float


class OutcomeCount(Model):
    outcome: OutcomePath
    count: int


class FirstUsefulCount(Model):
    tool: ToolName
    count: int


class PatternStatistics(Model):
    sample_size: int
    experience_ids: tuple[Hash, ...]
    outcome_counts: tuple[OutcomeCount, ...]
    first_evidence_yield: tuple[FirstUsefulCount, ...]
    tool_yield: tuple[ToolYield, ...]

    @property
    def first_useful_evidence(self):
        return self.first_evidence_yield


class ExperiencePatternAggregator:
    def __init__(self, repository):
        self.repository = repository

    def summarize(self, signature=None):
        experiences = tuple(e for e in self.repository.active() if signature is None or e.incident_signature == signature)
        outcomes, first, calls = Counter(), Counter(), defaultdict(list)
        for e in experiences:
            outcomes[e.outcome_path] += 1
            useful = next((s.tool for s in e.investigation_sequence if s.new_evidence_count), None)
            if useful:
                first[useful] += 1
            for s in e.investigation_sequence:
                calls[s.tool].append(s)
        return PatternStatistics(sample_size=len(experiences), experience_ids=tuple(e.experience_id for e in experiences),
            outcome_counts=tuple(OutcomeCount(outcome=k, count=outcomes[k]) for k in sorted(outcomes)),
            first_evidence_yield=tuple(FirstUsefulCount(tool=k, count=first[k]) for k in sorted(first)),
            tool_yield=tuple(ToolYield(tool=k, calls=len(calls[k]), calls_with_new_evidence=sum(s.new_evidence_count > 0 for s in calls[k]),
                new_evidence_count=sum(s.new_evidence_count for s in calls[k]),
                median_position=median(s.sequence for s in calls[k])) for k in sorted(calls)))

    def propose(self, skill, now):
        experiences = self.repository.active()
        if not experiences:
            return None
        # Advisory only: a repeated early observed yield proposes review of
        # priority, never edits/activates a skill or claims measured improvement.
        counts = Counter(c for e in experiences for c in {c for s in e.investigation_sequence[:3]
                         if s.new_evidence_count for c in s.produced_claim_types})
        existing_safety = {c for strategy in skill.evidence_strategy if strategy.priority == PriorityClass.SAFETY_CRITICAL
                           for c in strategy.recommended_claim_types}
        changes = tuple(EvidencePriorityChange(claim_type=c, proposed_priority=PriorityClass.DISCRIMINATING)
            for c in sorted(counts) if counts[c] >= 2 and c not in existing_safety)
        return SkillImprovementProposal(skill_id=skill.skill_id, base_version=skill.version,
            proposed_evidence_priority_changes=changes, supporting_experience_ids=tuple(e.experience_id for e in experiences),
            sample_size=len(experiences), generated_at=now)
