import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from credit_harness.api.app import create_app
from credit_harness.domain.enums import ScenarioId, ToolName
from credit_harness.persistence.store import SimulatorAdmin, create_schema, open_engine
from credit_harness.simulator.scenarios import ORDER_ID, build_scenario
from credit_harness.tools.contracts import Observation


def pytest_addoption(parser):
    parser.addoption("--postgres", action="store_true", help="run all database fixtures on TEST_POSTGRES_URL")


@pytest.fixture
def engine(tmp_path, request):
    if not request.config.getoption("--postgres"):
        engine = open_engine(f"sqlite:///{(tmp_path / 'simulator.db').as_posix()}")
        create_schema(engine)
        yield engine
        engine.dispose()
        return
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url or not url.startswith("postgresql"):
        raise pytest.UsageError("--postgres requires TEST_POSTGRES_URL for an isolated test database")
    base = open_engine(url)
    schema = "sim_test_" + uuid4().hex
    with base.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = base.execution_options(schema_translate_map={None: schema})
    try:
        create_schema(engine)
        yield engine
    finally:
        # Only this freshly generated, per-test schema is removed.
        with base.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        base.dispose()


@pytest.fixture
def admin(engine):
    return SimulatorAdmin(engine)


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as client:
        yield client


@pytest.fixture
def seeded(admin):
    def seed(scenario=ScenarioId.S6, tools=None, ttl_seconds=3600):
        simulation_id = admin.seed(build_scenario(scenario))
        token = admin.grant(simulation_id, set(ToolName) if tools is None else tools,
                            ttl_seconds=ttl_seconds)
        return simulation_id, token
    return seed


@pytest.fixture
def query(client):
    def call(token, tool, **kwargs):
        response = client.post(
            f"/tools/{tool.value}", headers={"Authorization": f"Bearer {token}"},
            json={"internal_order_id": ORDER_ID, **kwargs},
        )
        assert response.status_code == 200, response.text
        return Observation.model_validate(response.json())
    return call
