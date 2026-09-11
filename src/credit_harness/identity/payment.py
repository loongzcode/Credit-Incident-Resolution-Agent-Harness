"""Pure payment identity contract over the eligible Evidence index; no I/O."""
from collections import defaultdict

from credit_harness.domain.enums import SourceKind, ToolName
from credit_harness.evidence.models import ClaimType as C, SubjectKind
from .models import IdentityDimension as D, IdentityMatch as M, PaymentIdentityResult, PaymentIdentityWitness
from .service import ReferenceIdentityVerificationService

IDENTITY_CLAIMS = {
    D.FINALITY: C.PAYMENT_FINALITY, D.TRANSACTION: C.PAYMENT_TRANSACTION_ID,
    D.REQUEST: C.TRANSACTION_FUND_REQUEST_ID, D.AMOUNT: C.PAYMENT_AMOUNT,
    D.CURRENCY: C.PAYMENT_CURRENCY, D.CUSTOMER: C.PAYMENT_CUSTOMER_REF,
    D.BENEFICIARY: C.PAYMENT_BENEFICIARY_REF, D.ACCOUNT: C.PAYMENT_ACCOUNT_REF,
}


def verify_payment_identity(index) -> PaymentIdentityResult:
    subject = index.case.financial_subject
    service = ReferenceIdentityVerificationService()
    groups = defaultdict(list)
    current = {c: tuple(e for e in index.current(c) if e.tool == ToolName.PAYMENT
                         and e.source_kind == SourceKind.PRIMARY
                         and e.subject.kind == SubjectKind.TRANSACTION) for c in IDENTITY_CLAIMS.values()}
    for items in current.values():
        for e in items:
            groups[(e.subject, e.observation_id, e.event_time)].append(e)
    anchors = tuple(e for c in (C.REQUEST_SENT, C.HTTP_RESPONSE_STATUS, C.GUARANTEE_STATUS, C.FUND_BUSINESS_STATUS)
                    for e in index.current(c)
                    if e.source_kind == SourceKind.PRIMARY and e.tool in (ToolName.TRACE, ToolName.GUARANTEE, ToolName.FUND)
                    and (e.metadata.fund_request_id or e.subject.identifier) == index.request_id)
    witnesses = []
    for (transaction, observation_id, _), items in sorted(groups.items(), key=lambda kv: str(kv[0])):
        expected = {
            D.FINALITY: "SETTLED", D.TRANSACTION: transaction.identifier,
            D.REQUEST: index.request_id if anchors else None,
            D.AMOUNT: subject.expected_principal_minor if subject else None,
            D.CURRENCY: subject.currency.value if subject else None,
            D.CUSTOMER: subject.customer_ref if subject else None,
            D.BENEFICIARY: subject.expected_beneficiary_ref if subject else None,
            D.ACCOUNT: subject.expected_account_ref if subject else None,
        }
        mismatch, unknown = [], []
        # A single observation cannot have multiple hashes or different source snapshots.
        coherent = len({(e.content_hash, e.source_as_of, e.observed_at) for e in items}) == 1
        for dimension, claim in IDENTITY_CLAIMS.items():
            values = {(type(e.value), e.value) for e in items if e.claim_type == claim}
            simultaneous = {(type(e.value), e.value) for e in current[claim] if e.subject == transaction}
            if not coherent or len(values) != 1 or len(simultaneous) != 1 or expected[dimension] is None:
                unknown.append(dimension)
                continue
            kind, value = next(iter(values))
            comparison = {
                D.CUSTOMER: service.verify_customer,
                D.BENEFICIARY: service.verify_beneficiary,
                D.ACCOUNT: service.verify_account,
            }.get(dimension, service._compare)(expected[dimension], value)
            if kind is not type(expected[dimension]):
                comparison = M.MISMATCH
            if comparison == M.MISMATCH:
                (unknown if dimension == D.FINALITY else mismatch).append(dimension)
        witnesses.append(PaymentIdentityWitness(
            case_id=index.case.case_id, observation_id=observation_id, transaction_ref=transaction.identifier,
            result=M.MISMATCH if mismatch else M.UNKNOWN if unknown else M.MATCH,
            mismatch_dimensions=tuple(mismatch), unknown_dimensions=tuple(unknown),
            evidence_refs=index.refs((*items, *anchors)),
        ))
    mismatches = tuple(sorted({d for w in witnesses for d in w.mismatch_dimensions}))
    unknowns = {d for w in witnesses for d in w.unknown_dimensions}
    if not witnesses:
        unknowns.update(IDENTITY_CLAIMS)
    # Multiple transactions require explicit reconciliation, never choose a convenient winner.
    if len({w.transaction_ref for w in witnesses}) > 1:
        unknowns.add(D.TRANSACTION)
    result = M.MISMATCH if mismatches else M.UNKNOWN if unknowns else M.MATCH
    return PaymentIdentityResult(
        case_id=index.case.case_id, result=result, mismatch_dimensions=mismatches,
        unknown_dimensions=tuple(sorted(unknowns)), witnesses=tuple(witnesses),
        evidence_refs=tuple(sorted({ref for w in witnesses for ref in w.evidence_refs})),
    )
