import json

import pytest

from credit_harness.domain.enums import LoanStatus, ScenarioId, ToolName
from credit_harness.simulator.scenarios import ORDER_ID


def test_unauthenticated_and_invalid_token_are_rejected(client):
    for headers in ({}, {"Authorization": "Bearer bad-token"}):
        result = client.post(f"/tools/{ToolName.FUND.value}",
                             json={"internal_order_id": ORDER_ID}, headers=headers)
        assert result.status_code == 401


@pytest.mark.parametrize("endpoint", ["/world", "/ground_truth", "/scenarios", "/admin", "/tools/ground_truth"])
def test_no_hidden_state_route(client, seeded, endpoint):
    _, token = seeded()
    assert client.get(endpoint, headers={"Authorization": f"Bearer {token}"}).status_code in (404, 405)
    assert client.post(endpoint, headers={"Authorization": f"Bearer {token}"},
                       json={"internal_order_id": ORDER_ID}).status_code in (404, 405, 422)


def test_no_ground_truth_or_world_type_in_openapi(client):
    schema = client.get("/openapi.json").json()
    serialized = json.dumps(schema)
    for forbidden in ("WorldState", "GroundTruth", "ScenarioId", "RootCause", "ObservationFault", "expected_entry"):
        assert forbidden not in serialized
    assert set(schema["paths"]) == {"/tools/{tool}"}


def test_field_scopes_do_not_leak_raw_callback_or_payment(client, seeded):
    _, token = seeded(tools={ToolName.CALLBACK, ToolName.FUND, ToolName.MESSAGES, ToolName.ACCOUNTING})
    headers = {"Authorization": f"Bearer {token}"}
    for tool in (ToolName.CALLBACK, ToolName.MESSAGES):
        result = client.post(f"/tools/{tool.value}", headers=headers, json={"internal_order_id": ORDER_ID})
        assert result.status_code == 200
        assert "raw_callback" not in result.text
        assert '"loanNo":"LN-' not in result.text
    fund = client.post(f"/tools/{ToolName.FUND.value}", headers=headers, json={"internal_order_id": ORDER_ID})
    assert "payment_finality" not in fund.text and "disbursement_transaction" not in fund.text
    accounting = client.post(f"/tools/{ToolName.ACCOUNTING.value}", headers=headers, json={"internal_order_id": ORDER_ID})
    assert "expected_entry" not in accounting.text
    for forbidden_tool in (ToolName.CALLBACK_RAW, ToolName.PAYMENT):
        assert client.post(f"/tools/{forbidden_tool.value}", headers=headers,
                           json={"internal_order_id": ORDER_ID}).status_code == 403


def test_client_cannot_expand_scope_or_request_arbitrary_fields(client, seeded):
    _, token = seeded(tools={ToolName.FUND})
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(f"/tools/{ToolName.ASSET.value}", headers=headers,
                       json={"internal_order_id": ORDER_ID}).status_code == 403
    assert client.post(f"/tools/{ToolName.FUND.value}", headers=headers,
                       json={"internal_order_id": "OTHER-ORDER"}).status_code == 403
    for injected in ({"simulation_id": "anything"}, {"fields": ["ground_truth"]},
                     {"role": "admin"}, {"observed_at": "2030-01-01T00:00:00Z"},
                     {"sql": "SELECT * FROM evaluation_ground_truth"}):
        assert client.post(f"/tools/{ToolName.FUND.value}", headers=headers,
                           json={"internal_order_id": ORDER_ID, **injected}).status_code == 422


def test_token_binds_world_even_when_order_id_is_identical(seeded, query):
    _, successful_token = seeded(ScenarioId.S7)
    _, failed_token = seeded(ScenarioId.S3)
    assert query(successful_token, ToolName.GUARANTEE).data.record.status == LoanStatus.SUCCESS
    assert query(failed_token, ToolName.GUARANTEE).data.record.status == LoanStatus.FAILED


def test_expiry_and_revocation(client, seeded, admin):
    sid, token = seeded(ttl_seconds=2)
    headers = {"Authorization": f"Bearer {token}"}
    admin.advance(sid, 2)
    assert client.post(f"/tools/{ToolName.FUND.value}", headers=headers,
                       json={"internal_order_id": ORDER_ID}).status_code == 401
    _, token2 = seeded()
    admin.revoke(token2)
    assert client.post(f"/tools/{ToolName.FUND.value}", headers={"Authorization": f"Bearer {token2}"},
                       json={"internal_order_id": ORDER_ID}).status_code == 401


@pytest.mark.parametrize("scenario", list(ScenarioId))
def test_observations_never_include_oracle_or_scenario_metadata(seeded, query, scenario):
    _, token = seeded(scenario)
    for tool in ToolName:
        payload = query(token, tool).model_dump_json()
        for hidden in ("ground_truth", "scenario_id", "root_cause", "expected_entry",
                       "fund_accepted", "disbursement_intent_count", "cached_revision", "faults"):
            assert hidden not in payload


@pytest.mark.parametrize("tool", ["disburse", "retry_loan", "repair_order", "query_sql", "close_case"])
def test_no_write_or_generic_query_tool(client, seeded, tool):
    _, token = seeded()
    result = client.post(f"/tools/{tool}", headers={"Authorization": f"Bearer {token}"},
                         json={"internal_order_id": ORDER_ID})
    assert result.status_code == 422

