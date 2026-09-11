"""Explicit synthetic worker restarts. No model-based recovery or hidden retry."""
import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from scripts.demo_side_effect import run_demo as side_effect_demo
from scripts.demo_case_evidence import LocalHTTPClient
from credit_harness.api.app import create_app
from credit_harness.persistence.store import SimulatorAdmin, create_schema, open_engine
from credit_harness.simulator.scenarios import build_scenario
from credit_harness.domain.enums import ScenarioId, ToolName as T
from credit_harness.cases.schema import create_harness_schema
from credit_harness.cases.service import CaseService
from credit_harness.cases.fixtures import investigation_case
from credit_harness.cases.repository import CaseRepository, utc_now
from credit_harness.cases.tables import CaseCallRow
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.tools.contracts import ToolQuery
from credit_harness.authorization.tables import EffectRow
from credit_harness.authorization.store import SQLApprovalStore
from credit_harness.authorization.signing import HMACCapabilitySigner
from credit_harness.authorization.service import RemediationExecutionService
from credit_harness.remediation.state import InvestigationStateReader
from credit_harness.adapters.synthetic_remediation import SyntheticRemediationAdapter
from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
from credit_harness.recovery.models import RecoveryPolicy
from credit_harness.recovery.repository import RecoveryRepository
from credit_harness.recovery.read import ReadObservationRecoveryService
from credit_harness.recovery.service import SideEffectRecoveryCoordinator
from credit_harness.recovery.prepared import PreparedEffectResumer

CASE = "CASE-JD202609100001"


class SyntheticWorkerCrash(BaseException):
    pass


def read_orphan(engine):
    create_schema(engine)
    create_harness_schema(engine)
    admin = SimulatorAdmin(engine)
    sid = admin.seed(build_scenario(ScenarioId.S6))
    admin.advance(sid, 30)
    token = admin.grant(sid, set(T))
    cases = CaseRepository(engine, "demo")
    case = CaseService(cases).create(investigation_case(sid), tool_credential=token)
    evidence = EvidenceRepository(cases)
    dispatches = []
    with TestClient(create_app(engine)) as http:
        client = LocalHTTPClient(http, token)
        original = client.observe
        def lost(*args, **kwargs):
            dispatches.append(kwargs["dispatch_correlation_id"])
            original(*args, **kwargs)  # durable Observation before transport loss
            raise TimeoutError("synthetic response lost")
        client.observe = lost
        executor = CaseToolExecutor(cases, evidence, lambda _: client)
        try:
            executor.execute(CASE, T.MESSAGES, ToolQuery(internal_order_id=case.internal_order_id))
        except TimeoutError:
            pass
    with Session(engine) as session:
        call = session.scalar(select(CaseCallRow))
        call_id, correlation = call.call_id, call.dispatch_correlation_id
    before = dict(budget=cases.get(CASE).budget.used_tool_calls, evidence=len(evidence.list(CASE)))
    # New worker's independent repository; no Tool client exists in recovery.
    recovery = ReadObservationRecoveryService(EvidenceRepository(CaseRepository(engine, "demo")))
    result = recovery.recover(CASE, call_id)
    duplicate = recovery.recover(CASE, call_id)
    after = dict(budget=cases.get(CASE).budget.used_tool_calls, evidence=len(evidence.list(CASE)))
    return dict(scenario="read-orphan", case_id=CASE, correlation=correlation, before=before, after=after,
        recovery=result.model_dump(mode="json"), duplicate_recovery=duplicate.model_dump(mode="json"),
        tool_dispatch_count=len(dispatches), tool_redispatch=False, case_status=cases.get(CASE).status.value,
        evidence=[e.model_dump(mode="json") for e in evidence.list(CASE)])


def run_demo(engine, scenario):
    if scenario == "read-orphan":
        return read_orphan(engine)
    if scenario == "effect-timeout":
        side_effect_demo(engine, timeout=True, read_after_execution=False)
    else:
        target = "dispatch_prepared" if scenario == "worker-crash-after-prepared" else "_complete_dispatch"
        cls = SQLApprovalStore if target == "dispatch_prepared" else RemediationExecutionService
        try:
            with patch.object(cls, target, side_effect=SyntheticWorkerCrash):
                side_effect_demo(engine, read_after_execution=False)
        except SyntheticWorkerCrash:
            pass
    cases = CaseRepository(engine, "demo")
    evidence = EvidenceRepository(cases)
    reader = InvestigationStateReader(cases, evidence)
    store = SQLApprovalStore(cases)
    with Session(engine) as session:
        effect_id = session.scalar(select(EffectRow.effect_id).where(EffectRow.case_id == CASE))
    before = store.get_ledger(effect_id)
    evidence_before = evidence.list(CASE)
    resolver = SyntheticEffectStatusResolver(engine, "demo")
    repository = RecoveryRepository(store, resolver.capability, policy=RecoveryPolicy(grace_seconds=0))
    runtime = RemediationExecutionService(reader, HMACCapabilitySigner(), SyntheticRemediationAdapter(engine, "demo"))
    recovery = SideEffectRecoveryCoordinator(repository, resolver, prepared_resumer=PreparedEffectResumer(runtime),
                                             worker_id="NEW-RECOVERY-WORKER")
    result = recovery.recover(effect_id)
    after = store.get_ledger(effect_id)
    assert evidence.list(CASE) == evidence_before
    assert after.dispatch_correlation_id == before.dispatch_correlation_id
    return dict(scenario=scenario, case_id=CASE, new_worker=True, before=before.model_dump(mode="json"),
        recovery=result.model_dump(mode="json"), after=after.model_dump(mode="json"),
        dispatch_again=False, first_dispatch_resumed=before.attempt_count == 0 and after.attempt_count == 1,
        evidence_unchanged=True, evidence_count=len(evidence_before), business_verified=False,
        case_status=cases.get(CASE).status.value, next_step="Explicit Read Tool, then future independent verification",
        attempts=[a.model_dump(mode="json") for a in repository.attempts(effect_id)],
        audit=[r.model_dump(mode="json") for r in repository.audit(effect_id)])


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["read-orphan", "effect-timeout", "worker-crash-after-prepared",
                                               "worker-crash-after-dispatch"], default="effect-timeout")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/recovery-demo-{uuid4().hex}.db")
    try:
        output = run_demo(engine, args.scenario)
        payload = json.dumps(output, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
