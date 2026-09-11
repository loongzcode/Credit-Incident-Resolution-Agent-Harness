from collections import defaultdict

from credit_harness.cases.models import Case, CaseStatus
from credit_harness.evidence.models import ClaimType as C, Evidence
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.hypotheses.models import HypothesisStatus as S, PriorityClass as P
from .budget import case_payload, digest, fits, referenced_ids, seal, MandatoryContextOverflow
from .compaction import ContextCompactor, fact
from .eligibility import ContextEligibilityError, ContextEligibilityPolicy, MandatoryContextFactPolicy
from .models import (
    COMPACTION_POLICY_VERSION, CONTEXT_POLICY_VERSION, CONTEXT_SCHEMA_VERSION, ELIGIBILITY_POLICY_VERSION,
    ContextBudget, GapCapsule, HistoryDigest, HypothesisCapsule, OmissionGroup, OmissionReason as O,
    OmittedEvidenceSummary, PaymentIdentityContext, ResolvedHypothesisSummary, RuntimeBudgetContext,
    SafetyContext, SafetyInvariant, TaskContext,
)
from .tool_capabilities import ToolCapabilityCatalog


class ReasoningContextAssembler:
    def __init__(self, *, budget: ContextBudget | None = None, eligibility: ContextEligibilityPolicy | None = None):
        self.budget = budget or ContextBudget()
        self.eligibility = eligibility or ContextEligibilityPolicy()
        if type(self.budget) is not ContextBudget or type(self.eligibility) is not ContextEligibilityPolicy:
            raise TypeError("only trusted typed context policy configuration is accepted")

    def build(self, case: Case, evidence: tuple[Evidence, ...] | list[Evidence]):
        # Neither prior context nor external graph is accepted. No persistence access.
        index = EvidenceIndex(case, evidence)
        graph = HypothesisEngine().evaluate(case, index.evidence)
        by_id = {e.evidence_id: e for e in index.evidence}
        eligible = tuple(e for e in index.evidence if self.eligibility.allows(e))
        eligible_ids = {e.evidence_id for e in eligible}
        all_current = {e.evidence_id: e for c in C for e in index.current(c)}
        current = {ref: e for ref, e in all_current.items() if ref in eligible_ids}
        compactor = ContextCompactor()
        definitions = {d.hypothesis_id: d for d in graph.definitions}
        critical_gaps = {g.gap_id for g in graph.open_gaps if g.priority_class == P.SAFETY_CRITICAL}
        critical_hypotheses = {h for g in graph.open_gaps if g.gap_id in critical_gaps for h in g.hypothesis_ids}
        identity = PaymentIdentityContext(
            result=graph.payment_identity.result, mismatch_dimensions=graph.payment_identity.mismatch_dimensions,
            unknown_dimensions=graph.payment_identity.unknown_dimensions,
            transaction_refs=tuple(sorted({w.transaction_ref for w in graph.payment_identity.witnesses})),
            evidence_refs=graph.payment_identity.evidence_refs, verification_version=graph.payment_identity.verification_version,
        )

        def hypothesis(state):
            definition = definitions[state.hypothesis_id]
            return HypothesisCapsule(
                hypothesis_id=state.hypothesis_id, kind=definition.kind, statement=definition.statement, status=state.status,
                decisive_evidence_refs=state.decisive_evidence_refs,
                supporting_evidence_refs=compactor.relation_refs(state.supporting_evidence_refs, by_id),
                contradicting_evidence_refs=compactor.relation_refs(state.contradicting_evidence_refs, by_id),
                supporting_ref_count=len(state.supporting_evidence_refs), contradicting_ref_count=len(state.contradicting_evidence_refs),
                relation_refs_digest=digest([list(state.supporting_evidence_refs), list(state.contradicting_evidence_refs)]),
                open_gap_ids=state.missing_evidence, reason=state.reason,
            )

        hypotheses = tuple(hypothesis(s) for s in graph.hypotheses if s.status != S.ELIMINATED)
        gaps = tuple(GapCapsule(
            gap_id=g.gap_id, question=g.question, required_claim_types=g.required_claim_types,
            priority=g.priority_class, status=g.status, related_hypotheses=g.hypothesis_ids, evidence_refs=g.evidence_refs,
        ) for g in graph.open_gaps)
        mandatory_hypotheses = tuple(h for h in hypotheses if h.status == S.CONFIRMED)
        mandatory_gaps = tuple(g for g in gaps if g.gap_id in critical_gaps)
        mandatory_refs = set(referenced_ids((identity, mandatory_hypotheses, mandatory_gaps)))
        mandatory_refs.update(e.evidence_id for e in all_current.values() if e.claim_type in MandatoryContextFactPolicy.claims)
        if not mandatory_refs <= eligible_ids:
            raise ContextEligibilityError("mandatory context depends on information ineligible for this boundary")
        mandatory_facts = tuple(fact(current[ref]) for ref in sorted(mandatory_refs) if ref in current)
        selected = {"current_facts": list(mandatory_facts), "active_hypotheses": list(mandatory_hypotheses),
                    "open_evidence_gaps": list(mandatory_gaps), "resolved_hypotheses_summary": [],
                    "lookups": [], "states": []}
        base = dict(
            case_id=case.case_id, internal_order_id=case.internal_order_id,
            context_schema_version=CONTEXT_SCHEMA_VERSION, eligibility_policy_version=ELIGIBILITY_POLICY_VERSION,
            compaction_policy_version=COMPACTION_POLICY_VERSION, context_policy_version=CONTEXT_POLICY_VERSION,
            hypothesis_rule_version=graph.rule_version, assembled_at=graph.evaluated_at,
            case_fingerprint=digest(case_payload(case)),
            evidence_fingerprint=digest([e.model_dump(mode="json") for e in index.evidence]),
            hypothesis_input_fingerprint=graph.input_fingerprint, hypothesis_graph_fingerprint=digest(graph.model_dump(mode="json")),
            policy_fingerprint=digest({"budget": self.budget.model_dump(mode="json"),
                                       "denied_claims": sorted(self.eligibility.denied_claims),
                                       "allowed_classes": sorted(self.eligibility.allowed_classes),
                                       "schema": CONTEXT_SCHEMA_VERSION, "eligibility": ELIGIBILITY_POLICY_VERSION,
                                       "compaction": COMPACTION_POLICY_VERSION, "context": CONTEXT_POLICY_VERSION}),
            task=TaskContext(**case.task_contract.model_dump()), financial_subject=case.financial_subject,
            financial_identity=identity,
            safety_constraints=SafetyContext(invariants=tuple(SafetyInvariant), forbidden_actions=case.constraints.forbidden_actions),
            budget=RuntimeBudgetContext(max_tool_calls=case.budget.max_tool_calls, used_tool_calls=case.budget.used_tool_calls,
                                        remaining_tool_calls=case.budget.max_tool_calls-case.budget.used_tool_calls,
                                        investigation_allowed=case.status in (CaseStatus.NEW, CaseStatus.INVESTIGATING)
                                        and case.budget.used_tool_calls < case.budget.max_tool_calls),
            available_tools=ToolCapabilityCatalog().for_case(case),
        )
        reasons = {}
        for e in index.evidence:
            reasons[e.evidence_id] = (O.ELIGIBILITY_DENIED if e.evidence_id not in eligible_ids else
                                     O.REPEATED_LOOKUP if e.claim_type == C.SOURCE_LOOKUP_STATUS else
                                     O.HISTORICAL_SUPERSEDED if e.evidence_id not in current else
                                     O.IRRELEVANT_TO_ACTIVE_HYPOTHESES)

        def render():
            payload = {**base, **{k: tuple(v) for k, v in selected.items() if k not in ("lookups", "states")},
                       "history_digest": HistoryDigest(
                           tool_calls_used=case.budget.used_tool_calls, evidence_count=len(index.evidence),
                           latest_observation_time=max((e.observed_at for e in index.evidence), default=None),
                           repeated_lookup_groups=tuple(selected["lookups"]), state_transitions=tuple(selected["states"])),
                       }
            refs = referenced_ids(payload)
            omitted = defaultdict(list)
            for ref in sorted(set(by_id) - set(refs)):
                omitted[reasons[ref]].append(ref)
            payload.update(selected_evidence_refs=refs, omitted_evidence_summary=OmittedEvidenceSummary(
                total_evidence=len(index.evidence), selected=len(refs),
                omitted=tuple(OmissionGroup(reason=reason, count=len(items), evidence_ids_digest=digest(items))
                              for reason, items in sorted(omitted.items())),
            ))
            return seal(payload, self.budget)

        snapshot = render()
        if not fits(snapshot):
            raise MandatoryContextOverflow("Tier 0 exceeds context budget; no snapshot produced")
        candidates = []
        for h in hypotheses:
            if h.status != S.CONFIRMED:
                rank = 1 if h.status == S.SUPPORTED else 2
                candidates.append((rank, 0 if h.hypothesis_id in critical_hypotheses else 1,
                                   h.hypothesis_id.value, "active_hypotheses", h))
        for g in gaps:
            if g.gap_id not in critical_gaps:
                candidates.append((1 if g.priority == P.DISCRIMINATING else 2, 0, g.gap_id, "open_evidence_gaps", g))
        active_claims = {c for d in graph.definitions if d.hypothesis_id not in graph.eliminated for c in d.relevant_claim_types}
        active_claims.update(c for g in graph.open_gaps for c in g.required_claim_types if isinstance(c, C))
        support_ids = {ref for h in hypotheses for ref in h.supporting_evidence_refs}
        for e in current.values():
            if e.claim_type in (C.PROTOCOL_FIELD_TYPE, C.PROTOCOL_BUSINESS_SEMANTICS):
                expected_field = "loanNo" if e.claim_type == C.PROTOCOL_FIELD_TYPE else "SUCCESS"
                if e.subject.field != expected_field and e.evidence_id not in support_ids:
                    continue
            if e.evidence_id not in mandatory_refs and (e.claim_type in active_claims or e.evidence_id in support_ids):
                candidates.append((1 if e.evidence_id in support_ids else 2, 2, e.evidence_id, "current_facts", fact(e)))
        for s in graph.hypotheses:
            if s.status == S.ELIMINATED:
                candidates.append((3, 0, s.hypothesis_id.value, "resolved_hypotheses_summary", ResolvedHypothesisSummary(
                    hypothesis_id=s.hypothesis_id, statement=definitions[s.hypothesis_id].statement,
                    status=s.status, decisive_evidence_refs=s.decisive_evidence_refs)))
        for i, group in enumerate(compactor.lookup_groups(eligible)):
            candidates.append((3, 1, str(i), "lookups", group))
        for i, group in enumerate(compactor.historical_groups(eligible, current)):
            candidates.append((3, 2, str(i), "states", group))
        accepted = []
        for _, _, _, slot, item in sorted(candidates, key=lambda c: c[:4]):
            refs = set(referenced_ids(item))
            if not refs <= eligible_ids:
                continue  # omit whole optional capsule, never emit a misleading partial interpretation
            selected[slot].append(item)
            candidate = render()
            if fits(candidate):
                snapshot = candidate
                accepted.append((slot, item))
            else:
                selected[slot].pop()
                for ref in refs - set(snapshot.selected_evidence_refs):
                    reasons[ref] = O.SIZE_BUDGET
        # Omission reason labels also consume space. Remove optional entries only if
        # their audit bookkeeping pushed the final envelope over the character bound.
        snapshot = render()
        while not fits(snapshot) and accepted:
            slot, item = accepted.pop()
            selected[slot].remove(item)
            for ref in referenced_ids(item):
                reasons[ref] = O.SIZE_BUDGET
            snapshot = render()
        if not fits(snapshot):
            raise MandatoryContextOverflow("context audit envelope exceeds budget; no partial snapshot produced")
        from .invariants import ReasoningContextInvariantValidator
        ReasoningContextInvariantValidator().validate(snapshot, index, graph, mandatory_refs, self.eligibility)
        return snapshot
