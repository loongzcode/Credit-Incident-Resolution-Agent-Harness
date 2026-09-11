from credit_harness.context.budget import digest
from credit_harness.domain.enums import ToolName as T
from credit_harness.evidence.models import ClaimType as C
from .deployment import deployment_witness
from credit_harness.hypotheses.rules import RuleFacts
from . import models as versions
from .models import (
    RemediationActionType as A, PreflightStatus as S, RejectReason as R, RemediationIntent,
    RemediationPreflightResult, RejectedRemediationCandidate, ValidatedRemediationCandidate,
)
from .validator import RemediationCandidateValidator, evidence_hash


class RemediationPreflightService:
    def __init__(self, reader, catalog=None):
        self.reader = reader
        self.validator = RemediationCandidateValidator(catalog)

    def preview(self, validated: ValidatedRemediationCandidate) -> RemediationPreflightResult:
        validated = ValidatedRemediationCandidate.model_validate(validated.model_dump())
        state = self.reader.read(validated.target.case_id)  # fresh durable rebuild for EVERY preview
        case, index, snapshot = state.case, state.index, state.snapshot
        facts = RuleFacts(index)
        status, reasons = S.READY_FOR_FUTURE_AUTHORIZATION, []
        action, target = validated.candidate.action_type, validated.target
        support = validated.supporting_evidence_refs
        if target.tenant_id != case.tenant_id or target.internal_order_id != case.internal_order_id:
            status, reasons = S.BLOCKED, [R.FOREIGN_CASE]
        elif (validated.catalog_version != versions.REMEDIATION_CATALOG_VERSION
              or validated.policy_version != versions.REMEDIATION_POLICY_VERSION):
            status, reasons = S.STALE, [R.STALE_POLICY]
        else:
            # A fresh, definitive "already done" observation is a terminal
            # suppression, never permission to continue with a stale proposal.
            consumed = tuple(e for e in facts.messages("CONSUMED") if e.subject.identifier == target.message_ref
                             and e.metadata.callback_event_id == target.callback_event_ref)
            delivered = tuple(e for e in facts.facts(C.ASSET_DELIVERY_STATUS, "DELIVERED", {T.ASSET_DELIVERY})
                              if any(r.value == target.delivery_ref and r.observation_id == e.observation_id
                                     for r in index.current(C.ASSET_DELIVERY_EVENT_REF)))
            converged = facts.facts(C.ASSET_STATUS, "SUCCESS", {T.ASSET})
            if (action == A.REPLAY_CALLBACK_CONSUMPTION and consumed
                    or action == A.REDELIVER_ASSET_NOTIFICATION and (delivered or converged)):
                status, reasons = S.NOT_NEEDED, [R.ACTION_ALREADY_SATISFIED]
            elif validated.snapshot_id != snapshot.snapshot_id:
                status, reasons = S.STALE, [R.STALE_SNAPSHOT]
            else:
                current = self.validator.validate(state, validated.candidate)
                if isinstance(current, RejectedRemediationCandidate):
                    status, reasons = S.BLOCKED, list(current.reason_codes)
                elif current != validated:
                    status, reasons = S.BLOCKED, [R.TARGET_BINDING_MISMATCH]
                elif action == A.REPLAY_CALLBACK_CONSUMPTION:
                    failed = next((m for m in facts.messages("FAILED") if m.subject.identifier == target.message_ref
                                   and m.metadata.callback_event_id == target.callback_event_ref), None)
                    mismatch = next((g for g in facts.mismatch_groups() if failed in g), ())
                    if mismatch:
                        deployment = deployment_witness(index, failed)
                        if not deployment:
                            status, reasons = S.BLOCKED, [R.DEPLOYMENT_STATE_UNKNOWN]
                        else:
                            values = {e.claim_type: e.value for e in deployment}
                            protocols = tuple(e for e in facts.facts(C.CALLBACK_PROTOCOL_VERSION,
                                values[C.CONSUMER_ACCEPTED_PROTOCOL_VERSION], {T.CALLBACK, T.CALLBACK_RAW})
                                if e.subject.identifier == target.callback_event_ref)
                            actual = {e.value for e in mismatch if e.claim_type == C.MESSAGE_ACTUAL_FIELD_TYPE}
                            if not protocols or actual != {values[C.CONSUMER_LOAN_NO_FIELD_TYPE]}:
                                status, reasons = S.BLOCKED, [R.DEPLOYMENT_INCOMPATIBLE]
                            else:
                                support = tuple(sorted(set(support) | set(index.refs((*deployment, *protocols)))))
        proof_hash = evidence_hash(index, support)
        fingerprint = digest(dict(case_id=case.case_id, tenant_id=case.tenant_id,
            internal_order_id=case.internal_order_id, action_type=action.value,
            target=target.model_dump(mode="json"), evidence_hash=proof_hash,
            snapshot_id=snapshot.snapshot_id, proposal_snapshot_id=validated.snapshot_id,
            status=status.value, reasons=[r.value for r in reasons],
            policy_version=versions.REMEDIATION_POLICY_VERSION, catalog_version=versions.REMEDIATION_CATALOG_VERSION))
        intent = None
        if status == S.READY_FOR_FUTURE_AUTHORIZATION:
            entry = self.validator.catalog.get(action)
            payload = dict(case_id=case.case_id, tenant_id=case.tenant_id, internal_order_id=case.internal_order_id,
                action_type=action.value, risk_level=entry.risk_level.value, target=target.model_dump(mode="json"),
                supporting_evidence_refs=support, snapshot_id=snapshot.snapshot_id, evidence_hash=proof_hash,
                catalog_version=versions.REMEDIATION_CATALOG_VERSION, policy_version=versions.REMEDIATION_POLICY_VERSION,
                preflight_fingerprint=fingerprint, requires_approval=entry.requires_future_approval,
                requires_capability=entry.requires_future_capability, status="PROPOSED")
            intent = RemediationIntent(intent_id=digest(payload), **payload)
        return RemediationPreflightResult(candidate_id=validated.candidate.candidate_id, status=status,
            reason_codes=tuple(reasons), proposal_snapshot_id=validated.snapshot_id,
            fresh_snapshot_id=snapshot.snapshot_id, preflight_fingerprint=fingerprint, intent=intent)
