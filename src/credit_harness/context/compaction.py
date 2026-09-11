from collections import defaultdict

from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.index import business_time
from .budget import digest
from .models import (
    FactCapsule, FactSource, FactSubject, HistoricalStateGroup, LookupScope,
    ReferenceRange, RepeatedLookupGroup,
)


def fact(e):
    return FactCapsule(
        claim_type=e.claim_type, value=e.value, subject=FactSubject(**e.subject.model_dump()),
        business_time=business_time(e), observed_at=e.observed_at, freshness=e.freshness,
        completeness=e.completeness, protocol_version=e.protocol_version, evidence_refs=(e.evidence_id,),
        source=FactSource(tool=e.tool, source_kind=e.source_kind, source_version=e.source_version, source_as_of=e.source_as_of),
    )


def reference_range(items):
    ordered = sorted(items, key=lambda e: (e.observed_at, e.evidence_id))
    return ReferenceRange(count=len(ordered), first_ref=ordered[0].evidence_id,
                          latest_ref=ordered[-1].evidence_id, range_digest=digest([e.evidence_id for e in ordered]))


class ContextCompactor:
    def lookup_groups(self, evidence):
        groups = defaultdict(list)
        for e in evidence:
            if e.claim_type == C.SOURCE_LOOKUP_STATUS:
                groups[(e.tool, e.metadata.scope.model_dump_json(), e.value)].append(e)
        result = []
        for _, items in sorted(groups.items()):
            first, last = min(items, key=lambda e: (e.observed_at, e.evidence_id)), max(items, key=lambda e: (e.observed_at, e.evidence_id))
            result.append(RepeatedLookupGroup(
                tool=last.tool, scope=LookupScope(**last.metadata.scope.model_dump()), status=last.value,
                first_observed_at=first.observed_at, last_observed_at=last.observed_at,
                latest_freshness=last.freshness, latest_completeness=last.completeness, references=reference_range(items),
            ))
        return tuple(result)

    def historical_groups(self, evidence, current_ids):
        groups = defaultdict(list)
        for e in evidence:
            if e.evidence_id not in current_ids and e.claim_type != C.SOURCE_LOOKUP_STATUS:
                groups[(e.claim_type, e.subject.model_dump_json(), e.protocol_version or "")].append(e)
        result = []
        for _, items in sorted(groups.items()):
            ordered = sorted(items, key=lambda e: (business_time(e), e.observed_at, e.evidence_id))
            first, last = ordered[0], ordered[-1]
            result.append(HistoricalStateGroup(
                claim_type=last.claim_type, subject=FactSubject(**last.subject.model_dump()), protocol_version=last.protocol_version,
                first_observed_value=first.value, last_observed_value=last.value,
                first_business_time=business_time(first), last_business_time=business_time(last), last_freshness=last.freshness,
                references=reference_range(items),
            ))
        return tuple(result)

    def relation_refs(self, refs, by_id):
        # Preserve all non-lookup refs; repeated absences retain only group boundaries.
        items = [by_id[ref] for ref in refs]
        kept = {e.evidence_id for e in items if e.claim_type != C.SOURCE_LOOKUP_STATUS}
        for group in self.lookup_groups(items):
            kept.update((group.references.first_ref, group.references.latest_ref))
        return tuple(sorted(kept))
