from uuid import uuid4
from sqlalchemy.orm import Session
from sqlalchemy import select
from credit_harness.cases.tables import CaseRow
from credit_harness.cases.repository import utc_now
from credit_harness.domain.enums import ToolName as T, Freshness, Completeness
from credit_harness.evidence.models import ClaimType as C, SubjectKind
from credit_harness.context.budget import digest
from credit_harness.identity.models import IdentityMatch
from credit_harness.hypotheses.models import PriorityClass
from credit_harness.authorization.models import EffectStatus
from credit_harness.remediation.models import RemediationActionType as A
from .contract import EvaluationPolicy, CreditGuaranteeDisbursementVerificationContractV1
from .snapshot import VerificationSnapshotReader, UNRESOLVED_EFFECT_STATES
from .models import (EvaluationDimension as D, DimensionStatus as S, EvaluationVerdict as V,
    EvaluationReason as R, VerificationRequirement as Q, OutcomePath as P,
    EvaluationDimensionResult, UnresolvedVerificationRequirement, EvaluationReport)


def aggregate(statuses):
    statuses = tuple(statuses)
    return S.FAIL if S.FAIL in statuses else S.INCONCLUSIVE if S.INCONCLUSIVE in statuses else S.PASS


def report_identity(report):
    return digest(report.model_dump(mode="json", exclude={"report_id", "evaluation_run_id", "created_at"}))


class IndependentEvaluator:
    """Read-only, deterministic evaluation. No tool, adapter or model client.

    Report persistence is explicit and separate; evaluate never changes durable
    state, including Case lifecycle or an APPLIED ledger.
    """
    def __init__(self, cases, *, clock=utc_now, policy=None, contract=None, catalog=None):
        self.cases, self.clock = cases, clock
        self.policy = policy or EvaluationPolicy()
        self.contract = contract or CreditGuaranteeDisbursementVerificationContractV1()
        self.reader = VerificationSnapshotReader(cases, self.policy, self.contract, catalog)

    def evaluate(self, case_id):
        with Session(self.cases.engine) as session, session.begin():
            if session.bind.dialect.name == "sqlite":
                # sqlite legacy transaction mode otherwise does not start a
                # database snapshot for SELECT. This BEGIN performs no write.
                session.connection().exec_driver_sql("BEGIN")
            else:
                # All publishers lock Case first; a shared read lock gives this
                # report a consistent cut without changing any durable row.
                session.execute(select(CaseRow.case_id).where(CaseRow.case_id == case_id,
                    CaseRow.tenant_id == self.cases.tenant_id).with_for_update(read=True))
            return self._evaluate(session, case_id, self.clock())

    def _evaluate(self, session, case_id, now):
        state = self.reader.read(session, case_id, now)
        index = state.index
        parts = {d: [] for d in D}
        unresolved = []

        def check(dimension, status, reason=None, refs=(), effects=(), claims=(), requirement=None):
            parts[dimension].append(EvaluationDimensionResult(dimension=dimension, status=status,
                reason_codes=(reason,) if reason else (), supporting_evidence_refs=tuple(sorted(set(refs))),
                side_effect_refs=tuple(sorted(set(effects))), required_claims_missing=tuple(claims)
                    if status == S.INCONCLUSIVE else ()))
            if requirement and status in (S.FAIL, S.INCONCLUSIVE):
                unresolved.append(UnresolvedVerificationRequirement(requirement=requirement, dimension=dimension,
                    reason_code=reason, required_claims=tuple(claims), effect_ref=next(iter(effects), None)))

        def values(claim, tool=None):
            return tuple(e for e in index.current(claim) if tool is None or e.tool == tool)

        def refs(items):
            return tuple(e.evidence_id for e in items)

        money = values(C.PAYMENT_FINALITY, T.PAYMENT)
        finalities = {e.value for e in money}
        path = None
        if len(finalities) > 1:
            check(D.MONEY, S.FAIL, R.MONEY_STATE_CONTRADICTION, refs(money), requirement=Q.PAYMENT_FINALITY)
        elif finalities == {"SETTLED"}:
            path = P.SETTLED_PATH
            check(D.MONEY, S.PASS, refs=refs(money))
        elif finalities == {"NOT_EXECUTED"} and all(e.subject.kind == SubjectKind.FUND_REQUEST
                and e.subject.identifier == index.request_id and e.metadata.fund_request_id == index.request_id for e in money):
            settled_history = tuple(e for e in state.evidence.evidence if e.tool == T.PAYMENT
                and e.claim_type == C.PAYMENT_FINALITY and e.value == "SETTLED"
                and e.metadata.fund_request_id == index.request_id
                and e.source_kind.value == "PRIMARY" and e.freshness == Freshness.CURRENT
                and e.completeness == Completeness.COMPLETE)
            if settled_history:
                # V1 has no reversal path. A new no-effect assertion cannot
                # erase a previously observed settled transfer for this request.
                check(D.MONEY, S.FAIL, R.MONEY_STATE_CONTRADICTION, refs((*money, *settled_history)),
                      requirement=Q.PAYMENT_FINALITY)
            else:
                path = P.NO_DISBURSEMENT_PATH
                check(D.MONEY, S.PASS, refs=refs(money))
        else:
            check(D.MONEY, S.INCONCLUSIVE, R.PAYMENT_FINALITY_UNKNOWN, refs(money),
                  claims=(C.PAYMENT_FINALITY,), requirement=Q.PAYMENT_FINALITY)

        identity = state.graph.payment_identity
        if path == P.NO_DISBURSEMENT_PATH:
            check(D.IDENTITY, S.NOT_APPLICABLE, refs=refs(money))
        elif identity.result == IdentityMatch.MISMATCH:
            check(D.IDENTITY, S.FAIL, R.PAYMENT_IDENTITY_MISMATCH, identity.evidence_refs,
                  requirement=Q.PAYMENT_IDENTITY)
        elif identity.result == IdentityMatch.MATCH:
            check(D.IDENTITY, S.PASS, refs=identity.evidence_refs)
        else:
            check(D.IDENTITY, S.INCONCLUSIVE, R.PAYMENT_IDENTITY_UNKNOWN, identity.evidence_refs,
                  requirement=Q.PAYMENT_IDENTITY)

        def expect(claim, expected, reason, requirement):
            items = values(claim)
            observed = {(type(e.value), e.value) for e in items}
            if not items:
                check(D.STATE_CONVERGENCE, S.INCONCLUSIVE, reason, claims=(claim,), requirement=requirement)
            elif observed == {(type(expected), expected)}:
                check(D.STATE_CONVERGENCE, S.PASS, refs=refs(items))
            else:
                # Explicit mutually exclusive observations and terminal wrong
                # values do not get excused by an asynchronous grace window.
                transitional = observed <= {(str, "PROCESSING"), (str, "PENDING"),
                    (str, "NOT_ATTEMPTED"), (bool, False)}
                waiting = transitional and state.in_grace
                check(D.STATE_CONVERGENCE, S.INCONCLUSIVE if waiting else S.FAIL,
                      R.CONVERGENCE_WINDOW_OPEN if waiting else reason, refs(items), requirement=requirement)

        state_rules = {
            C.FUND_BUSINESS_STATUS: (R.FUND_NOT_CONVERGED, Q.FUND_FINAL_STATE),
            C.GUARANTEE_STATUS: (R.GUARANTEE_NOT_CONVERGED, Q.GUARANTEE_FINAL_STATE),
            C.ASSET_STATUS: (R.ASSET_NOT_CONVERGED, Q.ASSET_FINAL_STATE),
            C.ACCOUNTING_ENTRY_PRESENT: (R.ACCOUNTING_NOT_CONVERGED, Q.ACCOUNTING_ENTRY),
        }
        if path:
            for claim, expected in self.contract.state_expectations(path):
                expect(claim, expected, *state_rules[claim])
        else:
            check(D.STATE_CONVERGENCE, S.INCONCLUSIVE, R.BUSINESS_OUTCOME_NOT_CONVERGED)
        if path == P.SETTLED_PATH and self.contract.settled_requires_callback:
            gateways = values(C.CALLBACK_GATEWAY_RECEIVED)
            signatures = values(C.CALLBACK_SIGNATURE_VERIFIED)
            messages = values(C.MESSAGE_CONSUME_STATUS)
            matched = []
            for g in gateways:
                sig = tuple(s for s in signatures if s.subject == g.subject and s.observation_id == g.observation_id
                            and s.value is True)
                consumed = tuple(m for m in messages if m.metadata.callback_event_id == g.subject.identifier
                                 and m.value == "CONSUMED" and m.event_time is not None and g.event_time is not None
                                 and m.event_time >= g.event_time)
                if g.value is True and sig and consumed:
                    matched.extend((g, *sig, *consumed))
            if matched and all(m.value == "CONSUMED" for m in messages):
                check(D.STATE_CONVERGENCE, S.PASS, refs=refs(matched))
            else:
                known_failed = bool(messages and any(m.value == "FAILED" for m in messages))
                check(D.STATE_CONVERGENCE, S.FAIL if known_failed and not state.in_grace else S.INCONCLUSIVE,
                      R.CALLBACK_NOT_CONSUMED, refs(messages),
                      claims=(C.CALLBACK_GATEWAY_RECEIVED, C.CALLBACK_SIGNATURE_VERIFIED, C.MESSAGE_CONSUME_STATUS),
                      requirement=Q.CALLBACK_CONSUMPTION)
        if path == P.SETTLED_PATH and self.contract.settled_requires_delivery:
            expect(C.ASSET_DELIVERY_STATUS, "DELIVERED", R.DELIVERY_NOT_DELIVERED, Q.ASSET_DELIVERY)

        for effect in state.effects:
            ledger, target = effect.ledger, effect.target
            effect_refs = (ledger.effect_id,)
            if ledger.status in UNRESOLVED_EFFECT_STATES:
                check(D.SIDE_EFFECT, S.INCONCLUSIVE, R.UNRESOLVED_SIDE_EFFECT, effects=effect_refs,
                      requirement=Q.EFFECT_FINALITY)
                continue
            if ledger.status != EffectStatus.APPLIED:
                check(D.SIDE_EFFECT, S.NOT_APPLICABLE, effects=effect_refs)
                continue
            if ledger.action_type not in (A.REPLAY_CALLBACK_CONSUMPTION, A.REDELIVER_ASSET_NOTIFICATION):
                # Administrative completion has no business-outcome authority.
                check(D.SIDE_EFFECT, S.NOT_APPLICABLE, effects=effect_refs)
                continue
            replay = ledger.action_type == A.REPLAY_CALLBACK_CONSUMPTION
            claim = C.MESSAGE_CONSUME_STATUS if replay else C.ASSET_DELIVERY_STATUS
            requirement = Q.POST_EFFECT_MESSAGE_STATUS if replay else Q.POST_EFFECT_DELIVERY_STATUS
            items = values(claim, T.MESSAGES if replay else T.ASSET_DELIVERY)
            matched = []
            for e in items:
                if (e.observed_at <= ledger.updated_at or e.source_as_of < ledger.updated_at
                        or e.event_time is None or e.event_time < ledger.created_at):
                    continue
                if replay:
                    associated = e.subject.identifier == target.message_ref and e.metadata.callback_event_id == target.callback_event_ref
                else:
                    associated = any(r.value == target.delivery_ref and r.observation_id == e.observation_id
                                     for r in values(C.ASSET_DELIVERY_EVENT_REF, T.ASSET_DELIVERY))
                if associated:
                    matched.append(e)
            if not matched:
                check(D.SIDE_EFFECT, S.INCONCLUSIVE, R.POST_EFFECT_EVIDENCE_MISSING, effects=effect_refs,
                      claims=(claim,), requirement=requirement)
            elif {e.value for e in matched} == {"CONSUMED" if replay else "DELIVERED"}:
                check(D.SIDE_EFFECT, S.PASS, refs=refs(matched), effects=effect_refs)
            else:
                check(D.SIDE_EFFECT, S.FAIL, R.CALLBACK_NOT_CONSUMED if replay else R.DELIVERY_NOT_DELIVERED,
                      refs(matched), effects=effect_refs, requirement=requirement)

        if not state.evidence.provenance_valid:
            check(D.EVIDENCE_SUFFICIENCY, S.INCONCLUSIVE, R.PROVENANCE_INVALID, requirement=Q.PROVENANCE)
        if state.pending_reads:
            check(D.EVIDENCE_SUFFICIENCY, S.INCONCLUSIVE, R.PENDING_READ_DISPATCH, requirement=Q.READ_PUBLICATION)
        relevant_tools = {T.PAYMENT, T.TRACE, T.FUND, T.GUARANTEE, T.ASSET, T.ACCOUNTING}
        if path == P.SETTLED_PATH:
            relevant_tools.update((T.CALLBACK, T.CALLBACK_RAW, T.MESSAGES, T.ASSET_DELIVERY))
        quality_inputs = tuple(e for e in state.latest if e.tool in relevant_tools)
        if any(e.freshness == Freshness.STALE or (e.source_as_of and
               (now - e.source_as_of).total_seconds() > self.policy.evidence_max_age_seconds) for e in quality_inputs):
            check(D.EVIDENCE_SUFFICIENCY, S.INCONCLUSIVE, R.EVIDENCE_STALE)
        if any(e.completeness != Completeness.COMPLETE for e in quality_inputs):
            check(D.EVIDENCE_SUFFICIENCY, S.INCONCLUSIVE, R.EVIDENCE_INCOMPLETE)
        for gap in state.graph.open_gaps:
            if gap.priority_class == PriorityClass.SAFETY_CRITICAL and not self.contract.exempt_gap(gap, path):
                claims = tuple(c for c in gap.required_claim_types if isinstance(c, C))
                check(D.EVIDENCE_SUFFICIENCY, S.INCONCLUSIVE, R.OPEN_SAFETY_CRITICAL_GAP,
                      claims=claims, requirement=Q.SAFETY_GAP)
        if state.policy_invalid:
            check(D.POLICY, S.FAIL, R.FORBIDDEN_EFFECT_OBSERVED, requirement=Q.SAFE_POLICY)
        if state.recovery_unresolved or any(e.ledger.status in UNRESOLVED_EFFECT_STATES for e in state.effects):
            check(D.RECOVERY, S.INCONCLUSIVE, R.RECOVERY_UNRESOLVED, requirement=Q.RECOVERY_FINALITY)
        if state.case.status.is_terminal and (state.pending_reads or state.recovery_unresolved or any(
                e.ledger.status in UNRESOLVED_EFFECT_STATES for e in state.effects)):
            check(D.RECOVERY, S.FAIL, R.CLOSURE_INVARIANT_VIOLATION)

        check(D.OUTCOME, aggregate(p.status for d in (D.MONEY, D.IDENTITY, D.STATE_CONVERGENCE, D.SIDE_EFFECT)
                                  for p in parts[d]))
        dimensions = []
        for d in D:
            items = parts[d]
            status = S.NOT_APPLICABLE if items and all(i.status == S.NOT_APPLICABLE for i in items) else aggregate(i.status for i in items)
            dimensions.append(EvaluationDimensionResult(dimension=d, status=status,
                reason_codes=tuple(sorted({r for i in items for r in i.reason_codes})),
                supporting_evidence_refs=tuple(sorted({r for i in items for r in i.supporting_evidence_refs})),
                side_effect_refs=tuple(sorted({r for i in items for r in i.side_effect_refs})),
                required_claims_missing=tuple(sorted({r for i in items for r in i.required_claims_missing}))))
        verdict = V(aggregate(d.status for d in dimensions if d.dimension in self.policy.required_dimensions).value)
        # Missing core contract domains can never be made optional by a config typo.
        if set(self.policy.required_dimensions) != set(D):
            raise ValueError("V1 requires all verification domains")
        report = EvaluationReport(report_id="0" * 64, evaluation_run_id=str(uuid4()), case_id=case_id,
            verification_snapshot_id=state.snapshot.verification_snapshot_id, snapshot=state.snapshot,
            contract_version=self.contract.version, policy_version=self.policy.version, outcome_path=path,
            overall_verdict=verdict, dimensions=tuple(dimensions),
            reason_codes=tuple(sorted({r for d in dimensions for r in d.reason_codes})),
            supporting_evidence_refs=tuple(sorted({r for d in dimensions for r in d.supporting_evidence_refs})),
            side_effect_refs=tuple(sorted(e.ledger.effect_id for e in state.effects)),
            unresolved_requirements=tuple(sorted(set(unresolved), key=lambda u: (u.dimension, u.requirement, u.reason_code, u.effect_ref or ""))),
            created_at=now)
        return report.model_copy(update={"report_id": report_identity(report)})
