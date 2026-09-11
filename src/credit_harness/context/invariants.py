from credit_harness.hypotheses.models import HypothesisStatus as S, PriorityClass as P
from .budget import case_payload, digest, fits, referenced_ids, snapshot_digest
from .compaction import fact
from .eligibility import MandatoryContextFactPolicy
from .models import ReasoningContextSnapshot, GapCapsule, SafetyInvariant
from .identity_projection import IdentityContextProjector, identity_auxiliary_refs
from .envelope import ContextEnvelopeInvariantValidator


class ReasoningContextInvariantError(ValueError):
    pass


class ReasoningContextInvariantValidator:
    def validate(self, snapshot, index, graph, mandatory_refs, eligibility):
        def require(condition, message):
            if not condition:
                raise ReasoningContextInvariantError(message)
        require(type(snapshot) is ReasoningContextSnapshot, "invalid snapshot type")
        ContextEnvelopeInvariantValidator().validate(snapshot)
        ids = {e.evidence_id for e in index.evidence}
        selected = set(snapshot.selected_evidence_refs)
        require(selected <= ids, "foreign evidence reference")
        require(set(mandatory_refs) <= selected, "Tier 0 omitted")
        require(all(eligibility.allows(e) for e in index.evidence if e.evidence_id in selected), "ineligible selected input")
        require(selected == set(referenced_ids(snapshot)), "dangling selected refs")
        expected_gaps = {g.gap_id for g in graph.open_gaps if g.priority_class == P.SAFETY_CRITICAL}
        require(expected_gaps <= {g.gap_id for g in snapshot.open_evidence_gaps}, "missing safety gap")
        for capsule in snapshot.open_evidence_gaps:
            expected = next((g for g in graph.open_gaps if g.gap_id == capsule.gap_id), None)
            require(expected is not None and capsule == GapCapsule(
                gap_id=expected.gap_id, question=expected.question, required_claim_types=expected.required_claim_types,
                priority=expected.priority_class, status=expected.status, related_hypotheses=expected.hypothesis_ids,
                evidence_refs=expected.evidence_refs), "gap projection changed")
        states = {h.hypothesis_id: h for h in snapshot.active_hypotheses}
        for h in graph.hypotheses:
            if h.status == S.CONFIRMED:
                require(h.hypothesis_id in states and states[h.hypothesis_id].status == S.CONFIRMED
                        and bool(h.decisive_evidence_refs)
                        and states[h.hypothesis_id].decisive_evidence_refs == h.decisive_evidence_refs,
                        "confirmed hypothesis lost its witness")
        identity = snapshot.financial_identity
        require(identity == IdentityContextProjector().project(
            graph.payment_identity, index,
            preview_limit=snapshot.context_budget_usage.limits.max_identity_transaction_preview), "identity changed")
        for capsule in snapshot.active_hypotheses:
            state = next(h for h in graph.hypotheses if h.hypothesis_id == capsule.hypothesis_id)
            for relation in ("supporting", "contradicting"):
                refs = getattr(state, f"{relation}_evidence_refs")
                preview = getattr(capsule, f"{relation}_ref_preview")
                require(getattr(capsule, f"{relation}_ref_count") == len(refs)
                        and getattr(capsule, f"{relation}_refs_digest") == digest(sorted(refs))
                        and set(preview) <= set(refs)
                        and len(preview) <= snapshot.context_budget_usage.limits.max_relation_ref_preview,
                        "relation compaction changed")
        current = {e.evidence_id: e for c in {e.claim_type for e in index.evidence} for e in index.current(c)}
        auxiliary = identity_auxiliary_refs(graph.payment_identity, identity)
        critical_fact_ids = {ref for ref, e in current.items() if e.claim_type in MandatoryContextFactPolicy.claims and ref not in auxiliary}
        critical_fact_ids.update(ref for ref in identity.evidence_refs if ref in current)
        critical_fact_ids.update(ref for h in graph.hypotheses if h.status == S.CONFIRMED
                                 for ref in h.decisive_evidence_refs if ref in current)
        require(critical_fact_ids <= {ref for capsule in snapshot.current_facts for ref in capsule.evidence_refs}, "critical fact capsule omitted")
        for capsule in snapshot.current_facts:
            require(len(capsule.evidence_refs) == 1 and capsule.evidence_refs[0] in current, "non-current fact")
            require(capsule == fact(current[capsule.evidence_refs[0]]), "fact projection changed")
        summary = snapshot.omitted_evidence_summary
        require(summary.total_evidence == len(ids) and summary.selected == len(selected)
                and sum(g.count for g in summary.omitted) + len(selected) == len(ids), "omission accounting mismatch")
        require(snapshot.hypothesis_input_fingerprint == graph.input_fingerprint
                and snapshot.hypothesis_graph_fingerprint == digest(graph.model_dump(mode="json")), "stale graph")
        require(snapshot.case_id == index.case.case_id and snapshot.internal_order_id == index.case.internal_order_id
                and snapshot.case_fingerprint == digest(case_payload(index.case))
                and snapshot.evidence_fingerprint == digest([e.model_dump(mode="json") for e in index.evidence]), "input fingerprint mismatch")
        require(snapshot.task.model_dump() == index.case.task_contract.model_dump()
                and snapshot.financial_subject == index.case.financial_subject
                and snapshot.safety_constraints.invariants == tuple(SafetyInvariant)
                and snapshot.safety_constraints.forbidden_actions == index.case.constraints.forbidden_actions,
                "mandatory task/safety contract changed")
        usage = snapshot.context_budget_usage
        require(usage.fact_capsules == len(snapshot.current_facts)
                and usage.hypothesis_capsules == len(snapshot.active_hypotheses) + len(snapshot.resolved_hypotheses_summary)
                and usage.gap_capsules == len(snapshot.open_evidence_gaps)
                and usage.history_items == len(snapshot.history_digest.repeated_lookup_groups) + len(snapshot.history_digest.state_transitions), "invalid capsule counts")
        require(snapshot.snapshot_id == snapshot_digest(snapshot), "snapshot fingerprint mismatch")
        require(snapshot.context_budget_usage.serialized_chars == len(snapshot.model_dump_json())
                and fits(snapshot), "budget mismatch")
