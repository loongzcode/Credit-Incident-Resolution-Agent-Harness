from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import ValidationError
from credit_harness.context.budget import digest
from credit_harness.domain.enums import ToolName, TransportStatus, Freshness, Completeness
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.catalog import HYPOTHESIS_RULESET_VERSION
from credit_harness.hypotheses.models import HypothesisStatus
from credit_harness.identity.models import IdentityMatch
from credit_harness.evaluation.repository import lock_case
from credit_harness.authorization.models import EffectStatus
from credit_harness.remediation.models import RemediationActionType as A
from .models import *
from .source import read_closed_source
from .repository import experience_identity, checked_experience
from .tables import ExperienceRow

SIGNATURE_CLAIMS = {
    C.HTTP_RESPONSE_STATUS: "request_transport_status", C.FUND_BUSINESS_STATUS: "fund_business_status",
    C.PAYMENT_FINALITY: "payment_finality_at_detection", C.CALLBACK_GATEWAY_RECEIVED: "callback_gateway_state",
    C.MESSAGE_CONSUME_STATUS: "message_consume_state", C.ASSET_STATUS: "asset_state",
    C.GUARANTEE_STATUS: "guarantee_state", C.ACCOUNTING_ENTRY_PRESENT: "accounting_state",
    C.CALLBACK_PROTOCOL_VERSION: "protocol_version",
}


def signature_from_facts(facts, identity=IdentityMatch.UNKNOWN):
    values = {}
    for fact in facts:
        field = SIGNATURE_CLAIMS.get(fact.claim_type)
        if field and field not in values and fact.freshness == Freshness.CURRENT and fact.completeness == Completeness.COMPLETE:
            values[field] = fact.value
    errors = [f.value for f in facts if f.claim_type == C.MESSAGE_ERROR_CODE]
    if errors:
        values["schema_mismatch_present"] = "CALLBACK_SCHEMA_MISMATCH" in errors
    return IncidentSignature(payment_identity_state=identity, **values)


class VerifiedExperiencePublisher:
    def __init__(self, repository, *, registry=None):
        self.repository, self.cases = repository, repository.cases
        self.registry = registry
        if registry is not None and registry.tenant_id != self.cases.tenant_id:
            raise MemoryError("experience registry tenant mismatch")

    def publish(self, case_id):
        try:
            with Session(self.cases.engine) as session, session.begin():
                row = lock_case(session, self.cases, case_id)
                source = read_closed_source(session, row)
                old = session.scalar(select(ExperienceRow).where(ExperienceRow.tenant_id == self.cases.tenant_id,
                    ExperienceRow.closure_id == source.closure.closure_id, ExperienceRow.schema_version == EXPERIENCE_SCHEMA_VERSION))
                if old:
                    return checked_experience(old, self.cases.tenant_id)
                experience = self._project(source)
                if self.registry is not None:
                    # Freeze trusted route metadata into the immutable primary
                    # experience. Registry descriptions never enter embeddings.
                    route = self.registry.route(case_id, session)
                    scope = SkillScope(business_domain=route.business_domain, environment=route.environment,
                        funding_partner=route.funding_partner, asset_partner=route.asset_partner,
                        guarantee_partner=route.guarantee_partner, product_code=route.product_code,
                        protocol_versions=(route.protocol_version,) if route.protocol_version else ())
                    experience = experience.model_copy(update={"partner_context": scope})
                    experience = experience.model_copy(update={"experience_id": experience_identity(experience)})
                self.repository._insert(session, experience)
                return experience
        except (ValidationError, ValueError, TypeError, KeyError):
            raise MemoryError("experience source invalid") from None

    def _project(self, source):
        case, report, closure = source.case, source.report, source.closure
        order_token = digest(dict(tenant=case.tenant_id, order=case.internal_order_id))
        by_id = {e.evidence_id: e for e in source.evidence}
        seen, sequence, confirmations = set(), [], {}
        historical = []
        detection_identity = IdentityMatch.UNKNOWN
        for call in source.calls:
            produced = sorted(eid for eid, cid in source.origins if cid == call.call_id)
            new = [eid for eid in produced if eid not in seen]
            seen.update(new)
            historical.extend(by_id[eid] for eid in new)
            sequence.append(InvestigationStep(sequence=call.sequence, tool=ToolName(call.tool),
                scope=HistoricalScope(order_token=order_token, protocol_version=call.request.get("protocol_version"),
                                      effective_at=call.request.get("effective_at")),
                new_evidence_count=len(new), produced_claim_types=tuple(sorted({by_id[eid].claim_type for eid in produced}))))
            graph = HypothesisEngine().evaluate(case, tuple(historical))
            if detection_identity == IdentityMatch.UNKNOWN and graph.payment_identity.result != IdentityMatch.UNKNOWN:
                detection_identity = graph.payment_identity.result
            for state in graph.hypotheses:
                if state.status == HypothesisStatus.CONFIRMED and state.hypothesis_id not in confirmations:
                    confirmations[state.hypothesis_id] = HistoricalHypothesis(hypothesis_id=state.hypothesis_id,
                        evidence_refs=state.decisive_evidence_refs, rule_version=state.rule_version, at_call_sequence=call.sequence)
        signature = signature_from_facts(historical, detection_identity)
        # Partner role is not currently an Evidence Claim: do not guess funding
        # vs asset role from protocol subject names. Optional partner stays unknown.
        protocols = tuple(sorted({e.protocol_version for e in historical if e.protocol_version}))
        symptom_refs = tuple(sorted(e.evidence_id for e in historical if e.claim_type in SIGNATURE_CLAIMS
            or e.claim_type in (C.MESSAGE_ERROR_CODE, C.MESSAGE_EXPECTED_FIELD_TYPE, C.MESSAGE_ACTUAL_FIELD_TYPE)))
        symptoms = HistoricalSymptom(pattern=signature, evidence_refs=symptom_refs,
            error_codes=tuple(sorted({e.value for e in historical if e.claim_type == C.MESSAGE_ERROR_CODE})),
            expected_field_types=tuple(sorted({e.value for e in historical if e.claim_type == C.MESSAGE_EXPECTED_FIELD_TYPE})),
            actual_field_types=tuple(sorted({e.value for e in historical if e.claim_type == C.MESSAGE_ACTUAL_FIELD_TYPE})))
        actions = tuple(sorted({e.action_type for e in source.effects}))
        lessons = []
        if signature.request_transport_status == TransportStatus.TIMEOUT:
            lessons.append(BoundSafetyLesson(lesson=SafetyLesson.HTTP_TIMEOUT_DOES_NOT_PROVE_PAYMENT_FAILURE,
                evidence_refs=tuple(e.evidence_id for e in historical if e.claim_type == C.HTTP_RESPONSE_STATUS)))
        l2 = tuple(a for a in actions if a in (A.REPLAY_CALLBACK_CONSUMPTION, A.REDELIVER_ASSET_NOTIFICATION))
        if l2:
            lessons.append(BoundSafetyLesson(lesson=SafetyLesson.PAYMENT_IDENTITY_REQUIRED_BEFORE_L2_REMEDIATION,
                effect_actions=l2, evidence_refs=tuple(sorted(set(report.supporting_evidence_refs)))))
        if any(a.source_status == EffectStatus.UNKNOWN.value for a in source.attempts):
            lessons.append(BoundSafetyLesson(lesson=SafetyLesson.UNKNOWN_SIDE_EFFECT_MUST_NOT_BE_BLINDLY_RETRIED,
                                             effect_actions=actions))
        # Applied + observed unconverged state after the effect supplies an actual
        # case-specific basis; don't pretend every successful effect proved this.
        if any(e.status == EffectStatus.APPLIED and any(f.observed_at > e.updated_at and (
                (f.claim_type in (C.ASSET_STATUS, C.GUARANTEE_STATUS) and f.value == "PROCESSING")
                or (f.claim_type == C.ACCOUNTING_ENTRY_PRESENT and f.value is False)) for f in historical)
               for e in source.effects):
            lessons.append(BoundSafetyLesson(lesson=SafetyLesson.APPLIED_DOES_NOT_PROVE_BUSINESS_CONVERGENCE,
                effect_actions=actions, evidence_refs=symptom_refs))
        e = VerifiedIncidentExperience(experience_id="0" * 64, source_case_id=case.case_id,
            closure_id=closure.closure_id, report_id=report.report_id, verification_snapshot_id=report.verification_snapshot_id,
            tenant_id=case.tenant_id, internal_order_ref=order_token, outcome_path=report.outcome_path,
            incident_signature=signature, partner_context=SkillScope(), protocol_context=protocols,
            observed_symptoms=symptoms, confirmed_hypotheses=tuple(confirmations[k] for k in sorted(confirmations)),
            investigation_sequence=tuple(sequence), observed_evidence_types=tuple(sorted({f.claim_type for f in historical})),
            remediation_actions=actions, side_effect_outcomes=tuple(HistoricalEffect(action=e.action_type, outcome=e.status) for e in source.effects),
            recovery_patterns=tuple(RecoveryPattern(source_status=a.source_status, result_status=a.result_status,
                completed=a.completed_at is not None) for a in source.attempts), safety_lessons=tuple(lessons),
            provenance=ExperienceProvenance(source_extractor_versions=tuple(sorted({e.metadata.extractor_version for e in historical})),
                hypothesis_rule_version=HYPOTHESIS_RULESET_VERSION, source_evidence_fingerprint=report.snapshot.evidence_fingerprint,
                source_call_history_fingerprint=report.snapshot.call_history_fingerprint,
                evaluation_policy_version=report.policy_version, verification_contract_version=report.contract_version),
            created_at=closure.closed_at, applicable_since=closure.closed_at)
        return e.model_copy(update={"experience_id": experience_identity(e)})
