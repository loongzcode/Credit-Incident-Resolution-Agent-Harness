from credit_harness.authorization.models import AuthorizationError, AuthorizationCode as C, EffectStatus as S
from credit_harness.authorization.service import FreshIntentVerifier
from credit_harness.authorization.commands import DomainCommandBuilder
from credit_harness.remediation.state import InvestigationStateReader
from .identity import reconstruct_identity, original_signed_capability
from .models import RecoveryError, RecoveryFailure as F


class _PreparedCases:
    """Normalize only the proven prepare CAS for original-snapshot comparison.

    This never writes/reverts the actual Case. Any later Evidence/budget/lifecycle
    revision fails. All actual facts and constraints are reread unchanged.
    """
    def __init__(self, cases, ledger, cap):
        self.source, self.ledger, self.cap = cases, ledger, cap
        self.engine, self.tenant_id = cases.engine, cases.tenant_id

    def get(self, case_id):
        case = self.source.get(case_id)
        if case_id != self.ledger.case_id or case.updated_at != self.ledger.prepared_case_revision:
            raise AuthorizationError(C.STALE_AUTHORIZATION)
        return case.model_copy(update={"updated_at": self.cap.expected_case_revision})


class PreparedEffectResumer:
    """The ONLY recovery path allowed to dispatch, guarded by original PREPARED CAS."""
    def __init__(self, execution_service):
        self.execution = execution_service

    def resume(self, claim):
        runtime = self.execution
        ledger = runtime.store.get_ledger(claim.effect_id)
        if ledger.status != S.PREPARED or claim.source_status != S.PREPARED:
            raise RecoveryError(F.RECOVERY_STALE)
        _, cap, intent = reconstruct_identity(runtime.store, ledger)
        signed = original_signed_capability(runtime.store, cap)
        runtime.signer.verify(signed)
        # Checks TTL/revocation/version even though the existing Ledger consumes
        # the capability. No new token, effect key or correlation is generated.
        runtime.store.existing(cap, runtime.clock)
        cases = _PreparedCases(runtime.reader.cases, ledger, cap)
        reader = InvestigationStateReader(cases, runtime.reader.evidence, runtime.reader.assembler)
        fresh, _ = FreshIntentVerifier(reader).verify(intent)
        command = DomainCommandBuilder().build(fresh, cap)
        dispatched = runtime.store.dispatch_prepared(cap, runtime.clock, recovery_claim=claim)
        return runtime._complete_dispatch(command, dispatched)
