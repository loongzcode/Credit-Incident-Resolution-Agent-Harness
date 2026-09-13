from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from tests.test_agent_runtime import setup_agent
from tests.test_case_evidence import harness
from tests.test_organizational_memory import factory
from credit_harness.agent.models import AgentRunConfig
from credit_harness.investigation.trace_store import InvestigationTraceRow
from credit_harness.recovery.tables import AgentCheckpointRow
from credit_harness.cases.tables import CaseCallRow
from credit_harness.evaluation.tables import CaseClosureRow
from credit_harness.evaluation.models import EvaluationVerdict
from credit_harness.context.budget import digest


def test_crash_after_completed_turn_keeps_checkpoint_and_trace(setup_agent, monkeypatch):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    original = runtime.checkpoints.save
    class Crash(BaseException): pass
    def save(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get('completed_turn') is not None: raise Crash()
        return result
    monkeypatch.setattr(runtime.checkpoints,'save',save)
    with pytest.raises(Crash): runtime.run('CASE-JD202609100001')
    with Session(runtime.cases.engine) as s:
        assert s.scalar(select(func.count()).select_from(InvestigationTraceRow)) > 0
        assert s.scalar(select(func.count()).select_from(CaseCallRow)) == 1
        checkpoint = s.scalar(select(AgentCheckpointRow))
        assert checkpoint.last_completed_turn == 1
    assert not runtime.trace_store.records  # final append was never reached


def test_completed_turn_trace_is_idempotent(setup_agent):
    runtime = setup_agent(config=AgentRunConfig(max_turns=1)); result = runtime.run('CASE-JD202609100001')
    with Session(runtime.cases.engine) as s:
        before = s.scalar(select(func.count()).select_from(InvestigationTraceRow))
    runtime.trace_store.record_turn(result.case_id,result.run_id,result.turns[0])
    runtime.trace_store.append(result)
    with Session(runtime.cases.engine) as s:
        assert s.scalar(select(func.count()).select_from(InvestigationTraceRow)) == before


def test_production_planner_audit_is_durable_without_raw_proposal(setup_agent):
    from credit_harness.investigation.trace_store import SQLSafePlannerAuditStore
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    runtime.planner.audit = SQLSafePlannerAuditStore(runtime.trace_store)
    runtime.run('CASE-JD202609100001')
    with Session(runtime.cases.engine) as s:
        rows = list(s.scalars(select(InvestigationTraceRow)))
    audit = next(r.payload for r in rows if r.payload['status']=='AUDITED')
    names = {f['name'] for f in audit['fields']}
    assert {'input_hash','output_hash','model_name','selected_action'} <= names
    assert all(term not in str(audit) for term in ('internal_order_id','reason_summary','system_contract','private_reasoning'))


@pytest.mark.postgres
def test_postgres_evaluator_closure_lock_order_no_deadlock(factory):
    x = factory()
    if x.engine.dialect.name != 'postgresql': pytest.skip('PostgreSQL lock semantics')
    x.read(); x.progress(); x.read()
    start = Barrier(4)
    def evaluate(_):
        start.wait(timeout=10)
        return x.evaluate()
    with ThreadPoolExecutor(max_workers=4) as p: reports = list(p.map(evaluate,range(4)))
    assert all(r.overall_verdict == EvaluationVerdict.PASS for r in reports)
    start = Barrier(4)
    def close(_):
        start.wait(timeout=10)
        return x.closure.close(reports[0])
    with ThreadPoolExecutor(max_workers=4) as p: closures = list(p.map(close,range(4)))
    assert len({c.record.closure_id for c in closures}) == 1
    with Session(x.engine) as s:
        assert s.scalar(select(func.count()).select_from(CaseClosureRow)) == 1
