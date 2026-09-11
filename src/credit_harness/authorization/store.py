from datetime import datetime
from typing import Protocol
from uuid import uuid4
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from credit_harness.cases.tables import CaseRow
from credit_harness.cases.repository import next_update_time
from credit_harness.cases.models import CaseStatus
from credit_harness.remediation import models as remediation_versions
from credit_harness.remediation.models import RemediationIntent, RemediationActionType as A
from . import models as versions
from .models import (ApprovalRequest, ApprovalDecision, ApprovalStatus as P, ApprovalReason,
    ExecutionCapability, AuthorizationCode as C, AuthorizationError, SideEffectLedger,
    EffectStatus as S, FailureCode, AuditEvent, AuthorizationAuditRecord)
from .tables import AuthorizedIntentRow, ApprovalRow, CapabilityRow, CapabilitySignatureRow, EffectRow, AuthorizationAuditRow
from .commands import effect_key
from credit_harness.recovery.tables import synchronize_effect


class ApprovalStore(Protocol):
    def get_approval(self, approval_id: str, now) -> ApprovalRequest: ...
    def decide_approval(self, decision: ApprovalDecision, now) -> ApprovalRequest: ...


def approval_from_row(row, now):
    approval = ApprovalRequest.model_validate({**row.payload, "status": row.status})
    if approval.status in (P.PENDING, P.APPROVED) and approval.expires_at <= now:
        return approval.model_copy(update={"status": P.EXPIRED})
    return approval


class SQLApprovalStore:
    """Tenant-scoped durable approval/capability/ledger repository.

    Lock order is always Case, then approval/capability/effect. No transaction
    spans a network call. Updating the Case row serializes both supported DBs.
    """
    def __init__(self, cases):
        self.cases, self.engine = cases, cases.engine

    def _lock_case(self, session, case_id):
        result = session.execute(update(CaseRow).where(CaseRow.case_id == case_id,
            CaseRow.tenant_id == self.cases.tenant_id).values(updated_at=CaseRow.updated_at))
        if result.rowcount != 1:
            raise AuthorizationError(C.CAPABILITY_SCOPE_MISMATCH)
        return self.cases._row(session, case_id)

    def _intent(self, session, intent_id):
        row = session.get(AuthorizedIntentRow, intent_id)
        if row is None:
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        self.cases._row(session, row.case_id)
        return RemediationIntent.model_validate(row.payload)

    def get_intent(self, intent_id):
        with Session(self.engine) as session:
            return self._intent(session, intent_id)

    def _save_intent(self, session, intent):
        row = session.get(AuthorizedIntentRow, intent.intent_id)
        if row is None:
            session.add(AuthorizedIntentRow(intent_id=intent.intent_id, case_id=intent.case_id,
                                             payload=intent.model_dump(mode="json")))
            session.flush()
        elif row.payload != intent.model_dump(mode="json"):
            raise AuthorizationError(C.STALE_AUTHORIZATION)

    def _require_open(self, case):
        if CaseStatus(case.status).is_terminal:
            raise AuthorizationError(C.ACTION_NOT_ALLOWED)

    def _audit(self, session, event, intent, now, **fields):
        record = AuthorizationAuditRecord(audit_id=str(uuid4()), event=event, tenant_id=intent.tenant_id,
            case_id=intent.case_id, intent_id=intent.intent_id, recorded_at=now, **fields)
        session.add(AuthorizationAuditRow(audit_id=record.audit_id, case_id=record.case_id,
                                         payload=record.model_dump(mode="json")))

    def request_approval(self, request, revision):
        with Session(self.engine) as session, session.begin():
            case = self._lock_case(session, request.intent.case_id)
            self._require_open(case)
            if case.updated_at != revision.isoformat():
                raise AuthorizationError(C.STALE_AUTHORIZATION)
            self._save_intent(session, request.intent)
            session.add(ApprovalRow(approval_id=request.approval_id, intent_id=request.intent.intent_id,
                                   status=request.status.value, payload=request.model_dump(mode="json")))
            self._audit(session, AuditEvent.APPROVAL_REQUESTED, request.intent, request.requested_at,
                        approval_id=request.approval_id)
        return request

    def get_approval(self, approval_id, now):
        with Session(self.engine) as session:
            row = session.get(ApprovalRow, approval_id)
            if row is None:
                raise AuthorizationError(C.APPROVAL_REQUIRED)
            self._intent(session, row.intent_id)
            return approval_from_row(row, now)

    def decide_approval(self, decision: ApprovalDecision, now):
        decision = ApprovalDecision.model_validate(decision.model_dump())
        current = self.get_approval(decision.approval_id, now)
        with Session(self.engine) as session, session.begin():
            case = self._lock_case(session, current.intent.case_id)
            self._require_open(case)
            row = session.get(ApprovalRow, decision.approval_id)
            current = approval_from_row(row, now)
            allowed = current.status == P.PENDING or (current.status == P.APPROVED and decision.decision == P.REVOKED)
            if not allowed:
                raise AuthorizationError(C.APPROVAL_EXPIRED if current.status == P.EXPIRED else C.APPROVAL_NOT_APPROVED)
            reason = {P.APPROVED: ApprovalReason.OPERATOR_APPROVED, P.REJECTED: ApprovalReason.OPERATOR_REJECTED,
                      P.REVOKED: ApprovalReason.OPERATOR_REVOKED}[decision.decision]
            changed = current.model_copy(update=dict(status=decision.decision, decided_at=now,
                actor_ref=decision.actor_ref, reason_code=reason))
            row.status, row.payload = changed.status.value, changed.model_dump(mode="json")
            self._audit(session, AuditEvent.APPROVAL_DECIDED, changed.intent, now,
                        approval_id=changed.approval_id, actor_ref=changed.actor_ref)
        return changed

    def _check_approval(self, session, intent, approval_id, now):
        if not intent.requires_approval and approval_id is None:
            return
        if approval_id is None:
            raise AuthorizationError(C.APPROVAL_REQUIRED)
        row = session.get(ApprovalRow, approval_id)
        if row is None:
            raise AuthorizationError(C.APPROVAL_REQUIRED)
        approval = approval_from_row(row, now)
        if approval.intent != intent:
            raise AuthorizationError(C.APPROVAL_BINDING_MISMATCH)
        if approval.status != P.APPROVED:
            code = {P.EXPIRED: C.APPROVAL_EXPIRED, P.REVOKED: C.APPROVAL_REVOKED}.get(approval.status, C.APPROVAL_NOT_APPROVED)
            raise AuthorizationError(code)

    def issue(self, intent, capability, clock, *, signature=None):
        with Session(self.engine) as session, session.begin():
            case = self._lock_case(session, intent.case_id)
            self._require_open(case)
            now = clock()
            if case.updated_at != capability.expected_case_revision.isoformat() or now >= capability.expires_at:
                raise AuthorizationError(C.STALE_AUTHORIZATION)
            self._check_approval(session, intent, capability.approval_id, now)
            unresolved = session.scalar(select(EffectRow).where(
                EffectRow.case_id == capability.case_id, EffectRow.idempotency_key == effect_key(capability),
                EffectRow.status.in_([S.DISPATCHED.value, S.UNKNOWN.value, S.ACCEPTED.value])))
            if unresolved is not None:
                raise AuthorizationError(C.WRITE_FENCED_BY_UNRESOLVED_EFFECT)
            self._save_intent(session, intent)
            session.add(CapabilityRow(capability_id=capability.capability_id, intent_id=intent.intent_id,
                                     payload=capability.model_dump(mode="json"), used_effect_id=None))
            session.flush()
            if signature is not None:
                session.add(CapabilitySignatureRow(capability_id=capability.capability_id, signature=signature))
            self._audit(session, AuditEvent.CAPABILITY_ISSUED, intent, now,
                        approval_id=capability.approval_id, capability_id=capability.capability_id)

    def _check_capability(self, session, cap, now):
        if cap.tenant_id != self.cases.tenant_id:
            raise AuthorizationError(C.CAPABILITY_SCOPE_MISMATCH)
        if now >= cap.expires_at or now < cap.issued_at:
            raise AuthorizationError(C.CAPABILITY_EXPIRED)
        if (cap.policy_version != remediation_versions.REMEDIATION_POLICY_VERSION
                or cap.catalog_version != remediation_versions.REMEDIATION_CATALOG_VERSION
                or cap.authorization_policy_version != versions.AUTHORIZATION_POLICY_VERSION):
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        row = session.get(CapabilityRow, cap.capability_id)
        if row is None or row.payload != cap.model_dump(mode="json"):
            raise AuthorizationError(C.INVALID_CAPABILITY)
        intent = self._intent(session, cap.intent_id)
        self._check_approval(session, intent, cap.approval_id, now)
        return row, intent

    def _existing(self, session, cap, cap_row, key):
        row = session.scalar(select(EffectRow).where(EffectRow.idempotency_key == key))
        if row is not None:
            ledger = SideEffectLedger.model_validate(row.payload)
            if (ledger.payload_hash != cap.payload_hash or ledger.target_hash != cap.target_hash
                    or ledger.case_id != cap.case_id or ledger.action_type != cap.action_type
                    or ledger.tenant_id != cap.tenant_id):
                raise AuthorizationError(C.IDEMPOTENCY_CONFLICT)
            if cap_row.used_effect_id not in (None, row.effect_id):
                raise AuthorizationError(C.CAPABILITY_ALREADY_USED)
            cap_row.used_effect_id = row.effect_id
            return ledger
        if cap_row.used_effect_id is not None:
            raise AuthorizationError(C.CAPABILITY_ALREADY_USED)
        return None

    def existing(self, cap, clock):
        with Session(self.engine) as session, session.begin():
            self._lock_case(session, cap.case_id)
            row, _ = self._check_capability(session, cap, clock())
            return self._existing(session, cap, row, effect_key(cap))

    def prepare(self, cap, clock):
        with Session(self.engine) as session, session.begin():
            case = self._lock_case(session, cap.case_id)
            self._require_open(case)
            now = clock()
            cap_row, intent = self._check_capability(session, cap, now)
            key = effect_key(cap)
            old = self._existing(session, cap, cap_row, key)
            if old:
                return old, False
            noop = cap.action_type == A.NO_REMEDIATION
            new_revision = case.updated_at if noop else next_update_time(datetime.fromisoformat(case.updated_at))
            result = session.execute(update(CaseRow).where(CaseRow.case_id == cap.case_id,
                CaseRow.tenant_id == cap.tenant_id,
                CaseRow.updated_at == cap.expected_case_revision.isoformat()).values(
                    updated_at=new_revision))
            if result.rowcount != 1:
                raise AuthorizationError(C.STALE_AUTHORIZATION)
            ledger = SideEffectLedger(effect_id=key, idempotency_key=key, intent_id=cap.intent_id,
                capability_id=cap.capability_id, approval_id=cap.approval_id, tenant_id=cap.tenant_id,
                case_id=cap.case_id, internal_order_id=cap.internal_order_id, action_type=cap.action_type,
                target_hash=cap.target_hash, payload_hash=cap.payload_hash, snapshot_id=cap.snapshot_id,
                evidence_hash=cap.evidence_hash, status=S.NOOP if noop else S.PREPARED, attempt_count=0,
                prepared_case_revision=new_revision,
                created_at=now, updated_at=now, dispatch_correlation_id=str(uuid4()))
            session.add(EffectRow(effect_id=key, idempotency_key=key, capability_id=cap.capability_id,
                case_id=cap.case_id, status=ledger.status.value, payload=ledger.model_dump(mode="json")))
            cap_row.used_effect_id = key
            synchronize_effect(session, ledger)
            self._audit(session, AuditEvent.EFFECT_PREPARED, intent, now, capability_id=cap.capability_id,
                approval_id=cap.approval_id, effect_id=key, correlation_id=ledger.dispatch_correlation_id,
                effect_status=ledger.status)
            return ledger, not noop

    def dispatch_prepared(self, cap, clock, *, recovery_claim=None):
        with Session(self.engine) as session, session.begin():
            case = self._lock_case(session, cap.case_id)
            self._require_open(case)
            now = clock()
            cap_row, intent = self._check_capability(session, cap, now)
            row = session.get(EffectRow, cap_row.used_effect_id)
            if row is None or row.capability_id != cap.capability_id:
                raise AuthorizationError(C.CAPABILITY_ALREADY_USED)
            ledger = SideEffectLedger.model_validate(row.payload)
            if recovery_claim is not None:
                from credit_harness.recovery.tables import EffectRecoveryStateRow
                lease = session.get(EffectRecoveryStateRow, ledger.effect_id)
                if (recovery_claim.effect_id != ledger.effect_id or lease is None
                        or lease.lease_token != recovery_claim.recovery_id
                        or lease.lease_owner != recovery_claim.worker_id
                        or lease.lease_until is None or lease.lease_until <= now.timestamp()):
                    raise AuthorizationError(C.STALE_AUTHORIZATION)
            if case.updated_at != ledger.prepared_case_revision.isoformat():
                raise AuthorizationError(C.STALE_AUTHORIZATION)
            changed = ledger.model_copy(update=dict(status=S.DISPATCHED, attempt_count=1, updated_at=now))
            result = session.execute(update(EffectRow).where(EffectRow.effect_id == row.effect_id,
                EffectRow.status == S.PREPARED.value).values(status=S.DISPATCHED.value,
                payload=changed.model_dump(mode="json")))
            if result.rowcount != 1:
                raise AuthorizationError(C.INVALID_TRANSITION)
            synchronize_effect(session, changed)
            self._audit(session, AuditEvent.EFFECT_TRANSITION, intent, now,
                capability_id=cap.capability_id, approval_id=cap.approval_id, effect_id=ledger.effect_id,
                correlation_id=ledger.dispatch_correlation_id, effect_status=S.DISPATCHED)
            return changed

    def get_ledger(self, effect_id):
        with Session(self.engine) as session:
            row = session.get(EffectRow, effect_id)
            if row is None:
                raise KeyError("effect unavailable")
            self.cases._row(session, row.case_id)
            return SideEffectLedger.model_validate(row.payload)

    # PREPARED -> DISPATCHED is only available through dispatch_prepared, which
    # rechecks authorization. A generic status update cannot bypass that gate.
    TRANSITIONS = {S.DISPATCHED: {S.ACCEPTED, S.APPLIED, S.UNKNOWN, S.FAILED_CONFIRMED},
                   S.ACCEPTED: {S.APPLIED, S.UNKNOWN, S.FAILED_CONFIRMED}}

    def transition(self, effect_id, expected, status, now, *, receipt=None, failure=None):
        if status not in self.TRANSITIONS.get(expected, set()):
            raise AuthorizationError(C.INVALID_TRANSITION)
        if status != S.UNKNOWN and (receipt is None or receipt.outcome.value != status.value):
            raise AuthorizationError(C.INVALID_TRANSITION)
        with Session(self.engine) as session, session.begin():
            row = session.get(EffectRow, effect_id)
            self._lock_case(session, row.case_id)
            session.refresh(row)
            ledger = SideEffectLedger.model_validate(row.payload)
            if receipt and receipt.correlation_id != ledger.dispatch_correlation_id:
                raise AuthorizationError(C.INVALID_TRANSITION)
            if (receipt and receipt.external_effect_ref and ledger.external_effect_ref
                    and receipt.external_effect_ref != ledger.external_effect_ref):
                raise AuthorizationError(C.INVALID_TRANSITION)
            changed = ledger.model_copy(update=dict(status=status, updated_at=now,
                attempt_count=1 if status == S.DISPATCHED else ledger.attempt_count,
                external_effect_ref=receipt.external_effect_ref if receipt else ledger.external_effect_ref,
                failure_code=failure))
            updated = session.execute(update(EffectRow).where(EffectRow.effect_id == effect_id,
                EffectRow.status == expected.value).values(status=status.value, payload=changed.model_dump(mode="json")))
            if updated.rowcount != 1:
                raise AuthorizationError(C.INVALID_TRANSITION)
            synchronize_effect(session, changed)
            intent = self._intent(session, ledger.intent_id)
            self._audit(session, AuditEvent.EFFECT_TRANSITION, intent, now, capability_id=ledger.capability_id,
                approval_id=ledger.approval_id, effect_id=effect_id, correlation_id=ledger.dispatch_correlation_id,
                effect_status=status)
            return changed

    def audit(self, case_id):
        with Session(self.engine) as session:
            self.cases._row(session, case_id)
            return tuple(AuthorizationAuditRecord.model_validate(r.payload) for r in session.scalars(
                select(AuthorizationAuditRow).where(AuthorizationAuditRow.case_id == case_id)
                    .order_by(AuthorizationAuditRow.sequence)))
