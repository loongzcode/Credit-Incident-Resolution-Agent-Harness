"""Real DB/adapter tests: no LLM, real PII, money clients or network calls."""
import ast
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from tests.test_case_evidence import harness, execute
from tests.test_remediation import prepared, preview, business_state, CASE
from tests.support.remediation_fixture import install_deployment_observation
from tests.support.inspector import world_state
from credit_harness.domain.enums import ScenarioId, ToolName as T, ConsumeStatus, DeliveryStatus
from credit_harness.evidence.models import ClaimType
from credit_harness.context.budget import digest
from credit_harness.remediation.models import RemediationActionType as A, ActionRiskLevel as L, EffectScope
from credit_harness.authorization.models import (
    AuthorizationError, AuthorizationCode as C, ApprovalStatus as P, ApprovalDecision,
    EffectStatus as S, ReceiptOutcome, SideEffectReceipt, ExecutionCapability,
)
from credit_harness.authorization.commands import DomainCommandBuilder, COMMAND_ADAPTER, effect_key
from credit_harness.authorization.signing import HMACCapabilitySigner
from credit_harness.authorization.service import RemediationAuthorizationService, RemediationExecutionService
from credit_harness.authorization.store import SQLApprovalStore
from credit_harness.authorization.tables import create_authorization_schema, EffectRow, CapabilityRow
from credit_harness.adapters.synthetic_remediation import (
    SyntheticRemediationAdapter, SyntheticTaskRow, SyntheticEffectRow, create_synthetic_effect_schema,
)


@pytest.fixture
def setup_effect(prepared, monkeypatch, engine):
    def make(scenario=ScenarioId.S6, action=A.REPLAY_CALLBACK_CONSUMPTION, ready=True):
        if ready:
            install_deployment_observation(monkeypatch)
        f = prepared(scenario)
        create_authorization_schema(engine)
        create_synthetic_effect_schema(engine)
        monkeypatch.setenv("CAPABILITY_SIGNING_SECRET", "synthetic-test-secret-not-for-production-0001")
        clock = SimpleNamespace(now=datetime.now(timezone.utc))
        signer = HMACCapabilitySigner()
        store = SQLApprovalStore(f.cases)
        adapter = SyntheticRemediationAdapter(engine, tenant_id=f.cases.tenant_id)
        auth = RemediationAuthorizationService(f.reader, signer, clock=lambda: clock.now, store=store)
        runtime = RemediationExecutionService(f.reader, signer, adapter, clock=lambda: clock.now, store=store)
        intent = preview(f, action).intent
        assert intent is not None
        return SimpleNamespace(f=f, clock=clock, signer=signer, store=store, adapter=adapter,
                               auth=auth, runtime=runtime, intent=intent, engine=engine)
    return make


def approve(x, decision=P.APPROVED):
    approval = x.auth.request_approval(x.intent)
    return x.auth.decide_approval(ApprovalDecision(approval_id=approval.approval_id,
        decision=decision, actor_ref="OPERATOR-001"))


def issue(x):
    approval_id = approve(x).approval_id if x.intent.requires_approval else None
    return x.auth.issue_capability(x.intent, approval_id=approval_id)


def count(x, table):
    with Session(x.engine) as session:
        return session.scalar(select(func.count()).select_from(table))


def fails(code, call):
    with pytest.raises(AuthorizationError) as caught:
        call()
    assert caught.value.code == code


def test_unsigned_capability_rejected(setup_effect):
    x = setup_effect()
    cap = issue(x)
    fails(C.UNSIGNED_CAPABILITY, lambda: x.runtime.execute(cap.payload))
    fails(C.UNSIGNED_CAPABILITY, lambda: x.runtime.execute(cap.model_dump()))
    assert count(x, EffectRow) == 0


def test_modified_capability_rejected(setup_effect):
    x = setup_effect()
    cap = issue(x)
    altered = cap.model_copy(update={"payload": cap.payload.model_copy(update={"case_id": "CASE-OTHER"})})
    fails(C.INVALID_CAPABILITY, lambda: x.runtime.execute(altered))
    assert count(x, EffectRow) == 0


@pytest.mark.parametrize("field,value,code", [
    ("case_id", "CASE-OTHER", C.CAPABILITY_SCOPE_MISMATCH),
    ("internal_order_id", "ORDER-OTHER", C.CAPABILITY_SCOPE_MISMATCH),
    ("target_hash", "0" * 64, C.CAPABILITY_TARGET_MISMATCH),
    ("payload_hash", "0" * 64, C.CAPABILITY_PAYLOAD_MISMATCH),
], ids=["wrong_case", "wrong_order", "wrong_target", "wrong_payload"])
def test_command_capability_binding(setup_effect, field, value, code):
    x = setup_effect()
    cap = issue(x).payload.model_copy(update={field: value})
    fails(code, lambda: DomainCommandBuilder().build(x.intent, cap))
    # Even a valid signature cannot override the durable issuance binding.
    with pytest.raises(AuthorizationError):
        x.runtime.execute(x.signer.sign(cap))
    assert count(x, EffectRow) == 0


def test_expired_capability_rejected(setup_effect):
    x = setup_effect()
    cap = issue(x)
    x.clock.now = cap.payload.expires_at
    fails(C.CAPABILITY_EXPIRED, lambda: x.runtime.execute(cap))


@pytest.mark.parametrize("field", ["AUTHORIZATION_POLICY_VERSION", "REMEDIATION_POLICY_VERSION", "REMEDIATION_CATALOG_VERSION"])
def test_old_policy_capability_rejected(setup_effect, monkeypatch, field):
    x = setup_effect()
    cap = issue(x)
    from credit_harness.authorization import models as av
    from credit_harness.remediation import models as rv
    monkeypatch.setattr(av if field.startswith("AUTHORIZATION") else rv, field, "changed")
    fails(C.STALE_AUTHORIZATION, lambda: x.runtime.execute(cap))


def test_l2_requires_approval(setup_effect):
    x = setup_effect()
    fails(C.APPROVAL_REQUIRED, lambda: x.auth.issue_capability(x.intent))
    pending = x.auth.request_approval(x.intent)
    fails(C.APPROVAL_NOT_APPROVED, lambda: x.auth.issue_capability(x.intent, approval_id=pending.approval_id))
    assert count(x, CapabilityRow) == count(x, EffectRow) == 0


@pytest.mark.parametrize("action", [A.CREATE_RECONCILIATION_TASK, A.REQUEST_OPERATOR_REVIEW])
def test_l1_does_not_require_human_approval(setup_effect, action):
    x = setup_effect(action=action)
    cap = issue(x)
    assert cap.payload.approval_id is None and cap.payload.max_effects == 1
    first = x.runtime.execute(cap)
    second = x.runtime.execute(cap)
    assert first.ledger.status == S.APPLIED and second.idempotent_replay
    assert count(x, SyntheticTaskRow) == count(x, SyntheticEffectRow) == 1


def test_rejected_approval_cannot_issue_capability(setup_effect):
    x = setup_effect()
    before = business_state(x.f)
    rejected = approve(x, P.REJECTED)
    fails(C.APPROVAL_NOT_APPROVED, lambda: x.auth.issue_capability(x.intent, approval_id=rejected.approval_id))
    assert business_state(x.f) == before and count(x, EffectRow) == count(x, CapabilityRow) == 0


def test_expired_approval_cannot_issue_capability(setup_effect):
    x = setup_effect()
    approval = approve(x)
    x.clock.now = approval.expires_at
    assert SQLApprovalStore(x.f.cases).get_approval(approval.approval_id, x.clock.now).status == P.EXPIRED
    fails(C.APPROVAL_EXPIRED, lambda: x.auth.issue_capability(x.intent, approval_id=approval.approval_id))


def test_revoked_approval_invalidates_capability(setup_effect):
    x = setup_effect()
    cap = issue(x)
    x.auth.decide_approval(ApprovalDecision(approval_id=cap.payload.approval_id,
        decision=P.REVOKED, actor_ref="OPERATOR-002"))
    fails(C.APPROVAL_REVOKED, lambda: x.runtime.execute(cap))
    assert SQLApprovalStore(x.f.cases).get_approval(cap.payload.approval_id, x.clock.now).actor_ref == "OPERATOR-002"
    assert count(x, EffectRow) == 0


def test_approval_bound_to_intent(setup_effect):
    x = setup_effect()
    approval = approve(x)
    other = preview(x.f, A.REQUEST_OPERATOR_REVIEW).intent
    fails(C.APPROVAL_BINDING_MISMATCH, lambda: x.auth.issue_capability(other, approval_id=approval.approval_id))


def test_approval_cannot_authorize_changed_evidence(setup_effect):
    x = setup_effect()
    approval = approve(x)
    x.f.admin.advance(x.f.sid, 1)
    execute(x.f.executor, T.PAYMENT)
    fails(C.STALE_AUTHORIZATION, lambda: x.auth.issue_capability(x.intent, approval_id=approval.approval_id))


def test_approval_cannot_authorize_other_target(setup_effect):
    x = setup_effect()
    approval = approve(x)
    changed = x.intent.model_copy(update={"target": x.intent.target.model_copy(update={"message_ref": "MSG-OTHER"})})
    changed = changed.model_copy(update={"intent_id": digest(changed.model_dump(mode="json", exclude={"intent_id"}))})
    fails(C.STALE_AUTHORIZATION, lambda: x.auth.issue_capability(changed, approval_id=approval.approval_id))


def test_same_effect_executes_once(setup_effect):
    x = setup_effect()
    cap = issue(x)
    x.runtime.adapter = Mock(wraps=x.adapter)
    result = x.runtime.execute(cap)
    duplicate = x.runtime.execute(cap)
    assert result.ledger == duplicate.ledger
    assert duplicate.idempotent_replay and not duplicate.executed_now
    assert result.ledger.attempt_count == 1 and result.ledger.status == S.APPLIED
    assert x.runtime.adapter.dispatch.call_count == count(x, EffectRow) == count(x, SyntheticEffectRow) == 1


@pytest.mark.parametrize("distinct_capabilities", [False, True])
def test_concurrent_same_effect_executes_once(setup_effect, distinct_capabilities):
    x = setup_effect()
    first = issue(x)
    second = x.auth.issue_capability(x.intent, approval_id=first.payload.approval_id) if distinct_capabilities else first
    barrier = Barrier(2)
    def worker(cap):
        # Independent repository/service instances; serialization is in SQL.
        runtime = RemediationExecutionService(x.f.reader, x.signer, x.adapter, clock=lambda: x.clock.now)
        barrier.wait(timeout=10)
        return runtime.execute(cap)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (first, second)))
    assert sum(r.executed_now for r in results) == 1
    assert {r.ledger.effect_id for r in results} == {effect_key(first.payload)}
    assert x.store.get_ledger(effect_key(first.payload)).status == S.APPLIED
    assert count(x, EffectRow) == count(x, SyntheticEffectRow) == 1


def test_same_key_different_payload_fails(setup_effect, monkeypatch):
    x = setup_effect(action=A.REQUEST_OPERATOR_REVIEW)
    from credit_harness.authorization import store
    monkeypatch.setattr(store, "effect_key", lambda cap: "a" * 64)
    x.runtime.execute(issue(x))
    other = preview(x.f, A.CREATE_RECONCILIATION_TASK).intent
    cap = x.auth.issue_capability(other)
    fails(C.IDEMPOTENCY_CONFLICT, lambda: x.runtime.execute(cap))
    assert count(x, SyntheticTaskRow) == 1


def test_same_capability_cannot_execute_two_effects(setup_effect, monkeypatch):
    x = setup_effect()
    cap = issue(x)
    x.runtime.execute(cap)
    from credit_harness.authorization import store
    monkeypatch.setattr(store, "effect_key", lambda cap: "b" * 64)
    fails(C.CAPABILITY_ALREADY_USED, lambda: x.runtime.execute(cap))
    assert count(x, EffectRow) == 1


def test_timeout_after_dispatch_becomes_unknown(setup_effect):
    x = setup_effect()
    cap = issue(x)
    before = x.f.evidence.list(CASE)
    seen = []
    def lost_response(command, correlation):
        # Proves UNKNOWN even when the real effect has already happened.
        assert x.store.get_ledger(effect_key(cap.payload)).status == S.DISPATCHED
        seen.append(correlation)
        x.adapter.dispatch(command, correlation)
        raise TimeoutError("synthetic lost response")
    x.runtime.adapter = SimpleNamespace(dispatch=lost_response)
    result = x.runtime.execute(cap)
    duplicate = x.runtime.execute(cap)
    assert result.ledger.status == S.UNKNOWN and duplicate.ledger == result.ledger
    assert seen == [result.ledger.dispatch_correlation_id]
    assert not duplicate.executed_now and duplicate.idempotent_replay
    assert x.f.evidence.list(CASE) == before
    assert world_state(x.engine, x.f.sid).messages[0].consume_status == ConsumeStatus.CONSUMED
    for prohibited in (S.FAILED_CONFIRMED, S.APPLIED, S.DISPATCHED, S.VERIFIED):
        fails(C.INVALID_TRANSITION, lambda: x.store.transition(result.ledger.effect_id, S.UNKNOWN, prohibited, x.clock.now))


@pytest.mark.parametrize("outcome,status", [(ReceiptOutcome.ACCEPTED, S.ACCEPTED),
                                           (ReceiptOutcome.FAILED_CONFIRMED, S.FAILED_CONFIRMED)])
def test_side_effect_receipt_is_not_business_truth(setup_effect, outcome, status):
    x = setup_effect()
    before = world_state(x.engine, x.f.sid)
    x.runtime.adapter = SimpleNamespace(dispatch=lambda command, correlation: SideEffectReceipt(
        correlation_id=correlation, outcome=outcome, external_effect_ref="REMOTE-001", observed_at=x.clock.now,
        no_effect_confirmed=outcome == ReceiptOutcome.FAILED_CONFIRMED))
    result = x.runtime.execute(issue(x))
    assert result.ledger.status == status and world_state(x.engine, x.f.sid) == before


@pytest.mark.parametrize("bad", [None, {"status": "SUCCESS"}, "natural language success"])
def test_invalid_receipt_is_unknown(setup_effect, bad):
    x = setup_effect()
    x.runtime.adapter = Mock()
    x.runtime.adapter.dispatch.return_value = bad
    assert x.runtime.execute(issue(x)).ledger.status == S.UNKNOWN


def test_wrong_receipt_correlation_is_unknown(setup_effect):
    x = setup_effect()
    x.runtime.adapter = SimpleNamespace(dispatch=lambda command, correlation: SideEffectReceipt(
        correlation_id="FOREIGN-CALL", outcome=ReceiptOutcome.APPLIED, external_effect_ref="REMOTE-001", observed_at=x.clock.now))
    assert x.runtime.execute(issue(x)).ledger.status == S.UNKNOWN


def test_replay_end_to_end_reads_new_evidence_without_closing_case(setup_effect):
    x = setup_effect()
    before = business_state(x.f)
    cap = issue(x)
    result = x.runtime.execute(cap)
    after = business_state(x.f)
    assert result.ledger.status == S.APPLIED and result.ledger.status != S.VERIFIED
    assert before[0].status == after[0].status and before[0].budget == after[0].budget
    assert before[1:3] == after[1:3]  # no Evidence writes or read calls
    for name in ("fund", "guarantee", "accounting", "asset", "disbursement_intent_count"):
        assert getattr(before[3], name) == getattr(after[3], name)
    assert after[3].messages[0].consume_status == ConsumeStatus.CONSUMED
    assert after[3].messages[0].message == before[3].messages[0].message
    execute(x.f.executor, T.MESSAGES)
    new = x.f.evidence.list(CASE)
    assert len(new) > len(before[1])
    assert any(e.claim_type == ClaimType.MESSAGE_CONSUME_STATUS and e.value == "CONSUMED" for e in new)
    assert x.f.cases.get(CASE).status == before[0].status
    trace = x.store.audit(CASE)
    assert [r.effect_status for r in trace if r.effect_status] == [S.PREPARED, S.DISPATCHED, S.APPLIED]
    assert all(r.intent_id == cap.payload.intent_id for r in trace)
    assert {r.correlation_id for r in trace if r.effect_id} == {result.ledger.dispatch_correlation_id}


def test_redelivery_does_not_change_payment(setup_effect):
    x = setup_effect(ScenarioId.S7, A.REDELIVER_ASSET_NOTIFICATION)
    before = world_state(x.engine, x.f.sid)
    result = x.runtime.execute(issue(x))
    after = world_state(x.engine, x.f.sid)
    assert result.ledger.status == S.APPLIED and after.fund == before.fund
    assert after.accounting == before.accounting and after.guarantee == before.guarantee
    assert after.asset_delivery.event_id == before.asset_delivery.event_id
    assert after.asset_delivery.delivery_status == DeliveryStatus.DELIVERED
    execute(x.f.executor, T.ASSET_DELIVERY)
    assert any(e.claim_type == ClaimType.ASSET_DELIVERY_STATUS and e.value == "DELIVERED" for e in x.f.evidence.list(CASE))


def test_no_remediation_noop_never_dispatches(setup_effect):
    x = setup_effect(action=A.NO_REMEDIATION)
    before = business_state(x.f)
    x.runtime.adapter = Mock(side_effect=AssertionError("no side effect"))
    cap = issue(x)
    result = x.runtime.execute(cap)
    assert result.ledger.status == S.NOOP and not result.executed_now
    assert not result.idempotent_replay and x.runtime.execute(cap).idempotent_replay
    assert business_state(x.f) == before
    x.runtime.adapter.dispatch.assert_not_called()


@pytest.mark.parametrize("point", ["prepare", "dispatch"])
def test_crash_leaves_durable_boundary_without_automatic_recovery(setup_effect, monkeypatch, point):
    x = setup_effect()
    cap = issue(x)
    class WorkerCrash(BaseException):
        pass
    if point == "prepare":
        original = x.store.prepare
        def crash(*args):
            original(*args)
            raise WorkerCrash()
        monkeypatch.setattr(x.store, "prepare", crash)
    else:
        x.runtime.adapter = SimpleNamespace(dispatch=lambda *args: (_ for _ in ()).throw(WorkerCrash()))
    with pytest.raises(WorkerCrash):
        x.runtime.execute(cap)
    resumed = RemediationExecutionService(x.f.reader, x.signer, x.adapter, clock=lambda: x.clock.now).execute(cap)
    assert resumed.ledger.status == (S.PREPARED if point == "prepare" else S.DISPATCHED)
    assert resumed.idempotent_replay and not resumed.executed_now
    assert count(x, SyntheticEffectRow) == 0


@pytest.mark.parametrize("change", ["revoke", "expire", "evidence"])
def test_prepare_to_dispatch_rechecks_authorization(setup_effect, monkeypatch, change):
    x = setup_effect()
    cap = issue(x)
    original = x.store.prepare
    def changed(*args):
        result = original(*args)
        if change == "revoke":
            x.auth.decide_approval(ApprovalDecision(approval_id=cap.payload.approval_id,
                decision=P.REVOKED, actor_ref="OPERATOR-002"))
        elif change == "expire":
            x.clock.now = cap.payload.expires_at
        else:
            execute(x.f.executor, T.GUARANTEE)
        return result
    monkeypatch.setattr(x.store, "prepare", changed)
    fails({"revoke": C.APPROVAL_REVOKED, "expire": C.CAPABILITY_EXPIRED,
           "evidence": C.STALE_AUTHORIZATION}[change], lambda: x.runtime.execute(cap))
    assert x.store.get_ledger(effect_key(cap.payload)).status == S.PREPARED
    assert count(x, SyntheticEffectRow) == 0


def test_case_cas_rejects_late_evidence_before_prepare(setup_effect, monkeypatch):
    x = setup_effect()
    cap = issue(x)
    original = x.runtime.verifier.verify
    def changed(intent):
        result = original(intent)
        execute(x.f.executor, T.GUARANTEE)
        return result
    monkeypatch.setattr(x.runtime.verifier, "verify", changed)
    fails(C.STALE_AUTHORIZATION, lambda: x.runtime.execute(cap))
    assert count(x, EffectRow) == count(x, SyntheticEffectRow) == 0


def test_applied_is_not_verified(setup_effect):
    x = setup_effect()
    result = x.runtime.execute(issue(x))
    fails(C.INVALID_TRANSITION, lambda: x.store.transition(result.ledger.effect_id, S.APPLIED, S.VERIFIED, x.clock.now))


def test_ledger_cannot_skip_dispatch_authorization_or_forge_applied(setup_effect):
    x = setup_effect()
    cap = issue(x)
    ledger, _ = x.store.prepare(cap.payload, lambda: x.clock.now)
    fails(C.INVALID_TRANSITION, lambda: x.store.transition(ledger.effect_id, S.PREPARED, S.DISPATCHED, x.clock.now))
    x.store.dispatch_prepared(cap.payload, lambda: x.clock.now)
    fails(C.INVALID_TRANSITION, lambda: x.store.transition(ledger.effect_id, S.DISPATCHED, S.APPLIED, x.clock.now))


@pytest.mark.parametrize("risk", [L.L3_MONEY_MOVEMENT, L.L4_BULK_OR_SYSTEMIC])
def test_l3_l4_cannot_issue_capability(setup_effect, monkeypatch, risk):
    x = setup_effect()
    entry = x.auth.verifier.catalog.get(x.intent.action_type).model_copy(update={"risk_level": risk})
    monkeypatch.setattr(x.auth.verifier.catalog, "get", lambda _: entry)
    fails(C.ACTION_NOT_ALLOWED, lambda: x.auth.issue_capability(x.intent))
    assert count(x, CapabilityRow) == 0


def test_money_effect_scope_rejected(setup_effect, monkeypatch):
    x = setup_effect()
    entry = x.auth.verifier.catalog.get(x.intent.action_type).model_copy(update={"max_effect_scope": EffectScope.MONEY})
    monkeypatch.setattr(x.auth.verifier.catalog, "get", lambda _: entry)
    fails(C.ACTION_NOT_ALLOWED, lambda: x.auth.issue_capability(x.intent))


def test_no_money_action_has_command_builder():
    assert set(DomainCommandBuilder.TYPES) == set(A) - {A.NO_REMEDIATION}
    schema = json.dumps(COMMAND_ADAPTER.json_schema())
    for forbidden in ("RETRY_PAYMENT", "CREATE_DISBURSEMENT", "CHANGE_ACCOUNT", "REFUND", "SQL", "url", "credential"):
        assert forbidden not in schema


def test_commands_reject_alias_and_arbitrary_payload(setup_effect):
    x = setup_effect()
    command = DomainCommandBuilder().for_intent(x.intent).model_dump(mode="json")
    with pytest.raises(ValidationError):
        COMMAND_ADAPTER.validate_python({**command, "message_ref": "EXTREF-001"})
    with pytest.raises(ValidationError):
        COMMAND_ADAPTER.validate_python({**command, "payload": {"sql": "anything"}})


def test_capability_one_effect_schema_and_secret_configuration(setup_effect, monkeypatch):
    x = setup_effect()
    payload = issue(x).payload.model_dump()
    with pytest.raises(ValidationError):
        ExecutionCapability.model_validate({**payload, "max_effects": 2})
    monkeypatch.delenv("CAPABILITY_SIGNING_SECRET")
    with pytest.raises(ValueError, match="CAPABILITY_SIGNING_SECRET"):
        HMACCapabilitySigner()


def test_capability_and_actor_never_enter_model_context_or_audit_secret(setup_effect):
    x = setup_effect()
    cap = issue(x)
    x.runtime.execute(cap)
    from credit_harness.remediation.renderer import RemediationInputRenderer
    bundle = RemediationInputRenderer().render(x.f.reader.read(CASE).snapshot)
    # Renderer signature/model contract may include inspection state; capability
    # remains entirely in the separate authorization repository.
    text = bundle.model_dump_json()
    audit = json.dumps([r.model_dump(mode="json") for r in x.store.audit(CASE)])
    for value in (cap.signature, "synthetic-test-secret-not-for-production-0001"):
        assert value not in text and value not in audit
    assert "OPERATOR-001" not in text and cap.payload.capability_id not in text


def test_side_effect_executor_has_no_money_client_or_oracle():
    root = Path("src/credit_harness/authorization")
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = " ".join(n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
        assert not any(s in imported for s in ("simulator", "openai", "planner.model"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "credit_harness.persistence.store":
                assert path.name == "tables.py" and {a.name for a in node.names} == {"Base"}
        identifiers = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not identifiers & {"WorldState", "GroundTruth", "SimulatorAdmin", "Evaluator", "CaseToolExecutor"}


def test_s8_unknown_never_issues_l2_capability(setup_effect):
    x = setup_effect(ScenarioId.S8, A.REQUEST_OPERATOR_REVIEW, ready=False)
    result = x.runtime.execute(issue(x))
    assert result.ledger.status == S.APPLIED and count(x, SyntheticTaskRow) == 1
    from tests.test_remediation import validate
    from credit_harness.remediation.models import RejectedRemediationCandidate
    assert isinstance(validate(x.f, A.REPLAY_CALLBACK_CONSUMPTION), RejectedRemediationCandidate)
    from credit_harness.authorization.service import FreshIntentVerifier
    with pytest.raises(AuthorizationError):
        FreshIntentVerifier(x.f.reader).verify(validate(x.f, A.REPLAY_CALLBACK_CONSUMPTION))
    assert not any(e.claim_type == ClaimType.PAYMENT_FINALITY and e.value in ("FAILED", "SETTLED", "NOT_EXECUTED")
                   for e in x.f.evidence.list(CASE))


def test_pending_approval_persists_across_service_instances(setup_effect):
    x = setup_effect()
    pending = x.auth.request_approval(x.intent)
    store = SQLApprovalStore(x.f.cases)
    assert store.get_approval(pending.approval_id, x.clock.now) == pending
    fresh_auth = RemediationAuthorizationService(x.f.reader, x.signer, clock=lambda: x.clock.now, store=store)
    approved = fresh_auth.decide_approval(ApprovalDecision(approval_id=pending.approval_id,
        decision=P.APPROVED, actor_ref="OPERATOR-001"))
    assert approved.intent == x.intent
    fresh_auth.issue_capability(x.intent, approval_id=approved.approval_id)


def test_expired_approval_invalidates_still_live_capability(setup_effect):
    x = setup_effect()
    x.auth.approval_ttl = 1
    cap = issue(x)
    x.clock.now += timedelta(seconds=2)
    assert x.clock.now < cap.payload.expires_at
    fails(C.APPROVAL_EXPIRED, lambda: x.runtime.execute(cap))
    assert count(x, EffectRow) == 0


def test_foreign_tenant_cannot_execute_or_inspect_approval(setup_effect):
    x = setup_effect()
    cap = issue(x)
    from credit_harness.cases.repository import CaseRepository
    from credit_harness.cases.models import CaseAccessError
    foreign = SQLApprovalStore(CaseRepository(x.engine, "FOREIGN-TENANT"))
    runtime = RemediationExecutionService(x.f.reader, x.signer, x.adapter, store=foreign, clock=lambda: x.clock.now)
    fails(C.CAPABILITY_SCOPE_MISMATCH, lambda: runtime.execute(cap))
    with pytest.raises(CaseAccessError):
        foreign.get_approval(cap.payload.approval_id, x.clock.now)


def test_unregistered_signed_capability_rejected(setup_effect):
    x = setup_effect()
    cap = issue(x)
    other = x.signer.sign(cap.payload.model_copy(update={"capability_id": "UNREGISTERED"}))
    fails(C.INVALID_CAPABILITY, lambda: x.runtime.execute(other))


def test_execution_revalidates_new_case_status(setup_effect):
    x = setup_effect()
    cap = issue(x)
    from credit_harness.cases.models import CaseStatus
    x.f.cases.pause(CASE, CaseStatus.WAITING)
    fails(C.STALE_AUTHORIZATION, lambda: x.runtime.execute(cap))
    assert count(x, EffectRow) == 0


def test_failed_receipt_without_no_effect_confirmation_is_unknown(setup_effect):
    x = setup_effect()
    # Bypass construction in an adversarial adapter; executor revalidates it.
    x.runtime.adapter = SimpleNamespace(dispatch=lambda command, correlation: SideEffectReceipt.model_construct(
        correlation_id=correlation, outcome=ReceiptOutcome.FAILED_CONFIRMED, external_effect_ref=None,
        observed_at=x.clock.now, no_effect_confirmed=False))
    assert x.runtime.execute(issue(x)).ledger.status == S.UNKNOWN


def test_authorization_audit_is_ordered_and_contains_no_oracle(setup_effect):
    x = setup_effect()
    cap = issue(x)
    x.runtime.execute(cap)
    records = SQLApprovalStore(x.f.cases).audit(CASE)
    assert [r.event.value for r in records] == ["APPROVAL_REQUESTED", "APPROVAL_DECIDED", "CAPABILITY_ISSUED",
        "EFFECT_PREPARED", "EFFECT_TRANSITION", "EFFECT_TRANSITION"]
    text = json.dumps([r.model_dump(mode="json") for r in records])
    for forbidden in ("ground_truth", "scenario_id", "root_cause", "expected_entry", "disbursement_intent_count",
                      "bank_card", "id_card", "reason_summary", "signature"):
        assert forbidden not in text


@pytest.mark.parametrize("scenario,administrative", [(ScenarioId.S6, False), (ScenarioId.S7, False), (ScenarioId.S7, True)])
def test_side_effect_demo_uses_full_proposal_and_authorization_chain(engine, monkeypatch, scenario, administrative):
    monkeypatch.setenv("CAPABILITY_SIGNING_SECRET", "synthetic-demo-test-secret-for-offline-only")
    from scripts.demo_side_effect import run_demo
    result = run_demo(engine, scenario=scenario, administrative=administrative)
    assert result["execution"]["ledger"]["status"] == "APPLIED"
    assert result["duplicate_execution"]["idempotent_replay"]
    assert result["business_verification"] == "NOT_YET_VERIFIED"
    assert result["case_status"] == "INVESTIGATING"
    assert result["evidence_before"] == result["evidence_after_execution"]

