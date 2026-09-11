"""Trusted synthetic Step 8 demo; no live provider, bank or automatic recovery."""
import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient
from scripts.demo_case_evidence import LocalHTTPClient
from credit_harness.api.app import create_app
from credit_harness.cases.executor import CaseToolExecutor
from credit_harness.cases.fixtures import investigation_case
from credit_harness.cases.repository import CaseRepository
from credit_harness.cases.schema import create_harness_schema
from credit_harness.cases.service import CaseService
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.evidence.models import ClaimType
from credit_harness.domain.enums import ScenarioId, ToolName as T
from credit_harness.persistence.store import SimulatorAdmin, create_schema, open_engine
from credit_harness.simulator.scenarios import build_scenario
from credit_harness.simulator import service as projection_service
from credit_harness.tools.contracts import ToolQuery, MessagesData, ConsumerDeploymentObservation
from credit_harness.remediation.state import InvestigationStateReader
from credit_harness.remediation.model import FakeRemediationModel
from credit_harness.remediation.models import RemediationCandidate, RemediationDraft, RemediationActionType as A
from credit_harness.remediation.service import RemediationPlanner
from credit_harness.adapters.remediation_fake import remediation_fake_model
from credit_harness.authorization.models import ApprovalDecision, ApprovalStatus, EffectStatus
from credit_harness.authorization.signing import HMACCapabilitySigner
from credit_harness.authorization.service import RemediationAuthorizationService, RemediationExecutionService
from credit_harness.authorization.tables import create_authorization_schema
from credit_harness.adapters.synthetic_remediation import SyntheticRemediationAdapter, create_synthetic_effect_schema


def ready_projection(original):
    """Explicit synthetic READ fixture, before normal Observation hashing/storage.

    This does not change the normal S6 scenario or infer an old parser version.
    Only the demo fixture supplies a currently compatible deployment observation.
    """
    def project(world, tool, query, now):
        data, event_time = original(world, tool, query, now)
        if isinstance(data, MessagesData):
            data = MessagesData(records=tuple(r.model_copy(update={"consumer_deployment": ConsumerDeploymentObservation(
                event_time=now, schema_version="2.3", accepted_protocol_version="2.3", loan_no_type="string")})
                for r in data.records))
        return data, event_time
    return project


def run_demo(engine, *, scenario=ScenarioId.S6, administrative=False, reject=False, timeout=False,
             read_after_execution=True):
    signer = HMACCapabilitySigner()  # required environment secret; never printed
    create_schema(engine)
    create_harness_schema(engine)
    create_authorization_schema(engine)
    create_synthetic_effect_schema(engine)
    admin = SimulatorAdmin(engine)  # trusted synthetic bootstrap, outside runtime
    sid = admin.seed(build_scenario(scenario))
    token = admin.grant(sid, set(T))
    cases = CaseRepository(engine, "demo")
    case = CaseService(cases).create(investigation_case(sid), tool_credential=token)
    evidence = EvidenceRepository(cases)
    reader = InvestigationStateReader(cases, evidence)
    fixture = patch.object(projection_service, "project", ready_projection(projection_service.project)) if scenario == ScenarioId.S6 else nullcontext()
    with fixture, TestClient(create_app(engine)) as http:
        client = LocalHTTPClient(http, token)
        executor = CaseToolExecutor(cases, evidence, lambda _: client)
        def read(tool, version=None):
            admin.advance(sid, 1)
            return executor.execute(case.case_id, tool, ToolQuery(internal_order_id=case.internal_order_id,
                                                                 protocol_version=version))
        for tool in (T.TRACE, T.FUND, T.PAYMENT, T.CALLBACK, T.MESSAGES, T.GUARANTEE, T.ASSET, T.ASSET_DELIVERY):
            read(tool)
        read(T.PROTOCOL, "2.3")
        read(T.PROTOCOL, "2.2")
        model = remediation_fake_model()
        if scenario == ScenarioId.S7 or administrative:
            # Explicit fake model proposal, still goes through the real Step 7
            # validator, preflight and selection. Runtime does not synthesize it.
            action = A.CREATE_RECONCILIATION_TASK if administrative else A.REDELIVER_ASSET_NOTIFICATION
            def proposal(bundle):
                refs = tuple(sorted({r for f in bundle.untrusted_external_data.current_facts for r in f.evidence_refs}
                    | {r for h in bundle.deterministic_derived.hypotheses for r in h.decisive_evidence_refs}
                    | set(bundle.deterministic_derived.financial_identity.evidence_refs)))
                return RemediationDraft(snapshot_id=bundle.snapshot_id, candidates=(RemediationCandidate(
                    candidate_id="synthetic-proposal", action_type=action, target_order_id=case.internal_order_id,
                    target_problem_ids=("H7",), evidence_refs=refs, reason_summary="Synthetic demonstration proposal."),))
            model = FakeRemediationModel(proposal)
        decision = RemediationPlanner(reader, model).plan(case.case_id)
        intent = decision.final_intent
        if intent is None:
            raise RuntimeError("fixture did not produce a READY intent")
        authorization = RemediationAuthorizationService(reader, signer)
        approval = None
        if intent.requires_approval:
            requested = authorization.request_approval(intent)
            approval = authorization.decide_approval(ApprovalDecision(approval_id=requested.approval_id,
                decision=ApprovalStatus.REJECTED if reject else ApprovalStatus.APPROVED, actor_ref="OPERATOR-001"))
        output = dict(case_id=case.case_id, intent=intent.model_dump(mode="json"),
            approval=approval.model_dump(mode="json", exclude={"intent"}) if approval else None)
        if reject and approval:
            return {**output, "execution": "NOT_AUTHORIZED", "case_status": cases.get(case.case_id).status.value}
        capability = authorization.issue_capability(intent, approval_id=approval.approval_id if approval else None)
        adapter = SyntheticRemediationAdapter(engine, "demo")
        if timeout:
            class LostResponse:
                def dispatch(self, command, correlation):
                    adapter.dispatch(command, correlation)
                    raise TimeoutError("synthetic response lost after effect")
            chosen_adapter = LostResponse()
        else:
            chosen_adapter = adapter
        runtime = RemediationExecutionService(reader, signer, chosen_adapter)
        before = evidence.list(case.case_id)
        before_case = cases.get(case.case_id)
        result = runtime.execute(capability)
        duplicate = runtime.execute(capability)
        assert evidence.list(case.case_id) == before
        assert cases.get(case.case_id).status == before_case.status
        assert cases.get(case.case_id).budget == before_case.budget
        output.update(capability_summary=capability.payload.model_dump(mode="json", include={
            "case_id", "internal_order_id", "action_type", "max_effects", "expires_at"}),
            execution=result.model_dump(mode="json"), duplicate_execution=duplicate.model_dump(mode="json"),
            business_verification="NOT_YET_VERIFIED", evidence_before=len(before), evidence_after_execution=len(before))
        if read_after_execution and intent.action_type in (A.REPLAY_CALLBACK_CONSUMPTION, A.REDELIVER_ASSET_NOTIFICATION):
            read(T.MESSAGES if intent.action_type == A.REPLAY_CALLBACK_CONSUMPTION else T.ASSET_DELIVERY)
        old_ids = {e.evidence_id for e in before}
        new = [e for e in evidence.list(case.case_id) if e.evidence_id not in old_ids]
        output.update(new_evidence=[e.model_dump(mode="json") for e in new if e.claim_type in (
            ClaimType.MESSAGE_CONSUME_STATUS, ClaimType.ASSET_DELIVERY_STATUS)],
            evidence_after_read=len(evidence.list(case.case_id)), case_status=cases.get(case.case_id).status.value,
            audit=[r.model_dump(mode="json") for r in runtime.store.audit(case.case_id)])
        assert result.ledger.status != EffectStatus.VERIFIED
        return output


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["S6", "S7"], default="S6")
    parser.add_argument("--administrative", action="store_true")
    parser.add_argument("--reject", action="store_true")
    parser.add_argument("--timeout", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    Path(".local").mkdir(exist_ok=True)
    engine = open_engine(f"sqlite:///.local/side-effect-demo-{uuid4().hex}.db")
    try:
        output = run_demo(engine, scenario=ScenarioId(args.scenario), administrative=args.administrative,
                          reject=args.reject, timeout=args.timeout)
        payload = json.dumps(output, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        print("BUSINESS VERIFICATION: NOT YET VERIFIED; CASE NOT CLOSED")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
