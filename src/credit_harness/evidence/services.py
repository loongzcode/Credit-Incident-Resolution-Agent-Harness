from datetime import datetime, timedelta
from itertools import combinations
from typing import Protocol

from credit_harness.cases.repository import utc_now
from credit_harness.domain.enums import Completeness, Freshness
from .models import ClaimType, Evidence, EvidenceConflict, SubjectKind, json_hash


class FreshnessPolicy(Protocol):
    def is_stale(self, evidence: Evidence) -> bool: ...


class EvidenceFreshnessService:
    def is_stale(self, evidence: Evidence) -> bool:
        """UNKNOWN is not known stale, and also does not mean current."""
        return evidence.freshness == Freshness.STALE

    def age(self, evidence: Evidence, now: datetime) -> timedelta | None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone aware")
        if evidence.source_as_of is None:
            return None
        if now < evidence.source_as_of:
            raise ValueError("now precedes source_as_of; use the same clock domain")
        return now - evidence.source_as_of


class EvidenceConflictDetector:
    RULES = frozenset({ClaimType.PAYMENT_AMOUNT, ClaimType.PAYMENT_CURRENCY,
                       ClaimType.TRANSACTION_FUND_REQUEST_ID})

    def detect(self, case_id: str, evidence: tuple[Evidence, ...], *, now=None) -> tuple[EvidenceConflict, ...]:
        if any(e.case_id != case_id for e in evidence):
            raise ValueError("cannot compare evidence across cases")
        eligible = [e for e in evidence if e.claim_type in self.RULES
                    and e.subject.kind == SubjectKind.TRANSACTION and e.event_time is not None
                    and e.freshness == Freshness.CURRENT and e.completeness == Completeness.COMPLETE]
        result = []
        for a, b in combinations(eligible, 2):
            # Exact business instant only: interval inference is deliberately absent.
            # Two observable source identities (tool + source kind) are required.
            # Re-querying one endpoint does not create an independent source.
            if (a.claim_type != b.claim_type or a.subject != b.subject
                    or a.event_time != b.event_time or a.value == b.value
                    or a.observation_id == b.observation_id
                    or (a.tool, a.source_kind) == (b.tool, b.source_kind)):
                continue
            refs = tuple(sorted((a.evidence_id, b.evidence_id)))
            result.append(EvidenceConflict(
                conflict_id="CF-" + json_hash([case_id, a.claim_type.value, refs]),
                case_id=case_id, claim_type=a.claim_type, evidence_refs=refs,
                reason="同一交易、同一业务时刻的完整当前观测存在互斥值；需调查，不能据此选定真实值。",
                detected_at=now or utc_now(),
            ))
        return tuple(sorted(result, key=lambda c: c.conflict_id))
