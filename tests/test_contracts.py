from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from credit_harness.domain.enums import (
    FaultKind, FieldType, LoanStatus, ScenarioId, ToolName,
)
from credit_harness.domain.models import AssetSystem, DisbursementTransaction
from credit_harness.simulator.faults import ObservationFault
from credit_harness.simulator.scenarios import ORDER_ID, T0, build_scenario


@pytest.mark.parametrize("bad_amount", [20_000.00, 2_000_000.0, "2000000", True, -1])
def test_money_must_be_nonnegative_integer_minor_units(bad_amount):
    transaction = build_scenario(ScenarioId.S6).frames[-1].fund.disbursement_transaction.model_dump()
    transaction["amount"] = bad_amount
    with pytest.raises(ValidationError):
        DisbursementTransaction.model_validate(transaction)


def test_naive_datetime_and_undefined_state_rejected():
    valid = dict(event_time=T0, update_time=T0, asset_order_id=ORDER_ID, loan_status=LoanStatus.PROCESSING)
    with pytest.raises(ValidationError):
        AssetSystem(**{**valid, "event_time": datetime(2026, 9, 10)})
    with pytest.raises(ValidationError):
        AssetSystem(**{**valid, "loan_status": "WHATEVER"})


def test_version_selection_uses_effective_time(seeded, query):
    _, token = seeded()
    old = query(token, ToolName.PROTOCOL, effective_at=(T0 - timedelta(days=2)).isoformat())
    assert old.data.record.protocol_version == "2.2"
    assert old.data.record.field_schema["loanNo"] == FieldType.INTEGER
    assert query(token, ToolName.PROTOCOL).data.record.protocol_version == "2.3"


def test_future_queries_and_irrelevant_parameters_rejected(client, seeded):
    _, token = seeded()
    headers = {"Authorization": f"Bearer {token}"}
    future = {"internal_order_id": ORDER_ID, "effective_at": (T0 + timedelta(days=1)).isoformat()}
    assert client.post(f"/tools/{ToolName.PROTOCOL.value}", headers=headers, json=future).status_code == 422
    assert client.post(f"/tools/{ToolName.FUND.value}", headers=headers,
                       json={"internal_order_id": ORDER_ID, "protocol_version": "2.2"}).status_code == 422


def test_invalid_faults_and_clock_reversal_rejected(admin, seeded):
    sid, _ = seeded()
    for kwargs in (
        {"kind": FaultKind.OLD_CACHE},
        {"kind": FaultKind.REPLICA_LAG, "lag_seconds": 0},
        {"kind": FaultKind.TIMEOUT, "ends_at": T0},
    ):
        with pytest.raises(ValidationError):
            ObservationFault(tool=ToolName.FUND, starts_at=T0, **kwargs)
    for bad in (-1, 1.2, True):
        with pytest.raises(ValueError):
            admin.advance(sid, bad)

