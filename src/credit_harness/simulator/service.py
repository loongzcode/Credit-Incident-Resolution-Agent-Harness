import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from credit_harness.domain.enums import (
    Completeness, FaultKind, Freshness, KnowledgeStatus, ObservationStatus,
    SourceKind, ToolName,
)
from credit_harness.persistence.store import (
    FaultRow, GrantRow, ObservationRow, SimulationRow, snapshots, token_hash,
)
from credit_harness.tools.contracts import DispatchCorrelationId, Observation, ToolQuery
from .faults import ObservationFault
from .projections import project


class AuthenticationError(Exception):
    pass


class ScopeError(Exception):
    pass


class QueryError(Exception):
    pass


class ObservationService:
    """Trusted service; only returned Observation crosses the process boundary."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def observe(self, token: str, tool: ToolName, query: ToolQuery, *,
                dispatch_correlation_id: DispatchCorrelationId | None = None) -> Observation:
        if dispatch_correlation_id is not None:
            dispatch_correlation_id = TypeAdapter(DispatchCorrelationId).validate_python(dispatch_correlation_id)
        with Session(self._engine) as session, session.begin():
            digest = token_hash(token)
            grant = session.get(GrantRow, digest)
            if grant is None:
                raise AuthenticationError("invalid tool credential")
            simulation = session.get(SimulationRow, grant.simulation_id)
            if simulation is None:
                raise AuthenticationError("invalid tool credential")
            now = datetime.fromisoformat(simulation.clock)
            if now >= datetime.fromisoformat(grant.expires_at):
                raise AuthenticationError("expired tool credential")
            if tool.value not in grant.allowed_tools or query.internal_order_id != grant.order_id:
                # Check scope BEFORE reading any world snapshots or faults.
                raise ScopeError("tool or order outside granted scope")
            if tool != ToolName.PROTOCOL and (query.protocol_version or query.effective_at):
                raise QueryError("protocol parameters only apply to get_protocol")
            if query.effective_at is not None and query.effective_at > now:
                raise QueryError("future queries are not permitted")
            history = [(revision, world) for revision, world in snapshots(session, simulation.id)
                       if world.event_time <= now]
            if not history:
                raise QueryError("simulation has no visible history")
            policies = [ObservationFault.model_validate(row.policy) for row in session.scalars(
                select(FaultRow).where(FaultRow.simulation_id == simulation.id),
            )]
            active = [fault for fault in policies if fault.tool == tool and fault.active(now)]
            status = ObservationStatus.OK
            source = SourceKind.PRIMARY
            completeness = Completeness.COMPLETE
            freshness = Freshness.CURRENT
            source_as_of = now
            selected = history[-1][1]
            data = None
            event_time = None
            if any(f.kind == FaultKind.TIMEOUT for f in active):
                status = ObservationStatus.TIMEOUT
                completeness = Completeness.UNKNOWN
                freshness = Freshness.UNKNOWN
                source_as_of = None
            elif any(f.kind in (FaultKind.DATA_DELAY, FaultKind.INDEX_MISSING) for f in active):
                status = ObservationStatus.NOT_FOUND
                completeness = Completeness.PARTIAL
                freshness = Freshness.UNKNOWN
                source = SourceKind.INDEX
                source_as_of = None
            else:
                cache = [f for f in active if f.kind == FaultKind.OLD_CACHE]
                replicas = [f for f in active if f.kind == FaultKind.REPLICA_LAG]
                if cache or replicas:
                    if cache:
                        # If several policies apply, the oldest eligible view wins.
                        revision = min(f.cached_revision for f in cache)
                        eligible = [(r, w) for r, w in history if r <= revision]
                        source = SourceKind.CACHE
                        source_as_of = eligible[-1][1].event_time if eligible else None
                    else:
                        source_as_of = now - timedelta(seconds=max(f.lag_seconds for f in replicas))
                        eligible = [(r, w) for r, w in history if w.event_time <= source_as_of]
                        source = SourceKind.REPLICA
                    freshness = Freshness.STALE
                    completeness = Completeness.PARTIAL
                    selected = eligible[-1][1] if eligible else None
                if selected is not None:
                    data, event_time = project(selected, tool, query, now)
                if data is None:
                    status = ObservationStatus.NOT_FOUND
                    # Even primary lookup absence is not proof of no financial effect.
                    completeness = Completeness.UNKNOWN if not active else Completeness.PARTIAL
                    event_time = None
            observation = Observation(
                observation_id=str(uuid4()), tool=tool, internal_order_id=query.internal_order_id,
                status=status, knowledge=KnowledgeStatus.OBSERVED if data is not None else KnowledgeStatus.UNKNOWN,
                event_time=event_time, observed_at=now, source_as_of=source_as_of,
                source_kind=source, completeness=completeness, freshness=freshness, data=data,
            )
            payload = observation.model_dump(mode="json")
            content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            session.add(ObservationRow(
                id=observation.observation_id, simulation_id=simulation.id, grant_hash=digest,
                tool=tool.value, request=query.model_dump(mode="json"),
                observation=payload, content_hash=content_hash,
                dispatch_correlation_id=dispatch_correlation_id,
            ))
        return observation
