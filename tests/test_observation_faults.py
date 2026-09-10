from datetime import timedelta

import pytest

from credit_harness.domain.enums import (
    Completeness, FaultKind, Freshness, KnowledgeStatus, LoanStatus,
    ObservationStatus, ScenarioId, SourceKind, ToolName,
)
from credit_harness.simulator.faults import ObservationFault
from credit_harness.simulator.scenarios import T0
from tests.support.inspector import world_state


@pytest.mark.parametrize("kind", [FaultKind.TIMEOUT, FaultKind.DATA_DELAY, FaultKind.INDEX_MISSING])
def test_inaccessible_fact_does_not_become_negative_truth(engine, admin, seeded, query, kind):
    sid, token = seeded(ScenarioId.S6)
    before = world_state(engine, sid)
    admin.add_fault(sid, ObservationFault(
        tool=ToolName.CALLBACK, kind=kind, starts_at=T0,
        ends_at=T0 + timedelta(seconds=20),
    ))
    hidden = query(token, ToolName.CALLBACK)
    assert hidden.status == (ObservationStatus.TIMEOUT if kind == FaultKind.TIMEOUT else ObservationStatus.NOT_FOUND)
    assert hidden.knowledge == KnowledgeStatus.UNKNOWN
    assert hidden.data is None and hidden.event_time is None
    assert hidden.completeness != Completeness.COMPLETE
    assert hidden.source_as_of is None
    admin.advance(sid, 10)  # exact ends_at is outside the active fault window
    visible = query(token, ToolName.CALLBACK)
    assert visible.status == ObservationStatus.OK
    assert visible.data.record.signature_verified
    assert visible.event_time == T0 + timedelta(seconds=3)
    assert visible.observed_at == T0 + timedelta(seconds=20)
    assert world_state(engine, sid) == before


def test_old_cache_returns_actual_old_snapshot_not_relabelled_new_data(admin, seeded, query):
    sid, token = seeded(ScenarioId.S7)
    admin.add_fault(sid, ObservationFault(
        tool=ToolName.GUARANTEE, kind=FaultKind.OLD_CACHE,
        starts_at=T0, ends_at=T0 + timedelta(seconds=30), cached_revision=0,
    ))
    old = query(token, ToolName.GUARANTEE)
    assert old.data.record.status == LoanStatus.PROCESSING
    assert old.data.record.version == 17
    assert old.event_time == old.source_as_of == T0
    assert old.freshness == Freshness.STALE and old.source_kind == SourceKind.CACHE
    admin.advance(sid, 20)
    current = query(token, ToolName.GUARANTEE)
    assert current.data.record.status == LoanStatus.SUCCESS
    assert current.data.record.version == 18
    assert current.freshness == Freshness.CURRENT


def test_replica_with_no_old_snapshot_returns_unknown(admin, seeded, query):
    sid, token = seeded()
    admin.add_fault(sid, ObservationFault(
        tool=ToolName.GUARANTEE, kind=FaultKind.REPLICA_LAG,
        starts_at=T0, lag_seconds=100,
    ))
    result = query(token, ToolName.GUARANTEE)
    assert result.status == ObservationStatus.NOT_FOUND
    assert result.data is None and result.knowledge == KnowledgeStatus.UNKNOWN


def test_combined_faults_have_deterministic_safe_precedence(admin, seeded, query):
    sid, token = seeded()
    for kind in (FaultKind.INDEX_MISSING, FaultKind.TIMEOUT):
        admin.add_fault(sid, ObservationFault(tool=ToolName.FUND, kind=kind, starts_at=T0))
    assert query(token, ToolName.FUND).status == ObservationStatus.TIMEOUT


def test_faults_do_not_cross_tool_boundaries(admin, seeded, query):
    sid, token = seeded()
    admin.add_fault(sid, ObservationFault(tool=ToolName.CALLBACK, kind=FaultKind.INDEX_MISSING, starts_at=T0))
    assert query(token, ToolName.CALLBACK).data is None
    assert query(token, ToolName.MESSAGES).data.records[0].dlq is not None


def test_future_fault_does_not_apply_early(admin, seeded, query):
    sid, token = seeded()
    admin.add_fault(sid, ObservationFault(
        tool=ToolName.PAYMENT, kind=FaultKind.TIMEOUT,
        starts_at=T0 + timedelta(seconds=11),
    ))
    assert query(token, ToolName.PAYMENT).status == ObservationStatus.OK
    admin.advance(sid, 1)
    assert query(token, ToolName.PAYMENT).status == ObservationStatus.TIMEOUT

