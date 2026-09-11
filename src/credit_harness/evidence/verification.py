"""Read-only provenance revalidation for independent verification.

Raw observations remain inside the Evidence boundary. Re-extraction checks
stored facts; it never publishes facts or accepts caller-authored evidence.
"""
from dataclasses import dataclass
from sqlalchemy import select
from pydantic import ValidationError
from credit_harness.cases.repository import CallState, hydrate
from credit_harness.cases.tables import CaseCallRow
from credit_harness.persistence.store import ObservationRow
from .extractor import EvidenceExtractor
from .models import Evidence, fingerprint, json_hash
from .repository import ProvenanceError, raw_observation
from .tables import EvidenceRow, EvidenceOriginRow


@dataclass(frozen=True)
class VerifiedEvidenceRead:
    evidence: tuple[Evidence, ...]
    fingerprint: str
    provenance_valid: bool
    calls: tuple[dict, ...]


def read_verified_evidence(session, case_row):
    case = hydrate(case_row)
    rows = tuple(session.scalars(select(EvidenceRow).where(EvidenceRow.case_id == case.case_id)
                                 .order_by(EvidenceRow.evidence_id)))
    calls = tuple(session.scalars(select(CaseCallRow).where(CaseCallRow.case_id == case.case_id)
                                  .order_by(CaseCallRow.sequence)))
    origins = {(r.evidence_id, r.call_id) for r in session.scalars(select(EvidenceOriginRow)
               .join(CaseCallRow).where(CaseCallRow.case_id == case.case_id))}
    valid, receipts, extracted, originals = True, [], {}, {}
    for call in calls:
        if call.state != CallState.OBSERVED.value:
            continue
        row = session.get(ObservationRow, call.observation_id)
        try:
            if row is None:
                raise ProvenanceError("missing observation")
            receipts.append((row.id, row.content_hash, json_hash(row.observation), row.grant_hash,
                             row.simulation_id, row.tool, row.request, row.dispatch_correlation_id))
            raw = raw_observation(row)
            if (row.simulation_id != case.simulation_id or row.grant_hash != case_row.grant_hash
                    or row.tool != call.tool or row.request != call.request
                    or not call.dispatch_correlation_id
                    or row.dispatch_correlation_id != call.dispatch_correlation_id):
                raise ProvenanceError("dispatch mismatch")
            claims = EvidenceExtractor().extract(case, raw.observation, query=raw.request)
            if {(e.evidence_id, call.call_id) for e in claims} != {o for o in origins if o[1] == call.call_id}:
                raise ProvenanceError("origin coverage mismatch")
            for e in claims:
                originals[(e.evidence_id, e.observation_id)] = e
                old = extracted.get(e.evidence_id)
                if old is None or (e.observed_at, e.observation_id) > (old.observed_at, old.observation_id):
                    extracted[e.evidence_id] = e
        except (ProvenanceError, ValidationError, ValueError, TypeError):
            valid = False
    stored_ids = set()
    for row in rows:
        try:
            e = Evidence.model_validate(row.payload)
            if (e.evidence_id != row.evidence_id or e.case_id != row.case_id
                    or e.observation_id != row.observation_id or e.evidence_id != "E-" + fingerprint(e)
                    or originals.get((e.evidence_id, e.observation_id)) != e):
                raise ProvenanceError("stored fact differs from original extraction")
            stored_ids.add(e.evidence_id)
        except (ProvenanceError, ValidationError, ValueError, TypeError):
            valid = False
            extracted.pop(row.evidence_id, None)
    if set(extracted) != stored_ids:
        valid = False
    call_data = tuple(dict(call_id=c.call_id, sequence=c.sequence, tool=c.tool, request=c.request,
                          state=c.state, observation_id=c.observation_id,
                          dispatch_correlation_id=c.dispatch_correlation_id) for c in calls)
    return VerifiedEvidenceRead(tuple(extracted[k] for k in sorted(stored_ids & set(extracted))),
        json_hash(dict(evidence=[(r.evidence_id, r.observation_id, r.payload) for r in rows],
                       origins=sorted(origins), receipts=receipts)), valid, call_data)
