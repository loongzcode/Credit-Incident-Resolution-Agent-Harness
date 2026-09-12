"""Shared synthetic operator and failure hooks; no real financial endpoint."""
from types import SimpleNamespace
from sqlalchemy import select
from sqlalchemy.orm import Session
from credit_harness.remediation.model import FakeRemediationModel
from credit_harness.remediation.models import RemediationActionType as A, RemediationCandidate, RemediationDraft
from credit_harness.remediation.service import RemediationPlanner
from credit_harness.authorization.models import ApprovalDecision, ApprovalStatus, EffectStatus
from credit_harness.authorization.signing import HMACCapabilitySigner
from credit_harness.authorization.service import RemediationAuthorizationService, RemediationExecutionService
from credit_harness.authorization.tables import EffectRow
from credit_harness.adapters.synthetic_remediation import SyntheticRemediationAdapter
from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
from credit_harness.recovery.repository import RecoveryRepository
from credit_harness.recovery.models import RecoveryPolicy
from credit_harness.recovery.service import SideEffectRecoveryCoordinator
from credit_harness.recovery.prepared import PreparedEffectResumer


class BenchmarkApprovalActor:
    """Synthetic operator, real durable approval and capability issuance."""
    def authorize(self, auth, intent):
        approval = None
        if intent.requires_approval:
            request = auth.request_approval(intent)
            approval = auth.decide_approval(ApprovalDecision(approval_id=request.approval_id,
                decision=ApprovalStatus.APPROVED, actor_ref="SYNTHETIC-BENCHMARK-OPERATOR"))
        return auth.issue_capability(intent, approval_id=approval.approval_id if approval else None)


class SyntheticWorkerCrash(BaseException):
    pass


def propose(reader, case_id):
    def model(bundle):
        state = reader.read(case_id)
        refs = tuple(e.evidence_id for e in state.index.evidence)[-128:]
        candidates = tuple(RemediationCandidate(candidate_id=name, action_type=action,
            target_order_id=bundle.trusted_control.internal_order_id, target_problem_ids=(problem,),
            evidence_refs=refs, reason_summary="Proposal subject to shared deterministic preflight.")
            for name, action, problem in (("replay", A.REPLAY_CALLBACK_CONSUMPTION, "H6"),
                ("redeliver", A.REDELIVER_ASSET_NOTIFICATION, "H7"), ("review", A.REQUEST_OPERATOR_REVIEW, "H4")))
        return RemediationDraft(snapshot_id=bundle.snapshot_id, candidates=candidates)
    return RemediationPlanner(reader, FakeRemediationModel(model)).plan(case_id)


def execute_synthetic(x, decision, fault):
    intent = decision.final_intent
    if not intent:
        return (), (), 0, 0
    signer = HMACCapabilitySigner()
    auth = RemediationAuthorizationService(x.reader, signer, clock=lambda: x.clock.now)
    cap = BenchmarkApprovalActor().authorize(auth, intent)
    adapter = SyntheticRemediationAdapter(x.engine, x.case.tenant_id)
    dispatch_count = 0
    def dispatch(command, correlation):
        nonlocal dispatch_count
        dispatch_count += 1
        result = adapter.dispatch(command, correlation)
        if fault == "effect-unknown":
            raise TimeoutError("synthetic lost receipt")
        if fault == "dispatched-crash":
            raise SyntheticWorkerCrash()
        return result
    runtime = RemediationExecutionService(x.reader, signer, SimpleNamespace(dispatch=dispatch), clock=lambda: x.clock.now)
    unknown = 0
    if fault == "prepared-crash":
        def crash(*args, **kwargs):
            raise SyntheticWorkerCrash()
        runtime.store.dispatch_prepared = crash
    try:
        result = runtime.execute(cap)
        unknown = int(result.ledger.status == EffectStatus.UNKNOWN)
    except SyntheticWorkerCrash:
        pass
    with Session(x.engine) as session:
        row = session.scalar(select(EffectRow).where(EffectRow.case_id == x.case.case_id))
        effect_id = row.effect_id
        before = row.status
    recovery = []
    if before in ("PREPARED", "DISPATCHED", "UNKNOWN"):
        resolver = SyntheticEffectStatusResolver(x.engine, x.case.tenant_id, clock=lambda: x.clock.now)
        repo = RecoveryRepository(auth.store, resolver.capability,
            policy=RecoveryPolicy(grace_seconds=0, backoff_seconds=(1,2,3)))
        clean = RemediationExecutionService(x.reader, signer, adapter, clock=lambda: x.clock.now)
        coordinator = SideEffectRecoveryCoordinator(repo, resolver, prepared_resumer=PreparedEffectResumer(clean),
            clock=lambda: x.clock.now, worker_id="BENCHMARK-RECOVERY")
        x.advance(1)
        recovery.append(coordinator.recover(effect_id).model_dump(mode="json"))
    return (effect_id,), tuple(recovery), unknown, max(0, dispatch_count - 1)
