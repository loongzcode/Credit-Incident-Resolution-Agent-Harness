from credit_harness.hypotheses.models import PriorityClass, HypothesisStatus
from credit_harness.context.models import EstimateClass
from credit_harness.domain.enums import Freshness, Completeness
from .models import CallToolCandidate, RankingDetail
from .validator import addressed_requirements
from .policy import repeated_count

PRIORITY = {PriorityClass.SAFETY_CRITICAL: 3, PriorityClass.DISCRIMINATING: 2, PriorityClass.SUPPORTING: 1}


class DeterministicActionRanker:
    def rank(self, snapshot, proposals):
        gaps = {g.gap_id: g for g in snapshot.open_evidence_gaps}
        tools = {t.tool_name: t for t in snapshot.available_tools}
        known = {f.claim_type for f in snapshot.current_facts
                 if f.freshness == Freshness.CURRENT and f.completeness == Completeness.COMPLETE}
        active = {h.hypothesis_id for h in snapshot.active_hypotheses
                  if h.status not in (HypothesisStatus.CONFIRMED, HypothesisStatus.ELIMINATED)}
        call_coverage = set()
        for p in proposals:
            c = p.candidate
            if isinstance(c, CallToolCandidate):
                call_coverage.update(g for g in c.target_gap_ids if addressed_requirements(tools[c.tool_name], gaps[g]))
        rows = []
        for p in proposals:
            c = p.candidate
            targets = [gaps[g] for g in set(c.target_gap_ids)]
            if isinstance(c, CallToolCandidate):
                tool = tools[c.tool_name]
                targets = [g for g in targets if addressed_requirements(tool, g)]
                requirements = set().union(*(addressed_requirements(tool, g) for g in targets))
                detail = RankingDetail(
                    candidate_id=c.candidate_id, priority=max(PRIORITY[g.priority] for g in targets),
                    novel_requirement_coverage=len(requirements - known), coverage=len(requirements),
                    hypothesis_discrimination=len(set().union(*(set(g.related_hypotheses) for g in targets)) & active),
                    economy=int(tool.estimated_cost_class == EstimateClass.LOW) + int(tool.estimated_latency_class == EstimateClass.LOW),
                    repeated_query_penalty=repeated_count(snapshot, c),
                )
            else:
                # Uncovered higher-priority gaps can justify deferral over lower
                # priority investigation. A useful safety call beats deferral for it.
                uncovered = [g for g in targets if g.gap_id not in call_coverage]
                detail = RankingDetail(candidate_id=c.candidate_id,
                    priority=max((PRIORITY[g.priority] for g in uncovered), default=0),
                    novel_requirement_coverage=0, coverage=0, hypothesis_discrimination=0,
                    economy=0, repeated_query_penalty=0)
            rows.append((p, detail))
        rows.sort(key=lambda row: (-row[1].priority, -row[1].novel_requirement_coverage,
                                  -row[1].coverage, -row[1].hypothesis_discrimination,
                                  -row[1].economy, row[1].repeated_query_penalty, row[1].candidate_id))
        return tuple(p for p, _ in rows), tuple(d for _, d in rows)
