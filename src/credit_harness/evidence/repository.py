from sqlalchemy import select, update
from sqlalchemy.orm import Session

from credit_harness.cases.models import CaseAccessError
from credit_harness.cases.repository import CallState, CaseRepository, hydrate
from credit_harness.cases.tables import CaseCallRow, CaseRow
from credit_harness.persistence.store import ObservationRow
from credit_harness.tools.contracts import Observation, ToolQuery
from .extractor import EvidenceExtractor
from .models import Evidence, RawObservation, json_hash
from .tables import EvidenceOriginRow, EvidenceRow


class ProvenanceError(Exception):
    pass


def raw_observation(row: ObservationRow) -> RawObservation:
    if row.content_hash != json_hash(row.observation):
        raise ProvenanceError("observation content hash mismatch")
    observation = Observation.model_validate(row.observation)
    query = ToolQuery.model_validate(row.request)
    if (observation.observation_id != row.id or observation.tool.value != row.tool
            or query.internal_order_id != observation.internal_order_id):
        raise ProvenanceError("observation envelope mismatch")
    return RawObservation(observation_id=row.id, tool=observation.tool, request=query,
                          content_hash=row.content_hash, observation=observation)


class EvidenceRepository:
    """Trusted write path accepts a persisted receipt, never caller-authored Evidence."""

    def __init__(self, cases: CaseRepository, *, extractor: EvidenceExtractor | None = None):
        self.cases = cases
        self.engine = cases.engine
        self.extractor = extractor or EvidenceExtractor()

    def record_call(self, case_id: str, call_id: str, observation: Observation) -> tuple[str, ...]:
        with Session(self.engine) as session, session.begin():
            # Serialize claim insertion per case, portable to SQLite and PostgreSQL.
            result = session.execute(update(CaseRow).where(
                CaseRow.case_id == case_id, CaseRow.tenant_id == self.cases.tenant_id,
            ).values(updated_at=CaseRow.updated_at))
            if result.rowcount != 1:
                raise CaseAccessError("case unavailable")
            case_row = self.cases._row(session, case_id)
            call = session.get(CaseCallRow, call_id)
            if call is None or call.case_id != case_id or call.state != CallState.DISPATCHED.value:
                raise ProvenanceError("missing pending case dispatch")
            row = session.get(ObservationRow, observation.observation_id)
            if row is None:
                raise ProvenanceError("observation must be persisted before evidence")
            raw = raw_observation(row)
            if (row.simulation_id != case_row.simulation_id or row.grant_hash != case_row.grant_hash
                    or row.tool != call.tool or row.request != call.request
                    or raw.observation != observation):
                raise ProvenanceError("observation does not match case, credential or dispatch")
            # A returned observation may not be replayed as a new dispatch (even in
            # another case bound to the same upstream grant).
            if session.scalar(select(CaseCallRow.call_id).where(
                CaseCallRow.observation_id == row.id,
            )) is not None:
                raise ProvenanceError("observation already assigned to a case dispatch")
            if (not call.dispatch_correlation_id or not row.dispatch_correlation_id
                    or call.dispatch_correlation_id != row.dispatch_correlation_id):
                raise ProvenanceError("missing or mismatched dispatch correlation")
            case = hydrate(case_row)
            evidence = self.extractor.extract(case, raw.observation, query=raw.request)
            call.observation_id = row.id
            call.state = CallState.OBSERVED.value
            session.flush()
            for item in evidence:
                if session.get(EvidenceRow, item.evidence_id) is None:
                    session.add(EvidenceRow(evidence_id=item.evidence_id, case_id=case_id,
                                            observation_id=row.id, payload=item.model_dump(mode="json")))
                    session.flush()
                session.add(EvidenceOriginRow(evidence_id=item.evidence_id, call_id=call_id))
            return tuple(item.evidence_id for item in evidence)

    def list(self, case_id: str) -> tuple[Evidence, ...]:
        with Session(self.engine) as session:
            self.cases._row(session, case_id)
            items = [Evidence.model_validate(row.payload) for row in session.scalars(
                select(EvidenceRow).where(EvidenceRow.case_id == case_id),
            )]
            return tuple(sorted(items, key=lambda e: (e.observed_at, e.claim_type.value, e.evidence_id)))

    def _evidence(self, session, case_id, evidence_id):
        self.cases._row(session, case_id)
        row = session.scalar(select(EvidenceRow).where(
            EvidenceRow.case_id == case_id, EvidenceRow.evidence_id == evidence_id,
        ))
        if row is None:
            raise CaseAccessError("evidence unavailable")
        return row

    def get_raw_observation(self, evidence_id: str, *, case_id: str) -> RawObservation:
        """case_id is mandatory: knowledge of an evidence ID never grants access."""
        with Session(self.engine) as session:
            item = self._evidence(session, case_id, evidence_id)
            origins = session.scalar(select(CaseCallRow).join(
                EvidenceOriginRow, EvidenceOriginRow.call_id == CaseCallRow.call_id,
            ).where(EvidenceOriginRow.evidence_id == evidence_id,
                    CaseCallRow.case_id == case_id, CaseCallRow.observation_id == item.observation_id))
            if origins is None:
                raise ProvenanceError("missing case origin")
            raw = raw_observation(session.get(ObservationRow, item.observation_id))
            evidence = Evidence.model_validate(item.payload)
            if evidence.content_hash != raw.content_hash or raw.request.model_dump(mode="json") != origins.request:
                raise ProvenanceError("evidence provenance mismatch")
            return raw

    def observations(self, case_id: str) -> tuple[RawObservation, ...]:
        with Session(self.engine) as session:
            self.cases._row(session, case_id)
            rows = session.scalars(select(ObservationRow).join(
                CaseCallRow, CaseCallRow.observation_id == ObservationRow.id,
            ).where(CaseCallRow.case_id == case_id).order_by(CaseCallRow.sequence))
            return tuple(raw_observation(row) for row in rows)

    def origin_observation_ids(self, case_id: str, evidence_id: str) -> tuple[str, ...]:
        with Session(self.engine) as session:
            self._evidence(session, case_id, evidence_id)
            return tuple(session.scalars(select(CaseCallRow.observation_id).join(
                EvidenceOriginRow, EvidenceOriginRow.call_id == CaseCallRow.call_id,
            ).where(EvidenceOriginRow.evidence_id == evidence_id, CaseCallRow.case_id == case_id)
                .order_by(CaseCallRow.sequence)))
