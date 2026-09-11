from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, update, or_
from sqlalchemy.orm import Session
from credit_harness.authorization.models import SideEffectLedger, EffectStatus as S
from credit_harness.authorization.tables import EffectRow
from credit_harness.context.budget import digest
from .models import (RecoveryPolicy, RecoveryClaim, RecoveryError, RecoveryFailure as F, RecoveryProof,
    RecoveryCapability, SideEffectLookupResult, EffectRecoveryAttempt, RecoveryAuditRecord, RecoveryAuditEvent as E)
from .tables import EffectRecoveryStateRow as State, EffectRecoveryAttemptRow as Attempt, RecoveryAuditRow, synchronize_effect
from .identity import reconstruct_identity
from .proof import validate_lookup, reconcile_status

RECOVERABLE = (S.PREPARED, S.DISPATCHED, S.ACCEPTED, S.UNKNOWN)
LOOKUP_ONLY = (S.DISPATCHED, S.ACCEPTED, S.UNKNOWN)


def instant(value):
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


class RecoveryRepository:
    def __init__(self, authorization_store, contract, *, policy=None):
        self.authorization = authorization_store
        self.engine, self.cases = authorization_store.engine, authorization_store.cases
        # Trusted deployment config, never supplied by recover() or model output.
        self.contract = RecoveryCapability.model_validate(contract.model_dump())
        self.policy = policy or RecoveryPolicy()

    def _locked(self, session, effect_id):
        row = session.get(EffectRow, effect_id)
        if row is None:
            raise KeyError("effect unavailable")
        self.authorization._lock_case(session, row.case_id)
        session.refresh(row)
        ledger = SideEffectLedger.model_validate(row.payload)
        state = session.get(State, effect_id)
        if state is None:
            raise RecoveryError(F.RECOVERY_STALE)
        return row, ledger, state

    def list_recoverable_effects(self, now, *, older_than=None, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("bounded scan required")
        cutoff = min(now.timestamp() - self.policy.grace_seconds,
                     older_than.timestamp() if older_than else now.timestamp())
        with Session(self.engine) as session:
            return tuple(session.scalars(select(State.effect_id).where(
                State.tenant_id == self.cases.tenant_id, State.status.in_([s.value for s in RECOVERABLE]),
                State.ledger_updated_at <= cutoff, State.next_eligible_at <= now.timestamp(),
                State.requires_escalation.is_(False),
                or_(State.lease_until.is_(None), State.lease_until <= now.timestamp()))
                .order_by(State.next_eligible_at, State.effect_id).limit(limit)))

    def claim(self, effect_id, worker_id, now):
        with Session(self.engine) as session, session.begin():
            _, ledger, state = self._locked(session, effect_id)
            if ledger.status not in RECOVERABLE:
                return None
            if state.requires_escalation or state.attempt_count >= self.policy.max_attempts:
                state.requires_escalation = True
                return None
            if (now.timestamp() < state.next_eligible_at
                    or now.timestamp() < state.ledger_updated_at + self.policy.grace_seconds
                    or (state.lease_until is not None and state.lease_until > now.timestamp())):
                return None
            if state.lease_token:
                abandoned = session.get(Attempt, state.lease_token)
                if abandoned and abandoned.completed_at is None:
                    abandoned.completed_at, abandoned.failure_code = now.timestamp(), F.LEASE_LOST.value
            claim = RecoveryClaim(recovery_id=str(uuid4()), effect_id=effect_id, worker_id=worker_id,
                                  source_status=ledger.status, sequence=state.attempt_count + 1)
            state.lease_owner, state.lease_token = worker_id, claim.recovery_id
            state.lease_until = now.timestamp() + self.policy.lease_seconds
            state.attempt_count = claim.sequence
            session.add(Attempt(recovery_id=claim.recovery_id, worker_id=worker_id, effect_id=effect_id, sequence=claim.sequence,
                source_status=ledger.status.value, started_at=now.timestamp(), resolver_id=self.contract.resolver_id))
            self._audit(session, ledger, claim, E.RECOVERY_SCAN, now)
            return claim

    def _owned(self, session, claim, state, now):
        if (state.lease_token != claim.recovery_id or state.lease_owner != claim.worker_id
                or state.lease_until is None or state.lease_until <= now.timestamp()):
            raise RecoveryError(F.LEASE_LOST)
        attempt = session.get(Attempt, claim.recovery_id)
        if attempt is None or attempt.completed_at is not None or attempt.effect_id != claim.effect_id:
            raise RecoveryError(F.LEASE_LOST)
        return attempt

    def _audit(self, session, ledger, claim, event, now, *, proof=None, failure=None):
        record = RecoveryAuditRecord(recovery_id=claim.recovery_id, tenant_id=ledger.tenant_id,
            case_id=ledger.case_id, intent_id=ledger.intent_id, approval_id=ledger.approval_id,
            capability_id=ledger.capability_id, effect_id=ledger.effect_id,
            correlation_id=ledger.dispatch_correlation_id, event=event, recorded_at=now,
            proof=proof, failure_code=failure)
        session.add(RecoveryAuditRow(effect_id=ledger.effect_id, recovery_id=claim.recovery_id,
                                     payload=record.model_dump(mode="json")))

    def _persist_lookup(self, claim, identity, result, now):
        """Called only by the bound status lookup gateway after resolver.lookup.

        Normal ledger transitions cannot consume arbitrary caller-authored proof.
        """
        with Session(self.engine) as session, session.begin():
            _, ledger, state = self._locked(session, claim.effect_id)
            attempt = self._owned(session, claim, state, now)
            if ledger.status != claim.source_status or ledger.status not in LOOKUP_ONLY:
                raise RecoveryError(F.RECOVERY_STALE)
            if attempt.lookup_result is not None:
                raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
            result, proof = validate_lookup(ledger, identity, result, self.contract, now)
            attempt.lookup_result, attempt.proof = result.model_dump(mode="json"), proof.model_dump(mode="json")
            attempt.proof_hash = digest(attempt.proof)
            self._audit(session, ledger, claim, E.RECOVERY_LOOKUP, now, proof=proof)

    def recover_transition(self, claim, now):
        """Only a previously persisted, fully bound lookup proof may settle state.

        No status/result/contract argument is accepted from the caller.
        """
        with Session(self.engine) as session, session.begin():
            row, ledger, state = self._locked(session, claim.effect_id)
            attempt = self._owned(session, claim, state, now)
            if ledger.status != claim.source_status or ledger.status not in LOOKUP_ONLY:
                raise RecoveryError(F.RECOVERY_STALE)
            if not attempt.lookup_result or not attempt.proof or attempt.resolver_id != self.contract.resolver_id:
                raise RecoveryError(F.INVALID_LOOKUP_RECEIPT)
            identity, _, _ = reconstruct_identity(self.authorization, ledger)
            lookup = SideEffectLookupResult.model_validate(attempt.lookup_result)
            lookup, proof = validate_lookup(ledger, identity, lookup, self.contract, now)
            if (attempt.proof != proof.model_dump(mode="json") or attempt.proof_hash != digest(attempt.proof)):
                raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
            target, failure = reconcile_status(lookup, self.contract)
            changed = ledger.model_copy(update=dict(status=target, updated_at=now,
                external_effect_ref=lookup.external_effect_ref or ledger.external_effect_ref,
                failure_code=ledger.failure_code if target == S.UNKNOWN else None))
            self._cas(session, row, ledger, changed)
            self._finish(session, changed, state, attempt, claim, now, failure=failure, proof=proof)
            return changed

    def _cas(self, session, row, before, after):
        result = session.execute(update(EffectRow).where(EffectRow.effect_id == row.effect_id,
            EffectRow.status == before.status.value).values(status=after.status.value, payload=after.model_dump(mode="json")))
        if result.rowcount != 1:
            raise RecoveryError(F.RECOVERY_STALE)
        synchronize_effect(session, after)

    def _finish(self, session, ledger, state, attempt, claim, now, *, failure=None, proof=None, escalate=False):
        attempt.completed_at, attempt.result_status = now.timestamp(), ledger.status.value
        attempt.failure_code = failure.value if failure else None
        state.lease_owner = state.lease_token = state.lease_until = None
        state.next_eligible_at = now.timestamp() + self.policy.backoff_seconds[min(claim.sequence - 1, len(self.policy.backoff_seconds) - 1)]
        state.requires_escalation = ledger.status in RECOVERABLE and (escalate or claim.sequence >= self.policy.max_attempts)
        self._audit(session, ledger, claim, E.RECOVERY_TRANSITION, now, proof=proof, failure=failure)
        if state.requires_escalation:
            self._audit(session, ledger, claim, E.RECOVERY_ESCALATED, now,
                        failure=failure or F.RECOVERY_LIMIT_REACHED)

    def finish_error(self, claim, now, failure):
        with Session(self.engine) as session, session.begin():
            row, ledger, state = self._locked(session, claim.effect_id)
            attempt = self._owned(session, claim, state, now)
            # Never overwrite a final receipt committed by the original worker.
            if ledger.status == claim.source_status and ledger.status in LOOKUP_ONLY:
                changed = ledger.model_copy(update=dict(status=S.UNKNOWN, updated_at=now))
                self._cas(session, row, ledger, changed)
                ledger = changed
            self._finish(session, ledger, state, attempt, claim, now, failure=failure,
                         escalate=failure in (F.LOOKUP_PROOF_CONFLICT, F.INVALID_LOOKUP_RECEIPT))
            return ledger

    def finish_resume(self, claim, now):
        with Session(self.engine) as session, session.begin():
            _, ledger, state = self._locked(session, claim.effect_id)
            attempt = self._owned(session, claim, state, now)
            self._finish(session, ledger, state, attempt, claim, now)
            return ledger

    def requires_escalation(self, effect_id):
        self.authorization.get_ledger(effect_id)
        with Session(self.engine) as session:
            return session.get(State, effect_id).requires_escalation

    def attempts(self, effect_id):
        self.authorization.get_ledger(effect_id)
        with Session(self.engine) as session:
            return tuple(EffectRecoveryAttempt(recovery_id=r.recovery_id, worker_id=r.worker_id, effect_id=r.effect_id,
                sequence=r.sequence, started_at=instant(r.started_at), completed_at=instant(r.completed_at),
                source_status=r.source_status, lookup_result=r.lookup_result, result_status=r.result_status,
                resolver_id=r.resolver_id, proof_hash=r.proof_hash, failure_code=r.failure_code)
                for r in session.scalars(select(Attempt).where(Attempt.effect_id == effect_id).order_by(Attempt.sequence)))

    def audit(self, effect_id):
        self.authorization.get_ledger(effect_id)
        with Session(self.engine) as session:
            return tuple(RecoveryAuditRecord.model_validate(r.payload) for r in session.scalars(
                select(RecoveryAuditRow).where(RecoveryAuditRow.effect_id == effect_id).order_by(RecoveryAuditRow.sequence)))


class RecoveryScanner:
    def __init__(self, repository, clock):
        self.repository, self.clock = repository, clock

    def list_recoverable_effects(self, *, older_than=None, limit=100):
        return self.repository.list_recoverable_effects(self.clock(), older_than=older_than, limit=limit)
