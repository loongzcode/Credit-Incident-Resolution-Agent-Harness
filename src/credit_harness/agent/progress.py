from credit_harness.context.models import ReasoningContextSnapshot
from credit_harness.hypotheses.models import HypothesisStatus
from .models import KnowledgeProgress, KnowledgeSummary, HypothesisKnowledge


def knowledge_summary(snapshot: ReasoningContextSnapshot) -> KnowledgeSummary:
    return KnowledgeSummary(identity=snapshot.financial_identity.result,
        hypotheses=tuple(HypothesisKnowledge(hypothesis_id=h.hypothesis_id, status=h.status,
            decisive_evidence_refs=h.decisive_evidence_refs) for h in sorted(
                (*snapshot.active_hypotheses, *snapshot.resolved_hypotheses_summary),
                key=lambda h: h.hypothesis_id)),
        open_gap_ids=tuple(sorted(g.gap_id for g in snapshot.open_evidence_gaps)))


def _hypotheses(summary):
    # NEW -> INVESTIGATING alone changes UNKNOWN to POSSIBLE, not knowledge.
    return tuple((h.hypothesis_id, "UNDECIDED" if h.status in (
        HypothesisStatus.UNKNOWN, HypothesisStatus.POSSIBLE) else h.status,
        h.decisive_evidence_refs) for h in summary.hypotheses)


def knowledge_progress(before: ReasoningContextSnapshot, after: ReasoningContextSnapshot) -> KnowledgeProgress:
    old, new = knowledge_summary(before), knowledge_summary(after)
    changes = dict(
        new_evidence=before.evidence_fingerprint != after.evidence_fingerprint,
        hypothesis_changed=_hypotheses(old) != _hypotheses(new),
        identity_changed=before.financial_identity != after.financial_identity,
        gap_changed=before.open_evidence_gaps != after.open_evidence_gaps,
        history_changed=(before.history_digest.repeated_lookup_groups, before.history_digest.state_transitions)
                        != (after.history_digest.repeated_lookup_groups, after.history_digest.state_transitions),
        facts_changed=before.current_facts != after.current_facts,
    )
    # Graph hashes also bind Case/update time/budget. Report that difference but
    # never treat it, history.tool_calls_used or snapshot_id alone as progress.
    return KnowledgeProgress(**changes, has_progress=any(changes.values()), before=old, after=new,
        hypothesis_graph_fingerprint_changed=before.hypothesis_graph_fingerprint != after.hypothesis_graph_fingerprint)
