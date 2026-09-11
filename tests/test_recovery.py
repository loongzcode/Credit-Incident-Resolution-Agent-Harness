import ast
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from tests.test_case_evidence import harness, execute, Q
from tests.test_remediation import prepared, preview, CASE
from tests.test_side_effect import setup_effect, issue, count, fails
from tests.test_agent_runtime import setup_agent
from tests.support.inspector import world_state
from credit_harness.domain.enums import ToolName as T, ConsumeStatus
from credit_harness.cases.models import CaseAccessError
from credit_harness.cases.repository import CaseRepository, CallState
from credit_harness.cases.tables import CaseCallRow
from credit_harness.persistence.store import ObservationRow
from credit_harness.evidence.models import json_hash, ClaimType
from credit_harness.authorization.models import (
    EffectStatus as S, ReceiptOutcome, SideEffectReceipt, ApprovalDecision, ApprovalStatus as P,
    AuthorizationError, AuthorizationCode as C,
)
from credit_harness.authorization.commands import DomainCommandBuilder, effect_key
from credit_harness.authorization.store import SQLApprovalStore
from credit_harness.authorization.tables import EffectRow, CapabilitySignatureRow
from credit_harness.authorization.service import RemediationExecutionService
from credit_harness.context.budget import digest
from credit_harness.adapters.synthetic_remediation import SyntheticEffectRow
from credit_harness.adapters.synthetic_effect_resolver import SyntheticEffectStatusResolver
from credit_harness.recovery.models import (
    RecoveryCapability, RecoveryPolicy, LookupStatus as L, SideEffectLookupResult,
    RecoveryFailure as F, RecoveryOutcome as O, DispatchRecoveryStatus as D, RecoveryError,
)
from credit_harness.recovery.read import ReadObservationRecoveryService
from credit_harness.recovery.repository import RecoveryRepository, RecoveryScanner
from credit_harness.recovery.service import SideEffectRecoveryCoordinator
from credit_harness.recovery.prepared import PreparedEffectResumer
from credit_harness.recovery.identity import reconstruct_identity
from credit_harness.recovery.tables import EffectRecoveryStateRow as State, EffectRecoveryAttemptRow as Attempt
from credit_harness.recovery.case import CaseRecoveryCoordinator


class FakeResolver:
    """Explicit synthetic status-contract faults, no dispatch method."""
    def __init__(self, clock, status=L.INDETERMINATE, *, strong_absence=False, change=None):
        self.clock, self.status, self.change = clock, status, change
        self.calls = []
        self.capability = RecoveryCapability(resolver_id="TEST-RESOLVER", contract_version="1",
                                             not_found_proves_no_effect=strong_absence)

    def lookup(self, correlation_id, command_identity):
        self.calls.append((correlation_id, command_identity))
        result = SideEffectLookupResult(correlation_id=correlation_id,
            command_identity_hash=digest(command_identity.model_dump(mode="json")),
            resolver_id=self.capability.resolver_id, observed_at=self.clock(), lookup_status=self.status,
            external_effect_ref="REMOTE-EFFECT-001" if self.status in (L.FOUND_APPLIED, L.FOUND_ACCEPTED) else None,
            no_effect_confirmed=self.status == L.FOUND_FAILED_NO_EFFECT)
        return self.change(result) if self.change else result


def configure(x, resolver=None, policy=None, worker_id=None):
    resolver = resolver or SyntheticEffectStatusResolver(x.engine, x.f.cases.tenant_id, clock=lambda: x.clock.now)
    repo = RecoveryRepository(SQLApprovalStore(x.f.cases), resolver.capability,
                              policy=policy or RecoveryPolicy(grace_seconds=0, backoff_seconds=(1, 2, 3)))
    runtime = RemediationExecutionService(x.f.reader, x.signer, x.adapter, clock=lambda: x.clock.now)
    x.resolver, x.repo = resolver, repo
    x.recovery = SideEffectRecoveryCoordinator(repo, resolver, prepared_resumer=PreparedEffectResumer(runtime),
                                               clock=lambda: x.clock.now, worker_id=worker_id)
    return x.recovery


@pytest.fixture
def recovering(setup_effect):
    def make(state=S.UNKNOWN, *, apply=True, policy=None):
        x = setup_effect()
        x.cap = issue(x)
        x.effect_id = effect_key(x.cap.payload)
        if state in (S.PREPARED, S.DISPATCHED):
            x.store.prepare(x.cap.payload, lambda: x.clock.now)
            if state == S.DISPATCHED:
                ledger = x.store.dispatch_prepared(x.cap.payload, lambda: x.clock.now)
                if apply:
                    x.adapter.dispatch(DomainCommandBuilder().build(x.intent, x.cap.payload), ledger.dispatch_correlation_id)
        else:
            def dispatch(command, correlation):
                if state == S.ACCEPTED:
                    return SideEffectReceipt(correlation_id=correlation, outcome=ReceiptOutcome.ACCEPTED,
                        external_effect_ref=None, observed_at=x.clock.now)
                if apply:
                    x.adapter.dispatch(command, correlation)
                raise TimeoutError("synthetic lost response")
            x.runtime.adapter = SimpleNamespace(dispatch=dispatch)
            x.runtime.execute(x.cap)
        configure(x, policy=policy)
        return x
    return make


@pytest.fixture
def orphan(harness):
    cases, evidence, executor, sid, _ = harness()
    client = executor._client_for_case(CASE)
    original = client.observe
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("synthetic read response lost")
    client.observe = Mock(side_effect=lost)
    with pytest.raises(TimeoutError):
        execute(executor, T.MESSAGES)
    with Session(cases.engine) as session:
        call = session.scalar(select(CaseCallRow))
        call_id = call.call_id
    return SimpleNamespace(cases=cases, evidence=evidence, executor=executor, client=client,
        engine=cases.engine, call_id=call_id, recovery=ReadObservationRecoveryService(evidence))


def test_response_lost_after_observation_persisted_recovers_observation(orphan):
    assert not orphan.evidence.list(CASE)
    result = orphan.recovery.recover(CASE, orphan.call_id)
    assert result.action_taken == D.OBSERVATION_RECOVERED and result.recovered_evidence_refs
    with Session(orphan.engine) as session:
        call = session.get(CaseCallRow, orphan.call_id)
        assert call.state == CallState.OBSERVED and call.observation_id
    assert all(orphan.evidence.get_raw_observation(ref, case_id=CASE).observation_id == call.observation_id
               for ref in result.recovered_evidence_refs)


def test_recovered_observation_generates_evidence_once(orphan):
    first = orphan.recovery.recover(CASE, orphan.call_id)
    evidence = orphan.evidence.list(CASE)
    for _ in range(10):
        result = orphan.recovery.recover(CASE, orphan.call_id)
        assert result.action_taken == D.OBSERVATION_ALREADY_PUBLISHED
        assert result.recovered_evidence_refs == first.recovered_evidence_refs
    assert orphan.evidence.list(CASE) == evidence


def test_recovery_does_not_consume_second_tool_budget(orphan):
    before = orphan.cases.get(CASE).budget
    orphan.recovery.recover(CASE, orphan.call_id)
    assert orphan.cases.get(CASE).budget == before


def test_recovery_does_not_redispatch_read_tool(orphan):
    orphan.recovery.recover(CASE, orphan.call_id)
    orphan.client.observe.assert_called_once()


def test_wrong_dispatch_correlation_cannot_recover_foreign_observation(orphan):
    with Session(orphan.engine) as session, session.begin():
        session.get(CaseCallRow, orphan.call_id).dispatch_correlation_id = str(uuid4())
    result = orphan.recovery.recover(CASE, orphan.call_id)
    assert result.action_taken == D.OBSERVATION_NOT_FOUND and not orphan.evidence.list(CASE)


def test_ambiguous_observation_fails_safe(orphan):
    with Session(orphan.engine) as session, session.begin():
        row = session.scalar(select(ObservationRow))
        oid = str(uuid4())
        payload = {**row.observation, "observation_id": oid}
        session.add(ObservationRow(id=oid, simulation_id=row.simulation_id, grant_hash=row.grant_hash,
            tool=row.tool, request=row.request, observation=payload, content_hash=json_hash(payload),
            dispatch_correlation_id=row.dispatch_correlation_id))
    result = orphan.recovery.recover(CASE, orphan.call_id)
    assert result.action_taken == D.AMBIGUOUS_OBSERVATION and result.requires_escalation
    assert not orphan.evidence.list(CASE)


def test_missing_observation_does_not_fake_timeout_evidence(harness):
    cases, evidence, _, _, _ = harness()
    _, call_id = cases.reserve_call(CASE, T.PAYMENT, Q)
    cases.mark_error(CASE, call_id)
    result = ReadObservationRecoveryService(evidence).recover(CASE, call_id)
    assert result.action_taken == D.OBSERVATION_NOT_FOUND and not evidence.list(CASE)


@pytest.mark.parametrize("field,value", [("content_hash", "0" * 64), ("grant_hash", "0" * 64), ("tool", T.PAYMENT.value)])
def test_read_recovery_revalidates_provenance(orphan, field, value):
    with Session(orphan.engine) as session, session.begin():
        setattr(session.scalar(select(ObservationRow)), field, value)
    result = orphan.recovery.recover(CASE, orphan.call_id)
    assert result.action_taken == D.PROVENANCE_INVALID and not orphan.evidence.list(CASE)


def test_prepared_can_resume_when_authorization_still_valid(recovering):
    x = recovering(S.PREPARED)
    result = x.recovery.recover(x.effect_id)
    assert result.action_taken == O.RESUMED_PREPARED and result.after_status == S.APPLIED
    assert count(x, SyntheticEffectRow) == 1


def test_prepared_resume_uses_same_effect_and_correlation(recovering):
    x = recovering(S.PREPARED)
    before = x.store.get_ledger(x.effect_id)
    x.recovery.recover(x.effect_id)
    after = x.store.get_ledger(x.effect_id)
    assert (after.effect_id, after.idempotency_key, after.capability_id, after.dispatch_correlation_id) == (
        before.effect_id, before.idempotency_key, before.capability_id, before.dispatch_correlation_id)
    assert after.attempt_count == 1 and count(x, EffectRow) == 1


@pytest.mark.parametrize("change", ["expired_capability", "expired_approval", "revoked_approval", "evidence", "policy", "catalog", "auth_policy"])
def test_prepared_resume_rechecks_all_authorization(recovering, monkeypatch, change):
    x = recovering(S.PREPARED)
    if change == "expired_capability":
        x.clock.now = x.cap.payload.expires_at
    elif change == "expired_approval":
        from credit_harness.authorization.tables import ApprovalRow
        with Session(x.engine) as session, session.begin():
            row = session.get(ApprovalRow, x.cap.payload.approval_id)
            row.payload = {**row.payload, "expires_at": x.clock.now.isoformat()}
    elif change == "revoked_approval":
        x.auth.decide_approval(ApprovalDecision(approval_id=x.cap.payload.approval_id,
            decision=P.REVOKED, actor_ref="OPERATOR-002"))
    elif change == "evidence":
        execute(x.f.executor, T.GUARANTEE)
    else:
        from credit_harness.remediation import models as rm
        from credit_harness.authorization import models as am
        module, field = {"policy": (rm, "REMEDIATION_POLICY_VERSION"), "catalog": (rm, "REMEDIATION_CATALOG_VERSION"),
                         "auth_policy": (am, "AUTHORIZATION_POLICY_VERSION")}[change]
        monkeypatch.setattr(module, field, "new")
    result = x.recovery.recover(x.effect_id)
    assert result.action_taken == O.BLOCKED_PREPARED and result.after_status == S.PREPARED
    assert x.store.get_ledger(x.effect_id).attempt_count == 0 and count(x, SyntheticEffectRow) == 0


def test_legacy_prepared_without_original_signature_never_mints_capability(recovering):
    x = recovering(S.PREPARED)
    with Session(x.engine) as session, session.begin():
        session.delete(session.get(CapabilitySignatureRow, x.cap.payload.capability_id))
    assert x.recovery.recover(x.effect_id).action_taken == O.BLOCKED_PREPARED


@pytest.mark.parametrize("state", [S.DISPATCHED, S.UNKNOWN, S.ACCEPTED])
def test_dispatched_unknown_accepted_never_redispatch(recovering, state):
    x = recovering(state)
    x.adapter.dispatch = Mock(side_effect=AssertionError("lookup only"))
    result = x.recovery.recover(x.effect_id)
    x.adapter.dispatch.assert_not_called()
    assert result.after_status == (S.APPLIED if state != S.ACCEPTED else S.UNKNOWN)
    assert x.store.get_ledger(x.effect_id).attempt_count == 1


@pytest.mark.parametrize("state", [S.DISPATCHED, S.UNKNOWN, S.ACCEPTED])
@pytest.mark.parametrize("lookup,expected,strong", [
    (L.FOUND_APPLIED, S.APPLIED, False), (L.FOUND_ACCEPTED, S.ACCEPTED, False),
    (L.FOUND_FAILED_NO_EFFECT, S.FAILED_CONFIRMED, False), (L.INDETERMINATE, S.UNKNOWN, False),
    (L.NOT_FOUND, S.UNKNOWN, False), (L.NOT_FOUND, S.FAILED_CONFIRMED, True),
])
def test_recovery_lookup_contract_matrix(recovering, state, lookup, expected, strong):
    x = recovering(state, apply=False)
    resolver = FakeResolver(lambda: x.clock.now, lookup, strong_absence=strong)
    configure(x, resolver)
    before = x.f.evidence.list(CASE)
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == expected
    assert x.f.evidence.list(CASE) == before and count(x, SyntheticEffectRow) == 0
    assert len(resolver.calls) == 1 and result.proof_ref
    attempt = x.repo.attempts(x.effect_id)[0]
    assert attempt.proof_hash == result.proof_ref and attempt.lookup_result.lookup_status == lookup


@pytest.mark.parametrize("change", ["expiry", "revoked", "policy", "catalog", "auth_policy"])
def test_expiry_revocation_policy_upgrade_do_not_block_unknown_lookup(recovering, monkeypatch, change):
    x = recovering()
    if change == "expiry":
        x.clock.now = x.cap.payload.expires_at + timedelta(seconds=1)
    elif change == "revoked":
        x.auth.decide_approval(ApprovalDecision(approval_id=x.cap.payload.approval_id,
            decision=P.REVOKED, actor_ref="OPERATOR-002"))
    else:
        from credit_harness.remediation import models as rm
        from credit_harness.authorization import models as am
        module, field = {"policy": (rm, "REMEDIATION_POLICY_VERSION"), "catalog": (rm, "REMEDIATION_CATALOG_VERSION"),
                         "auth_policy": (am, "AUTHORIZATION_POLICY_VERSION")}[change]
        monkeypatch.setattr(module, field, "new")
    assert x.recovery.recover(x.effect_id).after_status == S.APPLIED


def test_unknown_repeated_lookup_is_bounded(recovering):
    x = recovering(apply=False)
    resolver = FakeResolver(lambda: x.clock.now)
    configure(x, resolver)
    for n in range(3):
        result = x.recovery.recover(x.effect_id)
        assert result.after_status == S.UNKNOWN
        if n < 2:
            assert x.recovery.recover(x.effect_id).action_taken == O.BUSY_OR_NOT_DUE
            x.clock.now += timedelta(seconds=n + 1)
    assert result.requires_escalation and result.action_taken == O.ESCALATION_REQUIRED
    x.clock.now += timedelta(days=1)
    assert x.recovery.recover(x.effect_id).requires_escalation and len(resolver.calls) == 3
    assert len(x.repo.attempts(x.effect_id)) == 3 and x.store.get_ledger(x.effect_id).attempt_count == 1
    assert not RecoveryScanner(x.repo, lambda: x.clock.now).list_recoverable_effects()


def test_recovery_scanner_respects_grace_period(recovering):
    x = recovering(policy=RecoveryPolicy(grace_seconds=30))
    scanner = RecoveryScanner(x.repo, lambda: x.clock.now)
    assert not scanner.list_recoverable_effects()
    assert x.recovery.recover(x.effect_id).action_taken == O.BUSY_OR_NOT_DUE
    x.clock.now += timedelta(seconds=30)
    assert scanner.list_recoverable_effects() == (x.effect_id,)


@pytest.mark.parametrize("state", [S.PREPARED, S.UNKNOWN])
def test_two_workers_do_not_duplicate_recovery(recovering, state):
    x = recovering(state)
    barrier, entered, release = Barrier(2), Event(), Event()
    if state == S.UNKNOWN:
        base_lookup = x.resolver.lookup
        def slow(*args):
            entered.set()
            assert release.wait(15)
            return base_lookup(*args)
        x.resolver.lookup = Mock(side_effect=slow)
    def worker(i):
        repo = RecoveryRepository(SQLApprovalStore(x.f.cases), x.resolver.capability, policy=x.repo.policy)
        runtime = RemediationExecutionService(x.f.reader, x.signer, x.adapter, clock=lambda: x.clock.now)
        service = SideEffectRecoveryCoordinator(repo, x.resolver, prepared_resumer=PreparedEffectResumer(runtime),
                                               clock=lambda: x.clock.now, worker_id=f"WORKER-{i}")
        barrier.wait(10)
        result = service.recover(x.effect_id)
        if result.action_taken in (O.BUSY_OR_NOT_DUE, O.NOTHING_TO_DO):
            release.set()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [1, 2]))
    assert x.store.get_ledger(x.effect_id).status == S.APPLIED
    assert len(x.repo.attempts(x.effect_id)) == 1 and count(x, SyntheticEffectRow) == 1
    if state == S.UNKNOWN:
        assert x.resolver.lookup.call_count == 1
    assert any(r.after_status == S.APPLIED for r in results)


def test_recovery_lease_can_be_taken_after_expiry(recovering):
    x = recovering()
    old = x.repo.claim(x.effect_id, "DEAD-WORKER", x.clock.now)
    assert old is not None
    assert x.recovery.recover(x.effect_id).action_taken == O.BUSY_OR_NOT_DUE
    x.clock.now += timedelta(seconds=x.repo.policy.lease_seconds)
    assert x.recovery.recover(x.effect_id).after_status == S.APPLIED
    with pytest.raises(RecoveryError):
        x.repo.recover_transition(old, x.clock.now)
    assert len(x.repo.attempts(x.effect_id)) == 2


def test_recovery_transition_is_cas_safe(recovering, monkeypatch):
    x = recovering(S.DISPATCHED)
    capture = x.recovery.lookup.capture
    def original_worker_wins(claim):
        capture(claim)
        ledger = x.store.get_ledger(x.effect_id)
        receipt = SideEffectReceipt(correlation_id=ledger.dispatch_correlation_id, outcome=ReceiptOutcome.APPLIED,
            external_effect_ref="ORIGINAL-RECEIPT", observed_at=x.clock.now)
        x.store.transition(x.effect_id, S.DISPATCHED, S.APPLIED, x.clock.now, receipt=receipt)
    monkeypatch.setattr(x.recovery.lookup, "capture", original_worker_wins)
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == S.APPLIED and result.action_taken == O.NOTHING_TO_DO
    assert result.failure_code == F.RECOVERY_STALE
    assert x.store.get_ledger(x.effect_id).external_effect_ref == "ORIGINAL-RECEIPT"


@pytest.mark.parametrize("field,value", [("correlation_id", "WRONG-CORRELATION"),
    ("command_identity_hash", "0" * 64), ("resolver_id", "FOREIGN-RESOLVER")])
def test_wrong_lookup_identity_fails_safe(recovering, field, value):
    x = recovering()
    configure(x, FakeResolver(lambda: x.clock.now, L.FOUND_APPLIED, change=lambda r: r.model_copy(update={field: value})))
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == S.UNKNOWN and result.requires_escalation
    assert result.failure_code == F.LOOKUP_PROOF_CONFLICT


def test_conflicting_external_effect_ref_fails_safe(recovering):
    x = recovering(S.DISPATCHED, apply=False)
    ledger = x.store.get_ledger(x.effect_id)
    x.store.transition(x.effect_id, S.DISPATCHED, S.ACCEPTED, x.clock.now,
        receipt=SideEffectReceipt(correlation_id=ledger.dispatch_correlation_id, outcome=ReceiptOutcome.ACCEPTED,
            external_effect_ref="ORIGINAL-REF", observed_at=x.clock.now))
    configure(x, FakeResolver(lambda: x.clock.now, L.FOUND_APPLIED))
    result = x.recovery.recover(x.effect_id)
    assert result.failure_code == F.LOOKUP_PROOF_CONFLICT and result.requires_escalation
    assert x.store.get_ledger(x.effect_id).external_effect_ref == "ORIGINAL-REF"


def test_ordinary_transition_cannot_forge_recovery_proof(recovering):
    x = recovering()
    with pytest.raises(AuthorizationError):
        x.store.transition(x.effect_id, S.UNKNOWN, S.APPLIED, x.clock.now)
    claim = x.repo.claim(x.effect_id, "WORKER", x.clock.now)
    with pytest.raises(RecoveryError):
        x.repo.recover_transition(claim, x.clock.now)


def test_foreign_tenant_cannot_scan_or_recover_effect(recovering):
    x = recovering()
    foreign = RecoveryRepository(SQLApprovalStore(CaseRepository(x.engine, "FOREIGN")), x.resolver.capability,
                                 policy=x.repo.policy)
    assert foreign.list_recoverable_effects(x.clock.now) == ()
    with pytest.raises((AuthorizationError, CaseAccessError)):
        foreign.claim(x.effect_id, "FOREIGN-WORKER", x.clock.now)


def test_write_fence_blocks_new_capability_for_unresolved_effect(recovering):
    x = recovering(apply=False)
    new_intent = preview(x.f).intent
    approval = x.auth.request_approval(new_intent)
    x.auth.decide_approval(ApprovalDecision(approval_id=approval.approval_id, decision=P.APPROVED, actor_ref="OPERATOR-001"))
    fails(C.WRITE_FENCED_BY_UNRESOLVED_EFFECT,
          lambda: x.auth.issue_capability(new_intent, approval_id=approval.approval_id))
    inspection = CaseRecoveryCoordinator(x.f.cases, x.f.evidence).before_investigation(CASE)
    assert inspection.unresolved_effect_ids == (x.effect_id,)
    # Investigation remains possible without authorizing another write.
    execute(x.f.executor, T.PAYMENT)


def test_s6_timeout_recovery_then_explicit_read(recovering):
    x = recovering()
    before = x.f.evidence.list(CASE)
    original = x.store.get_ledger(x.effect_id)
    assert world_state(x.engine, x.f.sid).messages[0].consume_status == ConsumeStatus.CONSUMED
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == S.APPLIED and x.f.evidence.list(CASE) == before
    assert x.store.get_ledger(x.effect_id).dispatch_correlation_id == original.dispatch_correlation_id
    execute(x.f.executor, T.MESSAGES)
    assert any(e.claim_type == ClaimType.MESSAGE_CONSUME_STATUS and e.value == "CONSUMED" for e in x.f.evidence.list(CASE))
    assert x.f.cases.get(CASE).status.value == "INVESTIGATING"
    assert {r.correlation_id for r in x.repo.audit(x.effect_id)} == {original.dispatch_correlation_id}


def test_restarted_agent_recovers_read_before_fresh_planner(setup_agent):
    from credit_harness.agent.models import AgentRunConfig
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    client = runtime.executor._client_for_case(CASE)
    original = client.observe
    def lost(*args, **kwargs):
        original(*args, **kwargs)
        raise TimeoutError("lost")
    client.observe = Mock(side_effect=lost)
    first = runtime.run(CASE)
    assert not runtime.evidence.list(CASE)
    client.observe = original
    seen = []
    original_plan = runtime.planner.plan
    def plan(snapshot):
        assert runtime.evidence.list(CASE)  # recovered before model input construction
        seen.append(snapshot.snapshot_id)
        return original_plan(snapshot)
    runtime.planner.plan = plan
    second = runtime.run(CASE)
    assert seen and first.run_id != second.run_id
    assert second.initial_snapshot_id != first.final_snapshot_id


def test_synthetic_lookup_has_no_side_effects(recovering):
    x = recovering()
    ledger = x.store.get_ledger(x.effect_id)
    identity, _, _ = reconstruct_identity(x.store, ledger)
    before = world_state(x.engine, x.f.sid)
    for _ in range(5):
        assert x.resolver.lookup(ledger.dispatch_correlation_id, identity).lookup_status == L.FOUND_APPLIED
    assert world_state(x.engine, x.f.sid) == before and count(x, SyntheticEffectRow) == 1


@pytest.mark.parametrize("error,code", [(TimeoutError("secret-token"), F.LOOKUP_TIMEOUT),
    (ConnectionError("https://secret.invalid"), F.LOOKUP_TRANSPORT_ERROR)])
def test_lookup_failure_audit_uses_only_enum(recovering, error, code):
    x = recovering()
    x.resolver.lookup = Mock(side_effect=error)
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == S.UNKNOWN and result.failure_code == code
    data = json.dumps([a.model_dump(mode="json") for a in x.repo.audit(x.effect_id)])
    assert "secret" not in data and code.value in data


def test_recovery_static_boundaries(recovering):
    for path in Path("src/credit_harness/recovery").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not any(s in (node.module or "") for s in ("openai", "planner.model", "remediation.model", "simulator"))
            if isinstance(node, ast.Name):
                assert node.id not in ("WorldState", "GroundTruth", "SimulatorAdmin", "Evaluator")
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("observe", "sleep", "dispatch")
                if node.attr in ("dispatch_prepared", "_complete_dispatch"):
                    assert path.name == "prepared.py"
    x = recovering()
    resolver = FakeResolver(lambda: x.clock.now)
    resolver.capability = resolver.capability.model_copy(update={"supports_lookup": False})
    configure(x, resolver)
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == S.UNKNOWN and result.requires_escalation and not resolver.calls


@pytest.mark.parametrize("point,expected", [("A", None), ("B", S.APPLIED), ("C", S.UNKNOWN),
    ("D", S.APPLIED), ("E", S.APPLIED), ("F", S.APPLIED)])
def test_crash_matrix_preserves_execution_identity(setup_effect, monkeypatch, point, expected):
    x = setup_effect()
    cap = issue(x)
    effect_id = effect_key(cap.payload)
    class Crash(BaseException):
        pass
    if point == "A":
        configure(x)
        assert not x.repo.list_recoverable_effects(x.clock.now)
        assert count(x, EffectRow) == 0
        return  # old planner/capability presence never auto-starts an effect
    original_transition = x.store.transition
    if point == "B":
        monkeypatch.setattr(x.store, "dispatch_prepared", Mock(side_effect=Crash))
    elif point == "C":
        monkeypatch.setattr(x.runtime, "_complete_dispatch", Mock(side_effect=Crash))
    elif point == "D":
        def applied_then_crash(command, correlation):
            x.adapter.dispatch(command, correlation)
            raise Crash()
        x.runtime.adapter = SimpleNamespace(dispatch=applied_then_crash)
    elif point in ("E", "F"):
        def transition(*args, **kwargs):
            if point == "F":
                original_transition(*args, **kwargs)
            raise Crash()
        monkeypatch.setattr(x.store, "transition", transition)
    with pytest.raises(Crash):
        x.runtime.execute(cap)
    before = SQLApprovalStore(x.f.cases).get_ledger(effect_id)
    configure(x)  # new worker, repositories and execution service
    result = x.recovery.recover(effect_id)
    after = SQLApprovalStore(x.f.cases).get_ledger(effect_id)
    assert after.status == expected and after.effect_id == before.effect_id
    assert after.dispatch_correlation_id == before.dispatch_correlation_id and after.attempt_count == 1
    assert count(x, SyntheticEffectRow) == (0 if point == "C" else 1)
    if point == "F":
        assert result.action_taken == O.NOTHING_TO_DO


def test_two_workers_publish_orphan_evidence_once(orphan):
    barrier = Barrier(2)
    def worker(_):
        from credit_harness.evidence.repository import EvidenceRepository
        recovery = ReadObservationRecoveryService(EvidenceRepository(CaseRepository(orphan.engine, "demo")))
        barrier.wait(10)
        return recovery.recover(CASE, orphan.call_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [1, 2]))
    assert {r.action_taken for r in results} == {D.OBSERVATION_RECOVERED, D.OBSERVATION_ALREADY_PUBLISHED}
    assert results[0].recovered_evidence_refs == results[1].recovered_evidence_refs
    assert orphan.cases.get(CASE).budget.used_tool_calls == 1


def test_expired_lease_cannot_resume_prepared(recovering):
    x = recovering(S.PREPARED)
    claim = x.repo.claim(x.effect_id, "OLD-WORKER", x.clock.now)
    x.clock.now += timedelta(seconds=x.repo.policy.lease_seconds)
    with pytest.raises(AuthorizationError):
        x.recovery.prepared_resumer.resume(claim)
    assert x.store.get_ledger(x.effect_id).status == S.PREPARED
    assert x.recovery.recover(x.effect_id).after_status == S.APPLIED


def test_expired_lookup_proof_fails_closed(recovering):
    x = recovering()
    configure(x, FakeResolver(lambda: x.clock.now, L.FOUND_APPLIED,
        change=lambda r: r.model_copy(update={"observed_at": x.clock.now - timedelta(seconds=31)})))
    result = x.recovery.recover(x.effect_id)
    assert result.after_status == S.UNKNOWN and result.failure_code == F.INVALID_LOOKUP_RECEIPT


def test_durable_checkpoint_is_not_an_execution_ticket(setup_agent):
    from credit_harness.agent.models import AgentRunConfig
    from credit_harness.recovery.checkpoint import AgentCheckpointStore
    runtime = setup_agent(config=AgentRunConfig(max_turns=1))
    result = runtime.run(CASE)
    records = AgentCheckpointStore(runtime.cases).list(CASE)
    assert len(records) == 1 and records[0].run_id == result.run_id
    assert records[0].last_completed_turn == 1 and records[0].stop_status == result.status
    assert records[0].last_call_id == result.turns[0].tool_call_id
    second = runtime.run(CASE)
    assert second.run_id != result.run_id and len(AgentCheckpointStore(runtime.cases).list(CASE)) == 2


@pytest.mark.parametrize("scenario,expected", [("read-orphan", D.OBSERVATION_RECOVERED),
    ("effect-timeout", S.APPLIED), ("worker-crash-after-prepared", S.APPLIED), ("worker-crash-after-dispatch", S.UNKNOWN)])
def test_recovery_demo(engine, monkeypatch, scenario, expected):
    monkeypatch.setenv("CAPABILITY_SIGNING_SECRET", "synthetic-recovery-demo-key-not-for-production")
    from scripts.demo_recovery import run_demo
    result = run_demo(engine, scenario)
    assert result["recovery"]["after_status"] == expected
    assert result["case_status"] == "INVESTIGATING"


def test_read_publication_and_recovery_metadata_are_atomic(orphan, monkeypatch):
    original = orphan.evidence._record_call
    class Crash(BaseException):
        pass
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()
    monkeypatch.setattr(orphan.evidence, "_record_call", crash)
    before = orphan.cases.get(CASE)
    with pytest.raises(Crash):
        orphan.recovery.recover(CASE, orphan.call_id)
    assert not orphan.evidence.list(CASE) and orphan.cases.get(CASE) == before
    monkeypatch.setattr(orphan.evidence, "_record_call", original)
    assert orphan.recovery.recover(CASE, orphan.call_id).action_taken == D.OBSERVATION_RECOVERED


def test_same_external_effect_ref_is_stable(recovering):
    x = recovering(S.DISPATCHED, apply=False)
    ledger = x.store.get_ledger(x.effect_id)
    x.store.transition(x.effect_id, S.DISPATCHED, S.ACCEPTED, x.clock.now, receipt=SideEffectReceipt(
        correlation_id=ledger.dispatch_correlation_id, outcome=ReceiptOutcome.ACCEPTED,
        external_effect_ref="REMOTE-EFFECT-001", observed_at=x.clock.now))
    configure(x, FakeResolver(lambda: x.clock.now, L.FOUND_APPLIED))
    assert x.recovery.recover(x.effect_id).after_status == S.APPLIED
    assert x.store.get_ledger(x.effect_id).external_effect_ref == "REMOTE-EFFECT-001"


def test_write_fence_allows_different_effect_identity(recovering):
    from credit_harness.remediation.models import RemediationActionType
    x = recovering(apply=False)
    review = preview(x.f, RemediationActionType.REQUEST_OPERATOR_REVIEW).intent
    cap = x.auth.issue_capability(review)
    assert cap.payload.action_type == RemediationActionType.REQUEST_OPERATOR_REVIEW
    assert x.store.get_ledger(x.effect_id).status == S.UNKNOWN
