"""Reduce verification context, never recompute or change verification outcomes."""
from pydantic import TypeAdapter, ValidationError
from credit_harness.evidence.models import ClaimType as C
from credit_harness.identity.models import IdentityDimension as D, IdentityMatch as M
from credit_harness.identity.payment import IDENTITY_CLAIMS
from .budget import digest
from .models import PaymentIdentityContext
from .structured_values import OpaqueBusinessRef

_REFERENCE = TypeAdapter(OpaqueBusinessRef)


class IdentityContextProjector:
    def project(self, result, index, *, preview_limit=3):
        by_id = {e.evidence_id: e for e in index.evidence}
        witnesses = sorted(result.witnesses, key=lambda w: (w.transaction_ref, w.observation_id, w.evidence_refs))
        transactions = sorted({w.transaction_ref for w in witnesses})
        safe_transactions = []
        for transaction in transactions:
            try:
                safe_transactions.append(_REFERENCE.validate_python(transaction))
            except ValidationError:
                # Preview eligibility cannot be bypassed through the full result.
                # Counts/digests still cover every original candidate.
                continue
        critical = set()

        def select(witness, claims):
            critical.update(ref for ref in witness.evidence_refs if by_id[ref].claim_type in claims)

        association = {C.PAYMENT_FINALITY, C.PAYMENT_TRANSACTION_ID, C.TRANSACTION_FUND_REQUEST_ID,
                       C.REQUEST_SENT, C.HTTP_RESPONSE_STATUS, C.GUARANTEE_STATUS, C.FUND_BUSINESS_STATUS}
        if result.result == M.MATCH and witnesses:
            # A complete single-observation witness, never a subset of its eight fields.
            critical.update(next(w for w in witnesses if w.result == M.MATCH).evidence_refs)
        else:
            # Every directly mismatching field is retained, not sampled by preview size.
            for witness in witnesses:
                if witness.mismatch_dimensions:
                    select(witness, association | {IDENTITY_CLAIMS[d] for d in witness.mismatch_dimensions})
            for dimension in result.unknown_dimensions:
                if dimension == D.TRANSACTION and len(transactions) > 1:
                    for transaction in transactions[:2]:
                        critical.update(next(w for w in witnesses if w.transaction_ref == transaction).evidence_refs)
                affected = [w for w in witnesses if dimension in w.unknown_dimensions]
                if not affected:
                    continue
                witness = affected[0]
                critical.update(witness.evidence_refs)
                # Equal-time conflicting values may span observations for the same
                # transaction. Include actual counter-evidence, not merely its count.
                candidates = [e for e in index.current(IDENTITY_CLAIMS[dimension])
                              if e.evidence_id in result.evidence_refs and e.subject.identifier == witness.transaction_ref]
                values = {}
                for e in sorted(candidates, key=lambda e: e.evidence_id):
                    values.setdefault((type(e.value).__name__, str(e.value)), e.evidence_id)
                if len(values) > 1:
                    critical.update(values[k] for k in sorted(values)[:2])
                elif not any(by_id[ref].claim_type == C.PAYMENT_TRANSACTION_ID for ref in witness.evidence_refs):
                    # Missing transaction ID still needs an actual observation origin.
                    if witness.evidence_refs:
                        critical.add(sorted(witness.evidence_refs)[0])
        return PaymentIdentityContext(
            result=result.result, mismatch_dimensions=result.mismatch_dimensions,
            unknown_dimensions=result.unknown_dimensions, verification_version=result.verification_version,
            candidate_transaction_count=len(transactions), transaction_ref_preview=tuple(safe_transactions[:preview_limit]),
            transaction_refs_digest=digest(transactions), evidence_ref_count=len(result.evidence_refs),
            evidence_refs_digest=digest(sorted(result.evidence_refs)), evidence_refs=tuple(sorted(critical)),
        )


def identity_auxiliary_refs(result, context):
    return set(result.evidence_refs) - set(context.evidence_refs)
