import hashlib
import secrets
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from credit_harness.domain.enums import ToolName
from credit_harness.domain.models import WorldState
from credit_harness.simulator.faults import ObservationFault
from credit_harness.simulator.scenarios import Scenario


class Base(DeclarativeBase):
    pass


class SimulationRow(Base):
    __tablename__ = "simulations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(8))
    clock: Mapped[str] = mapped_column(String(40))
    clock_version: Mapped[int] = mapped_column(Integer, default=0)


class SnapshotRow(Base):
    __tablename__ = "world_snapshots"
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[dict] = mapped_column(JSON)


class GroundTruthRow(Base):
    __tablename__ = "evaluation_ground_truth"
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"), primary_key=True)
    truth: Mapped[dict] = mapped_column(JSON)


class FaultRow(Base):
    __tablename__ = "observation_faults"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"))
    policy: Mapped[dict] = mapped_column(JSON)


class GrantRow(Base):
    __tablename__ = "tool_grants"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"))
    order_id: Mapped[str] = mapped_column(String(80))
    allowed_tools: Mapped[list] = mapped_column(JSON)
    expires_at: Mapped[str] = mapped_column(String(40))


class ObservationRow(Base):
    __tablename__ = "tool_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    simulation_id: Mapped[str] = mapped_column(ForeignKey("simulations.id"))
    grant_hash: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(50))
    request: Mapped[dict] = mapped_column(JSON)
    observation: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    # NULL is allowed for standalone simulator calls and legacy observations.
    dispatch_correlation_id: Mapped[str | None] = mapped_column(String(36))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def open_engine(url: str) -> Engine:
    options = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    return create_engine(url, **options)


def create_schema(engine: Engine) -> None:
    from credit_harness.production.schema import runtime_managed
    if runtime_managed(engine):
        return  # schema version is validated by the production startup gate
    """Additive bootstrap, including Step 2.5 provenance columns on existing DBs."""
    from .migrations import add_dispatch_correlation_columns

    Base.metadata.create_all(engine)
    add_dispatch_correlation_columns(engine)


def snapshots(session: Session, simulation_id: str) -> list[tuple[int, WorldState]]:
    rows = session.scalars(select(SnapshotRow).where(
        SnapshotRow.simulation_id == simulation_id,
    ).order_by(SnapshotRow.revision)).all()
    return [(row.revision, WorldState.model_validate(row.state)) for row in rows]


class SimulatorAdmin:
    """Trusted local setup/clock controls. Never mounted in the HTTP app."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def seed(self, scenario: Scenario) -> str:
        simulation_id = str(uuid4())
        with Session(self.engine) as session, session.begin():
            session.add(SimulationRow(
                id=simulation_id, scenario_id=scenario.scenario_id.value,
                clock=scenario.initial_time.isoformat(), clock_version=0,
            ))
            session.flush()
            for revision, state in enumerate(scenario.frames):
                session.add(SnapshotRow(
                    simulation_id=simulation_id, revision=revision,
                    state=state.model_dump(mode="json"),
                ))
            session.add(GroundTruthRow(
                simulation_id=simulation_id, truth=scenario.ground_truth.model_dump(mode="json"),
            ))
            for policy in scenario.faults:
                session.add(FaultRow(id=str(uuid4()), simulation_id=simulation_id,
                                     policy=policy.model_dump(mode="json")))
        return simulation_id

    def grant(self, simulation_id: str, tools: set[ToolName], *, ttl_seconds: int = 3600) -> str:
        from datetime import datetime

        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        allowed = [ToolName(tool).value for tool in tools]
        token = secrets.token_urlsafe(32)
        with Session(self.engine) as session, session.begin():
            row = session.get(SimulationRow, simulation_id)
            if row is None:
                raise KeyError("simulation not found")
            state = snapshots(session, simulation_id)[-1][1]
            expires_at = datetime.fromisoformat(row.clock) + timedelta(seconds=ttl_seconds)
            session.add(GrantRow(
                token_hash=token_hash(token), simulation_id=simulation_id,
                order_id=state.guarantee.internal_order_id, allowed_tools=sorted(allowed),
                expires_at=expires_at.isoformat(),
            ))
        return token

    def advance(self, simulation_id: str, seconds: int):
        from datetime import datetime
        from sqlalchemy import update

        if type(seconds) is not int or seconds < 0:
            raise ValueError("clock only advances by nonnegative integer seconds")
        with Session(self.engine) as session, session.begin():
            row = session.get(SimulationRow, simulation_id)
            if row is None:
                raise KeyError("simulation not found")
            new_time = datetime.fromisoformat(row.clock) + timedelta(seconds=seconds)
            result = session.execute(update(SimulationRow).where(
                SimulationRow.id == simulation_id,
                SimulationRow.clock_version == row.clock_version,
            ).values(clock=new_time.isoformat(), clock_version=row.clock_version + 1))
            if result.rowcount != 1:
                raise RuntimeError("concurrent clock advance; retry from current time")
        return new_time

    def add_fault(self, simulation_id: str, fault: ObservationFault) -> None:
        with Session(self.engine) as session, session.begin():
            if session.get(SimulationRow, simulation_id) is None:
                raise KeyError("simulation not found")
            session.add(FaultRow(id=str(uuid4()), simulation_id=simulation_id,
                                 policy=fault.model_dump(mode="json")))

    def revoke(self, token: str) -> None:
        with Session(self.engine) as session, session.begin():
            row = session.get(GrantRow, token_hash(token))
            if row is not None:
                session.delete(row)
