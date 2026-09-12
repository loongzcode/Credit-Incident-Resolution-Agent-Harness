"""Recheck durable Observation -> CaseCall -> Evidence -> registered source, not text."""
from sqlalchemy import select
from pydantic import ValidationError
from credit_harness.cases.repository import CallState
from credit_harness.cases.tables import CaseCallRow
from credit_harness.evidence.models import Evidence, ClaimType as C, SubjectKind
from credit_harness.evidence.tables import EvidenceRow, EvidenceOriginRow
from credit_harness.evidence.repository import raw_observation, ProvenanceError
from credit_harness.evidence.extractor import EvidenceExtractor
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.persistence.store import ObservationRow
from credit_harness.domain.enums import ToolName, Freshness, Completeness, ObservationStatus
from credit_harness.tools.contracts import ProtocolData
from .tables import DispatchSourceRow
from .models import DispatchSource, RegistryError, ResolutionCode as Code, CapabilityType, SystemType, AuthorityLevel
from .revisions import revision_chain


def verified_protocol(session, case, revision, refs, resolver, now):
    try:
        return _verify(session, case, revision, refs, resolver, now)
    except RegistryError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, ProvenanceError, ValidationError):
        raise RegistryError(Code.INVALID_ROUTING_PROOF) from None


def _verify(session, case, revision, refs, resolver, now):
    def require(condition):
        if not condition:
            raise RegistryError(Code.INVALID_ROUTING_PROOF)
    rows = session.scalars(select(EvidenceRow).where(EvidenceRow.case_id == case.case_id)).all()
    evidence = {row.evidence_id: Evidence.model_validate(row.payload) for row in rows}
    current_ids = {e.evidence_id for e in EvidenceIndex(case, tuple(evidence.values())).current(C.PROTOCOL_FIELD_TYPE)}
    require(set(refs) <= current_ids)
    require(all(evidence[r].evidence_id == r for r in refs))
    require(all(row.observation_id == evidence[row.evidence_id].observation_id for row in rows if row.evidence_id in refs))
    source = resolver.resolve_tool(case, ToolName.PROTOCOL, revision.route_context, session=session)
    _, definition = resolver.repository.current(session)
    capability = next(c for c in definition.capabilities if c.capability_id == source.capability_id)
    system = next(s for s in definition.systems if s.system_id == source.system_id)
    require(capability.capability_type == CapabilityType.READ_PROTOCOL_SCHEMA and system.system_type == SystemType.PROTOCOL_REGISTRY)
    require(any(a.claim_type == C.PROTOCOL_FIELD_TYPE and a.authority_level == AuthorityLevel.AUTHORITATIVE
                for a in source.authority))
    fingerprints = {r.routing_fingerprint for r in revision_chain(session, revision)}
    from credit_harness.cases.tables import CaseRow
    case_row = session.get(CaseRow, case.case_id)
    versions, observations = set(), set()
    for ref in sorted(set(refs)):
        e = evidence[ref]
        require(e.tool == ToolName.PROTOCOL and e.subject.kind == SubjectKind.PROTOCOL
                and e.subject.internal_order_id == case.internal_order_id
                and e.freshness == Freshness.CURRENT and e.completeness == Completeness.COMPLETE
                and e.source_as_of is not None and e.observed_at <= now)
        calls = session.scalars(select(CaseCallRow).join(EvidenceOriginRow,
            EvidenceOriginRow.call_id == CaseCallRow.call_id).where(EvidenceOriginRow.evidence_id == ref,
                CaseCallRow.case_id == case.case_id, CaseCallRow.observation_id == e.observation_id)).all()
        require(len(calls) == 1)
        call = calls[0]
        row = session.get(ObservationRow, e.observation_id)
        raw = raw_observation(row)
        require(call.state == CallState.OBSERVED.value and row.simulation_id == case_row.simulation_id
                and row.grant_hash == case_row.grant_hash and call.tool == row.tool == ToolName.PROTOCOL.value
                and call.request == row.request and bool(call.dispatch_correlation_id)
                and call.dispatch_correlation_id == row.dispatch_correlation_id
                and raw.content_hash == e.content_hash and raw.request.internal_order_id == case.internal_order_id)
        o = raw.observation
        require(o.status == ObservationStatus.OK and type(o.data) is ProtocolData
                and e in EvidenceExtractor().extract(case, o, query=raw.request))
        require(e.protocol_version == o.data.record.protocol_version
                and e.subject.identifier == f"{o.data.record.partner}@{e.protocol_version}"
                and (raw.request.protocol_version is None or raw.request.protocol_version == e.protocol_version))
        origin = session.get(DispatchSourceRow, call.call_id)
        require(origin is not None and origin.case_id == case.case_id and origin.tenant_id == case.tenant_id)
        bound = DispatchSource.model_validate(origin.payload)
        require(bound.call_id == call.call_id and bound.case_id == case.case_id
                and bound.resolved.case_id == case.case_id and bound.resolved.routing_fingerprint in fingerprints
                and bound.resolved.model_dump(exclude={"routing_fingerprint"}) == source.model_dump(exclude={"routing_fingerprint"}))
        versions.add(e.protocol_version)
        observations.add(e.observation_id)
    if len(versions) != 1:
        raise RegistryError(Code.ROUTING_FACT_CONFLICT)
    require(len(observations) == 1)  # no cross-observation protocol proof assembly
    return next(iter(versions))
