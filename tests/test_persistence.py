import hashlib
import json
import os

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from credit_harness.domain.enums import PaymentFinality, ScenarioId, ToolName
from credit_harness.persistence.store import (
    GrantRow, ObservationRow, SimulatorAdmin, create_schema, open_engine,
)
from credit_harness.simulator.scenarios import ORDER_ID, build_scenario
from credit_harness.simulator.service import ObservationService
from credit_harness.tools.contracts import ToolQuery
from tests.support.inspector import ground_truth, world_state


def test_observations_are_persisted_with_hash_without_plaintext_tokens(engine, seeded, query):
    sid, token = seeded()
    observation = query(token, ToolName.CALLBACK)
    with Session(engine) as session:
        row = session.get(ObservationRow, observation.observation_id)
        assert row.simulation_id == sid
        assert row.observation == observation.model_dump(mode="json")
        assert row.content_hash == hashlib.sha256(json.dumps(row.observation, sort_keys=True).encode()).hexdigest()
        assert token not in json.dumps(row.observation)
        grant = session.scalar(select(GrantRow))
        assert grant.token_hash != token


def _verify_reopen(url):
    first = open_engine(url)
    create_schema(first)
    admin = SimulatorAdmin(first)
    sid = admin.seed(build_scenario(ScenarioId.S6))
    token = admin.grant(sid, {ToolName.PAYMENT})
    query = ToolQuery(internal_order_id=ORDER_ID)
    before = ObservationService(first).observe(token, ToolName.PAYMENT, query)
    state_before = world_state(first, sid)
    first.dispose()
    second = open_engine(url)
    try:
        after = ObservationService(second).observe(token, ToolName.PAYMENT, query)
        assert before.data == after.data
        assert after.data.record.payment_finality == PaymentFinality.SETTLED
        assert after.observation_id != before.observation_id
        assert world_state(second, sid) == state_before
        assert ground_truth(second, sid).disbursement_count == 1
        with Session(second) as session:
            assert session.get(ObservationRow, before.observation_id) is not None
    finally:
        second.dispose()


def test_file_database_reopen(tmp_path):
    _verify_reopen(f"sqlite:///{(tmp_path / 'persistent.db').as_posix()}")


@pytest.mark.postgres
def test_postgres_reopen():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to a dedicated PostgreSQL test database")
    _verify_reopen(url)
