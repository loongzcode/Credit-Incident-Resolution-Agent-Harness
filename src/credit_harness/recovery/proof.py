from credit_harness.context.budget import digest
from credit_harness.authorization.models import EffectStatus as S
from .models import (LookupStatus as L, SideEffectLookupResult, RecoveryCapability,
                     RecoveryProof, RecoveryError, RecoveryFailure as F)


def validate_lookup(ledger, identity, result, contract, now):
    result = SideEffectLookupResult.model_validate(result.model_dump())
    contract = RecoveryCapability.model_validate(contract.model_dump())
    if (not contract.supports_lookup or result.resolver_id != contract.resolver_id
            or result.correlation_id != ledger.dispatch_correlation_id
            or result.command_identity_hash != digest(identity.model_dump(mode="json"))
            or ledger.effect_id != identity.effect_id):
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    age = (now - result.observed_at).total_seconds()
    if age < 0 or age > contract.lookup_freshness_sla_seconds:
        raise RecoveryError(F.INVALID_LOOKUP_RECEIPT)
    if (ledger.external_effect_ref and result.external_effect_ref
            and ledger.external_effect_ref != result.external_effect_ref):
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    if result.lookup_status == L.NOT_FOUND and ledger.external_effect_ref:
        # A missing index cannot erase a previously bound external operation.
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    if result.external_effect_ref and not contract.supports_external_effect_ref:
        raise RecoveryError(F.INVALID_LOOKUP_RECEIPT)
    proof = RecoveryProof(effect_id=ledger.effect_id, correlation_id=result.correlation_id,
        resolver_id=result.resolver_id, lookup_status=result.lookup_status,
        external_effect_ref=result.external_effect_ref, observed_at=result.observed_at,
        contract_version=contract.contract_version, command_identity_hash=result.command_identity_hash,
        result_hash=digest(result.model_dump(mode="json")))
    return result, proof


def reconcile_status(result, contract):
    if result.lookup_status == L.FOUND_APPLIED:
        return S.APPLIED, None
    if result.lookup_status == L.FOUND_ACCEPTED:
        return S.ACCEPTED, None
    if result.lookup_status == L.FOUND_FAILED_NO_EFFECT and result.no_effect_confirmed:
        return S.FAILED_CONFIRMED, None
    if result.lookup_status == L.NOT_FOUND:
        if contract.not_found_proves_no_effect:
            return S.FAILED_CONFIRMED, None
        return S.UNKNOWN, F.LOOKUP_NOT_FOUND
    return S.UNKNOWN, F.LOOKUP_INDETERMINATE
