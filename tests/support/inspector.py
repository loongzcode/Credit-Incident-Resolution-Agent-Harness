"""Privileged oracle access ONLY for tests, never exported through tool API."""

from sqlalchemy.orm import Session

from credit_harness.persistence.store import GroundTruthRow, snapshots
from credit_harness.simulator.scenarios import GroundTruth


def ground_truth(engine, simulation_id):
    with Session(engine) as session:
        row = session.get(GroundTruthRow, simulation_id)
        return GroundTruth.model_validate(row.truth)


def world_state(engine, simulation_id):
    with Session(engine) as session:
        return snapshots(session, simulation_id)[-1][1]

