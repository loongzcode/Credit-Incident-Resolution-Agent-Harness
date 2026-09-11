import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pydantic import ValidationError
from credit_harness.context.budget import digest
from credit_harness.remediation.models import (RemediationIntent, RemediationCandidate, RemediationActionType as A,
    RejectedRemediationCandidate, PreflightStatus, ActionRiskLevel as L)
from credit_harness.remediation.catalog import RemediationActionCatalog, ForbiddenRemediationPolicy
from credit_harness.remediation.validator import RemediationCandidateValidator
from credit_harness.remediation.preflight import RemediationPreflightService
from credit_harness.hypotheses.models import HypothesisId as H
from . import models as versions
from .models import (ApprovalRequest, ApprovalStatus, ExecutionCapability, AuthorizationCode as C,
    AuthorizationError, EffectStatus as S, FailureCode, SideEffectReceipt, SideEffectExecutionResult)
from .commands import DomainCommandBuilder
from .signing import CapabilitySigner
from .store import SQLApprovalStore


def utc_now():
    return datetime.now(timezone.utc)


class FreshIntentVerifier:
    def __init__(self, reader):
        self.reader = reader
        self.catalog = RemediationActionCatalog()

    def verify(self, intent):
        if type(intent) is not RemediationIntent:
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        intent = RemediationIntent.model_validate(intent.model_dump())
        if intent.intent_id != digest(intent.model_dump(mode="json", exclude={"intent_id"})):
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        entry = self.catalog.get(intent.action_type)
        if ForbiddenRemediationPolicy().check(entry) or intent.risk_level != entry.risk_level:
            raise AuthorizationError(C.ACTION_NOT_ALLOWED)
        if entry.risk_level == L.L2_SINGLE_ORDER_SIDE_EFFECT and not entry.requires_future_approval:
            raise AuthorizationError(C.ACTION_NOT_ALLOWED)
        state = self.reader.read(intent.case_id)
        if (intent.tenant_id != state.case.tenant_id or intent.internal_order_id != state.case.internal_order_id
                or state.snapshot.snapshot_id != intent.snapshot_id):
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        if intent.action_type == A.REPLAY_CALLBACK_CONSUMPTION:
            problems = (H.H6.value,)
        elif intent.action_type in (A.REDELIVER_ASSET_NOTIFICATION, A.CREATE_RECONCILIATION_TASK):
            problems = (H.H7.value,)
        else:
            problems = tuple(g.gap_id for g in state.graph.open_gaps) or tuple(h.value for h in state.graph.confirmed)
        candidate = RemediationCandidate(candidate_id="authorization-revalidation", action_type=intent.action_type,
            target_order_id=intent.internal_order_id, target_problem_ids=problems,
            evidence_refs=intent.supporting_evidence_refs, reason_summary="")
        checked = RemediationCandidateValidator(self.catalog).validate(state, candidate)
        if isinstance(checked, RejectedRemediationCandidate):
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        result = RemediationPreflightService(self.reader, self.catalog).preview(checked)
        if result.status != PreflightStatus.READY_FOR_FUTURE_AUTHORIZATION or result.intent != intent:
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        return intent, state.case


class RemediationAuthorizationService:
    def __init__(self, reader, signer: CapabilitySigner, *, clock=utc_now, capability_ttl_seconds=None,
                 approval_ttl_seconds=300, store=None):
        self.reader, self.signer, self.clock = reader, signer, clock
        self.store = store or SQLApprovalStore(reader.cases)
        self.verifier = FreshIntentVerifier(reader)
        self.builder = DomainCommandBuilder()
        self.ttl = int(capability_ttl_seconds if capability_ttl_seconds is not None else os.getenv("CAPABILITY_TTL_SECONDS", "300"))
        self.approval_ttl = approval_ttl_seconds
        if not 1 <= self.ttl <= 300 or not 1 <= approval_ttl_seconds <= 3600:
            raise ValueError("capability TTL is 1..300s; approval TTL is 1..3600s")

    def request_approval(self, intent):
        intent, case = self.verifier.verify(intent)
        now = self.clock()
        request = ApprovalRequest(approval_id=str(uuid4()), intent=intent, status=ApprovalStatus.PENDING,
            requested_at=now, expires_at=now + timedelta(seconds=self.approval_ttl))
        return self.store.request_approval(request, case.updated_at)

    def decide_approval(self, decision):
        return self.store.decide_approval(decision, self.clock())

    def issue_capability(self, intent, *, approval_id=None):
        intent, case = self.verifier.verify(intent)
        now = self.clock()
        capability = ExecutionCapability(capability_id=str(uuid4()), intent_id=intent.intent_id,
            tenant_id=intent.tenant_id, case_id=intent.case_id, internal_order_id=intent.internal_order_id,
            action_type=intent.action_type, target_hash=digest(intent.target.model_dump(mode="json")),
            payload_hash=self.builder.payload_hash(intent), snapshot_id=intent.snapshot_id,
            evidence_hash=intent.evidence_hash, preflight_fingerprint=intent.preflight_fingerprint,
            approval_id=approval_id, policy_version=intent.policy_version, catalog_version=intent.catalog_version,
            authorization_policy_version=versions.AUTHORIZATION_POLICY_VERSION, expected_case_revision=case.updated_at,
            issued_at=now, expires_at=now + timedelta(seconds=self.ttl))
        self.store.issue(intent, capability, self.clock)
        return self.signer.sign(capability)


class RemediationExecutionService:
    """Signed capability only. Contains no model, planning or automatic retries."""
    def __init__(self, reader, signer, adapter, *, clock=utc_now, store=None):
        self.reader, self.signer, self.adapter, self.clock = reader, signer, adapter, clock
        self.store = store or SQLApprovalStore(reader.cases)
        self.verifier, self.builder = FreshIntentVerifier(reader), DomainCommandBuilder()

    def execute(self, signed_capability):
        cap = self.signer.verify(signed_capability)
        # Authentication/expiry/approval are checked even on duplicate requests.
        existing = self.store.existing(cap, self.clock)
        if existing:
            return SideEffectExecutionResult(ledger=existing, executed_now=False, idempotent_replay=True)
        intent = self.store.get_intent(cap.intent_id)
        try:
            fresh, case = self.verifier.verify(intent)
        except AuthorizationError as error:
            if error.code == C.STALE_AUTHORIZATION:
                existing = self.store.existing(cap, self.clock)
                if existing:
                    return SideEffectExecutionResult(ledger=existing, executed_now=False, idempotent_replay=True)
            raise
        if case.updated_at != cap.expected_case_revision:
            # A concurrent winner may have prepared while this worker rebuilt.
            existing = self.store.existing(cap, self.clock)
            if existing:
                return SideEffectExecutionResult(ledger=existing, executed_now=False, idempotent_replay=True)
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        command = self.builder.build(fresh, cap)
        ledger, owns_dispatch = self.store.prepare(cap, self.clock)
        if not owns_dispatch:
            return SideEffectExecutionResult(ledger=ledger, executed_now=False,
                idempotent_replay=ledger.capability_id != cap.capability_id or ledger.status != S.NOOP)
        # PREPARED and durable capability consumption have committed before this.
        ledger = self.store.dispatch_prepared(cap, self.clock)
        failure, receipt = None, None
        try:
            receipt = self.adapter.dispatch(command, ledger.dispatch_correlation_id)
            receipt = SideEffectReceipt.model_validate(receipt.model_dump())
            if receipt.correlation_id != ledger.dispatch_correlation_id:
                raise ValueError("receipt correlation mismatch")
            status = S(receipt.outcome.value)
            if status == S.FAILED_CONFIRMED:
                failure = FailureCode.REMOTE_REJECTED
        except (ValueError, TypeError, AttributeError, ValidationError):
            status, receipt, failure = S.UNKNOWN, None, FailureCode.INVALID_RECEIPT
        except Exception:
            status, receipt, failure = S.UNKNOWN, None, FailureCode.TRANSPORT_UNCERTAIN
        ledger = self.store.transition(ledger.effect_id, S.DISPATCHED, status, self.clock(),
                                       receipt=receipt, failure=failure)
        return SideEffectExecutionResult(ledger=ledger, executed_now=True, idempotent_replay=False)
