from uuid import uuid4
from credit_harness.authorization.models import EffectStatus as S, AuthorizationError, AuthorizationCode as C
from credit_harness.cases.repository import utc_now
from .models import (RecoveryResult, RecoverySubject, RecoveryOutcome as O, RecoveryFailure as F, RecoveryError)
from .identity import reconstruct_identity
from .repository import LOOKUP_ONLY


class EffectStatusLookupService:
    """Bound resolver only. No side-effect adapter, command dispatch or signer."""
    def __init__(self, repository, resolver, clock=utc_now):
        if repository.contract != resolver.capability:
            raise ValueError("resolver must match trusted deployment contract")
        self.repository, self.resolver, self.clock = repository, resolver, clock

    def capture(self, claim):
        if (not self.repository.contract.supports_lookup
                or self.resolver.capability != self.repository.contract):
            raise RecoveryError(F.INVALID_LOOKUP_RECEIPT)
        ledger = self.repository.authorization.get_ledger(claim.effect_id)
        if ledger.status not in LOOKUP_ONLY or claim.source_status != ledger.status:
            raise RecoveryError(F.RECOVERY_STALE)
        identity, _, _ = reconstruct_identity(self.repository.authorization, ledger)
        result = self.resolver.lookup(ledger.dispatch_correlation_id, identity)
        self.repository._persist_lookup(claim, identity, result, self.clock())


class SideEffectRecoveryCoordinator:
    def __init__(self, repository, resolver, *, prepared_resumer=None, clock=utc_now, worker_id=None):
        self.repository, self.clock = repository, clock
        self.worker_id = worker_id or str(uuid4())
        self.lookup = EffectStatusLookupService(repository, resolver, clock)
        self.prepared_resumer = prepared_resumer

    def recover(self, effect_id):
        before = self.repository.authorization.get_ledger(effect_id)
        claim = self.repository.claim(effect_id, self.worker_id, self.clock())
        if claim is None:
            ledger = self.repository.authorization.get_ledger(effect_id)
            escalation = self.repository.requires_escalation(effect_id)
            action = O.ESCALATION_REQUIRED if escalation else (
                O.NOTHING_TO_DO if ledger.status not in (S.PREPARED, *LOOKUP_ONLY) else O.BUSY_OR_NOT_DUE)
            return self._result(str(uuid4()), before, ledger, action, escalation=escalation)
        failure = None
        try:
            if claim.source_status == S.PREPARED:
                if self.prepared_resumer is None:
                    raise RecoveryError(F.RECOVERY_STALE)
                self.prepared_resumer.resume(claim)
                ledger = self.repository.finish_resume(claim, self.clock())
                action = O.RESUMED_PREPARED
            else:
                self.lookup.capture(claim)
                ledger = self.repository.recover_transition(claim, self.clock())
                action = {S.APPLIED: O.RECOVERED_APPLIED, S.FAILED_CONFIRMED: O.RECOVERED_FAILED_CONFIRMED,
                          S.ACCEPTED: O.RECOVERED_ACCEPTED, S.UNKNOWN: O.STILL_UNKNOWN}[ledger.status]
        except AuthorizationError as error:
            failure = {C.CAPABILITY_EXPIRED: F.RECOVERY_AUTHORIZATION_EXPIRED,
                C.APPROVAL_EXPIRED: F.RECOVERY_AUTHORIZATION_EXPIRED,
                C.APPROVAL_REVOKED: F.RECOVERY_APPROVAL_REVOKED}.get(error.code, F.RECOVERY_STALE)
        except RecoveryError as error:
            failure = error.code
        except TimeoutError:
            failure = F.LOOKUP_TIMEOUT
        except (ValueError, TypeError, AttributeError):
            failure = F.INVALID_LOOKUP_RECEIPT
        except Exception:
            failure = F.LOOKUP_TRANSPORT_ERROR
        if failure:
            try:
                ledger = self.repository.finish_error(claim, self.clock(), failure)
            except RecoveryError:
                # A lease successor/original worker owns the result now. Do not
                # overwrite it with this stale worker's proof or exception.
                ledger = self.repository.authorization.get_ledger(effect_id)
            action = (O.BLOCKED_PREPARED if ledger.status == S.PREPARED else
                      O.STILL_UNKNOWN if ledger.status in LOOKUP_ONLY else O.NOTHING_TO_DO)
        escalation = self.repository.requires_escalation(effect_id)
        if escalation and ledger.status in LOOKUP_ONLY:
            action = O.ESCALATION_REQUIRED
        attempts = self.repository.attempts(effect_id)
        attempt = next(a for a in attempts if a.recovery_id == claim.recovery_id)
        return self._result(claim.recovery_id, before, ledger, action, escalation=escalation,
                            proof=attempt.proof_hash, failure=failure or attempt.failure_code)

    def _result(self, recovery_id, before, ledger, action, *, escalation=False, proof=None, failure=None):
        return RecoveryResult(recovery_id=recovery_id, subject_type=RecoverySubject.SIDE_EFFECT,
            subject_id=ledger.effect_id, before_status=before.status, after_status=ledger.status,
            action_taken=action, proof_ref=proof, requires_escalation=escalation, failure_code=failure)
