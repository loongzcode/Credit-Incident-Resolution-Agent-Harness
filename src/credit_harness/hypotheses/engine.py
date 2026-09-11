import hashlib
import json

from credit_harness.cases.models import Case
from credit_harness.evidence.models import Evidence
from .catalog import CATALOG, HYPOTHESIS_RULESET_VERSION
from .gaps import derive_gaps
from .index import EvidenceIndex
from .models import GapStatus, HypothesisGraphView, HypothesisState, HypothesisStatus as S, RelationKind as R
from .rules import evaluate_rules


class HypothesisEngine:
    """No collaborators, I/O or mutable previous state; Case + Evidence only."""

    def evaluate(self, case: Case, evidence: tuple[Evidence, ...] | list[Evidence]) -> HypothesisGraphView:
        index = EvidenceIndex(case, evidence)
        results = evaluate_rules(index)
        gaps = derive_gaps(index, results)
        states = []
        for result in results:
            def refs(*kinds):
                return tuple(sorted(r.evidence_id for r in result.relations if r.relation in kinds))
            states.append(HypothesisState(
                hypothesis_id=result.hypothesis_id, status=result.status,
                supporting_evidence_refs=refs(R.SUPPORTS, R.DECISIVE_SUPPORT),
                contradicting_evidence_refs=refs(R.CONTRADICTS, R.DECISIVE_CONTRADICTION),
                decisive_evidence_refs=refs(R.DECISIVE_SUPPORT, R.DECISIVE_CONTRADICTION),
                missing_evidence=tuple(g.gap_id for g in gaps if g.status == GapStatus.OPEN
                                       and result.hypothesis_id in g.hypothesis_ids
                                       and result.status not in (S.CONFIRMED, S.ELIMINATED)),
                reason=result.reason, rule_version=HYPOTHESIS_RULESET_VERSION, evaluated_at=index.evaluated_at,
            ))
        payload = case.model_dump(mode="json")
        payload["scope"] = {"allowed_order_ids": sorted(case.scope.allowed_order_ids),
                            "allowed_tools": sorted(t.value for t in case.scope.allowed_tools)}
        fingerprint = hashlib.sha256(json.dumps({
            "case": payload, "evidence": [e.model_dump(mode="json") for e in index.evidence],
            "rule_version": HYPOTHESIS_RULESET_VERSION,
        }, sort_keys=True).encode()).hexdigest()
        return HypothesisGraphView(
            case_id=case.case_id, rule_version=HYPOTHESIS_RULESET_VERSION, evaluated_at=index.evaluated_at,
            input_fingerprint=fingerprint, definitions=CATALOG, hypotheses=tuple(states),
            relations=tuple(r for result in results for r in result.relations), gaps=gaps,
            open_gaps=tuple(g for g in gaps if g.status == GapStatus.OPEN),
            confirmed=tuple(s.hypothesis_id for s in states if s.status == S.CONFIRMED),
            supported=tuple(s.hypothesis_id for s in states if s.status == S.SUPPORTED),
            eliminated=tuple(s.hypothesis_id for s in states if s.status == S.ELIMINATED),
        )
