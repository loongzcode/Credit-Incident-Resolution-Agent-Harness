from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType

from credit_harness.cases.models import Case
from credit_harness.domain.enums import Completeness, Freshness, ToolName
from credit_harness.evidence.models import ClaimType as C, Evidence


def dimension(e):
    return (e.subject.kind, e.subject.identifier, e.subject.internal_order_id, e.subject.field,
            e.protocol_version if e.tool == ToolName.PROTOCOL else None)


def business_time(e):
    return e.event_time or e.source_as_of or e.observed_at


@dataclass(frozen=True, init=False)
class EvidenceIndex:
    case: Case
    evidence: tuple[Evidence, ...]
    _by_claim: object

    def __init__(self, case: Case, evidence: tuple[Evidence, ...] | list[Evidence]):
        if type(case) is not Case or not isinstance(evidence, (tuple, list)):
            raise TypeError("expected Case and a list/tuple of Evidence")
        by_id = {}
        for e in evidence:
            if type(e) is not Evidence:
                raise TypeError("only Evidence instances are accepted")
            if (e.case_id != case.case_id or e.subject.internal_order_id != case.internal_order_id
                    or e.metadata.scope.internal_order_id != case.internal_order_id
                    or e.tool not in case.scope.allowed_tools):
                raise ValueError("evidence outside current case/order/tool scope")
            if e.evidence_id in by_id and by_id[e.evidence_id] != e:
                raise ValueError("same evidence ID has conflicting contents")
            if e.raw_ref != f"observation://{e.observation_id}":
                raise ValueError("invalid evidence provenance reference")
            by_id[e.evidence_id] = e
        items = tuple(sorted(by_id.values(), key=lambda e: e.evidence_id))
        groups = defaultdict(list)
        for e in items:
            groups[e.claim_type].append(e)
        object.__setattr__(self, "case", case)
        object.__setattr__(self, "evidence", items)
        object.__setattr__(self, "_by_claim", MappingProxyType({k: tuple(v) for k, v in groups.items()}))

    def all(self, claim_type: C) -> tuple[Evidence, ...]:
        return self._by_claim.get(claim_type, ())

    def latest(self, claim_type: C, *, candidates=None) -> tuple[Evidence, ...]:
        """Latest business time per full subject; equal-time disagreements survive."""
        groups = defaultdict(list)
        for e in self.all(claim_type) if candidates is None else candidates:
            groups[dimension(e)].append(e)
        return tuple(sorted((e for group in groups.values() for e in group
                             if business_time(e) == max(map(business_time, group))), key=lambda e: e.evidence_id))

    def current(self, claim_type: C) -> tuple[Evidence, ...]:
        lookups = self.all(C.SOURCE_LOOKUP_STATUS)
        eligible = [e for e in self.all(claim_type)
                    if e.freshness == Freshness.CURRENT and e.completeness == Completeness.COMPLETE
                    and e.source_as_of is not None
                    and not any(l.tool == e.tool and l.metadata.scope == e.metadata.scope
                                and l.observed_at >= e.observed_at for l in lookups)]
        return self.latest(claim_type, candidates=eligible)

    def agreed(self, claim_type: C, value) -> tuple[Evidence, ...]:
        """Conflicting current values never become decisive by arbitrary tie-break."""
        current = self.current(claim_type)
        return tuple(e for e in current if type(e.value) is type(value) and e.value == value
                     and all((type(other.value), other.value) == (type(value), value)
                             for other in current if dimension(other) == dimension(e)))

    def has_value(self, claim_type: C, value) -> bool:
        return bool(self.agreed(claim_type, value))

    @staticmethod
    def refs(items) -> tuple[str, ...]:
        return tuple(sorted({e.evidence_id for e in items}))

    @property
    def evaluated_at(self) -> datetime:
        # A logical evaluation watermark makes the entire projection reproducible.
        return max((e.observed_at for e in self.evidence), default=self.case.created_at).astimezone(timezone.utc)

    @property
    def request_id(self) -> str | None:
        anchors = {e.metadata.fund_request_id for c in (C.REQUEST_SENT, C.HTTP_RESPONSE_STATUS, C.GUARANTEE_STATUS)
                   for e in self.current(c) if e.metadata.fund_request_id}
        if not anchors:
            anchors = {e.subject.identifier for c in (C.FUND_BUSINESS_STATUS, C.PAYMENT_FINALITY)
                       for e in self.current(c) if e.subject.kind.value == "FUND_REQUEST"}
        return next(iter(anchors)) if len(anchors) == 1 else None

    def for_request(self, items):
        return tuple(e for e in items if self.request_id is not None
                     and (e.metadata.fund_request_id or e.subject.identifier) == self.request_id)
