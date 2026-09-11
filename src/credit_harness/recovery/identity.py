from sqlalchemy.orm import Session
from credit_harness.authorization.models import ExecutionCapability, SignedExecutionCapability, AuthorizationError
from credit_harness.authorization.tables import CapabilityRow, CapabilitySignatureRow
from credit_harness.authorization.commands import DomainCommandBuilder, effect_key
from credit_harness.context.budget import digest
from .models import EffectIdentity, RecoveryError, RecoveryFailure as F


def reconstruct_identity(store, ledger):
    """Historical binding only: expiry/revocation never prevent a status lookup."""
    intent = store.get_intent(ledger.intent_id)
    with Session(store.engine) as session:
        row = session.get(CapabilityRow, ledger.capability_id)
        if row is None or row.used_effect_id != ledger.effect_id:
            raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
        cap = ExecutionCapability.model_validate(row.payload)
    if intent.intent_id != digest(intent.model_dump(mode="json", exclude={"intent_id"})):
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    for field in ("intent_id", "tenant_id", "case_id", "internal_order_id", "action_type", "target_hash",
                  "payload_hash", "snapshot_id", "evidence_hash", "approval_id"):
        if getattr(cap, field) != getattr(ledger, field):
            raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    if effect_key(cap) != ledger.effect_id:
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    try:
        command = DomainCommandBuilder().build(intent, cap)
    except AuthorizationError:
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT) from None
    if command is None:
        raise RecoveryError(F.LOOKUP_PROOF_CONFLICT)
    return EffectIdentity(effect_id=ledger.effect_id, target_hash=ledger.target_hash,
                          payload_hash=ledger.payload_hash, command=command), cap, intent


def original_signed_capability(store, cap):
    with Session(store.engine) as session:
        row = session.get(CapabilitySignatureRow, cap.capability_id)
        if row is None:
            # Legacy Step 8 rows did not persist their envelope. Never mint a new
            # credential merely because a PREPARED Ledger exists.
            raise RecoveryError(F.RECOVERY_STALE)
        return SignedExecutionCapability(payload=cap, signature=row.signature)
