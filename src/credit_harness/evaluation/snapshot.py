from dataclasses import dataclass
from datetime import datetime
from sqlalchemy import inspect, select
from pydantic import ValidationError
from credit_harness.cases.repository import hydrate, CallState
from credit_harness.cases.models import Case
from credit_harness.domain.enums import Freshness, Completeness, SourceKind
from credit_harness.evidence.models import ClaimType as C, Evidence
from credit_harness.evidence.verification import read_verified_evidence, VerifiedEvidenceRead
from credit_harness.hypotheses.engine import HypothesisEngine
from credit_harness.hypotheses.index import EvidenceIndex
from credit_harness.context.budget import digest
from credit_harness.authorization import models as av
from credit_harness.authorization.models import SideEffectLedger, ExecutionCapability, EffectStatus
from credit_harness.authorization.tables import EffectRow, CapabilityRow, AuthorizedIntentRow, ApprovalRow
from credit_harness.authorization.commands import DomainCommandBuilder, effect_key
from credit_harness.remediation import models as rv
from credit_harness.remediation.models import RemediationIntent, RemediationTarget
from credit_harness.remediation.catalog import RemediationActionCatalog, ForbiddenRemediationPolicy
from credit_harness.recovery.tables import EffectRecoveryStateRow, EffectRecoveryAttemptRow, ReadDispatchRecoveryRow
from .models import VerificationSnapshot

UNRESOLVED_EFFECT_STATES = frozenset((EffectStatus.PREPARED, EffectStatus.DISPATCHED,
                                    EffectStatus.ACCEPTED, EffectStatus.UNKNOWN))


def row_data(row):
    return {col.key: getattr(row, col.key) for col in row.__table__.columns}


def table_exists(session, table):
    connection = session.connection()
    schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
    return inspect(connection).has_table(table.__tablename__, schema=schema)


def latest_observation_evidence(evidence):
    watermarks = {}
    for e in evidence:
        key = (e.tool, e.metadata.scope)
        watermarks[key] = max(watermarks.get(key, e.observed_at), e.observed_at)
    return tuple(e for e in evidence if e.observed_at == watermarks[(e.tool, e.metadata.scope)])


def eligible(e, now, policy):
    return (e.claim_type != C.SOURCE_LOOKUP_STATUS and e.freshness == Freshness.CURRENT
            and e.completeness == Completeness.COMPLETE and e.source_kind == SourceKind.PRIMARY
            and e.source_as_of is not None and e.observed_at <= now
            and 0 <= (now - e.source_as_of).total_seconds() <= policy.evidence_max_age_seconds)


@dataclass(frozen=True)
class BoundEffect:
    ledger: SideEffectLedger
    target: RemediationTarget


@dataclass(frozen=True)
class EvaluationState:
    case: Case
    evidence: VerifiedEvidenceRead
    latest: tuple[Evidence, ...]
    index: EvidenceIndex
    graph: object
    effects: tuple[BoundEffect, ...]
    policy_invalid: bool
    recovery_unresolved: bool
    pending_reads: bool
    in_grace: bool
    snapshot: VerificationSnapshot


class VerificationSnapshotReader:
    def __init__(self, cases, policy, contract, catalog=None):
        self.cases, self.policy, self.contract = cases, policy, contract
        self.catalog = catalog or RemediationActionCatalog()

    def read(self, session, case_id, now):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("evaluation clock must be timezone aware")
        case_row = self.cases._row(session, case_id)
        case = hydrate(case_row)
        read = read_verified_evidence(session, case_row)
        latest = latest_observation_evidence(read.evidence)
        current = tuple(e for e in latest if eligible(e, now, self.policy))
        index = EvidenceIndex(case, current)
        graph = HypothesisEngine().evaluate(case, current)
        effects, raw_effects, authorization = [], [], []
        invalid = False
        if table_exists(session, EffectRow):
            for row in session.scalars(select(EffectRow).where(EffectRow.case_id == case_id).order_by(EffectRow.effect_id)):
                raw_effects.append(row_data(row))
                try:
                    ledger = SideEffectLedger.model_validate(row.payload)
                    if (row.effect_id != ledger.effect_id or row.status != ledger.status.value
                            or row.capability_id != ledger.capability_id or row.idempotency_key != ledger.idempotency_key
                            or ledger.tenant_id != case.tenant_id or ledger.case_id != case_id
                            or ledger.internal_order_id != case.internal_order_id):
                        raise ValueError("ledger scope or storage mismatch")
                    ir = session.get(AuthorizedIntentRow, ledger.intent_id)
                    cr = session.get(CapabilityRow, ledger.capability_id)
                    if ir is None or cr is None:
                        raise ValueError("missing authorization binding")
                    intent, cap = RemediationIntent.model_validate(ir.payload), ExecutionCapability.model_validate(cr.payload)
                    # Only hashes of authorization data enter Snapshot. No actor,
                    # signature, rationale or raw receipt enters a report.
                    authorization.append((ir.intent_id, digest(ir.payload), cr.capability_id, cr.payload, cr.used_effect_id))
                    if cap.approval_id:
                        approval = session.get(ApprovalRow, cap.approval_id)
                        if approval is None:
                            raise ValueError("missing approval")
                        authorization.append((approval.approval_id, digest(approval.payload), approval.status))
                    if (intent.intent_id != digest(intent.model_dump(mode="json", exclude={"intent_id"}))
                            or cr.used_effect_id != ledger.effect_id or cap.case_id != case_id
                            or cap.tenant_id != case.tenant_id or cap.intent_id != ledger.intent_id
                            or cap.action_type != ledger.action_type or cap.target_hash != ledger.target_hash
                            or cap.payload_hash != ledger.payload_hash or cap.approval_id != ledger.approval_id
                            or effect_key(cap) != ledger.effect_id):
                        raise ValueError("effect identity mismatch")
                    DomainCommandBuilder().build(intent, cap)
                    entry = self.catalog.get(ledger.action_type)
                    if ForbiddenRemediationPolicy().check(entry) or intent.risk_level != entry.risk_level:
                        invalid = True
                    effects.append(BoundEffect(ledger, intent.target))
                except (ValidationError, ValueError, KeyError, AttributeError, av.AuthorizationError):
                    invalid = True
        recovery = []
        recovery_unresolved = False
        if table_exists(session, EffectRecoveryStateRow):
            for row in session.scalars(select(EffectRecoveryStateRow).where(
                    EffectRecoveryStateRow.case_id == case_id).order_by(EffectRecoveryStateRow.effect_id)):
                recovery.append(row_data(row))
                if row.tenant_id != case.tenant_id or row.requires_escalation:
                    recovery_unresolved = True
                if row.lease_token and row.lease_until and row.lease_until > now.timestamp():
                    recovery_unresolved = True
            ids = [r["effect_id"] for r in raw_effects]
            for row in session.scalars(select(EffectRecoveryAttemptRow).where(
                    EffectRecoveryAttemptRow.effect_id.in_(ids)).order_by(EffectRecoveryAttemptRow.recovery_id)):
                recovery.append(row_data(row))
                if row.completed_at is None:
                    recovery_unresolved = True
        if table_exists(session, ReadDispatchRecoveryRow):
            recovery.extend(row_data(r) for r in session.scalars(select(ReadDispatchRecoveryRow)
                .where(ReadDispatchRecoveryRow.case_id == case_id).order_by(ReadDispatchRecoveryRow.call_id)))
        pending = any(c["state"] in (CallState.DISPATCHED.value, CallState.ERROR.value) for c in read.calls)
        # Applied time anchors an asynchronous convergence window. Without an
        # effect, last known business event time is the observable anchor.
        anchors = [e.ledger.updated_at for e in effects if e.ledger.status == EffectStatus.APPLIED]
        if not anchors:
            anchors = [e.event_time for e in current if e.event_time is not None]
        in_grace = bool(anchors and (now - max(anchors)).total_seconds() < self.policy.convergence_grace_seconds)
        case_payload = case.model_dump(mode="json")
        case_payload["scope"] = dict(allowed_order_ids=sorted(case.scope.allowed_order_ids),
                                     allowed_tools=sorted(t.value for t in case.scope.allowed_tools))
        payload = dict(case_id=case_id, tenant_id=case.tenant_id, case_revision=case.updated_at,
            case_fingerprint=digest(case_payload), evidence_fingerprint=read.fingerprint,
            payment_identity_fingerprint=digest(graph.payment_identity.model_dump(mode="json")),
            current_state_fingerprint=digest(dict(current=[e.model_dump(mode="json") for e in current],
                graph=graph.input_fingerprint, in_grace=in_grace, provenance_valid=read.provenance_valid)),
            side_effect_ledger_fingerprint=digest(raw_effects), recovery_fingerprint=digest(recovery),
            call_history_fingerprint=digest(read.calls), authorization_policy_fingerprint=digest(dict(
                version=av.AUTHORIZATION_POLICY_VERSION, catalog_version=rv.REMEDIATION_CATALOG_VERSION,
                remediation_policy=rv.REMEDIATION_POLICY_VERSION,
                catalog=[e.model_dump(mode="json") for e in self.catalog.entries], bindings=authorization)),
            evaluation_policy_version=self.policy.version, verification_contract_version=self.contract.version,
            evaluation_policy_fingerprint=digest(self.policy.model_dump(mode="json")),
            verification_contract_fingerprint=digest(self.contract.model_dump(mode="json")))
        payload["case_revision"] = case.updated_at.isoformat()
        snapshot = VerificationSnapshot(verification_snapshot_id="0" * 64, **payload)
        # Hash the public canonical representation, including Pydantic's UTC Z
        # serialization, so an auditor can recompute the exported identity.
        snapshot = snapshot.model_copy(update={"verification_snapshot_id": digest(
            snapshot.model_dump(mode="json", exclude={"verification_snapshot_id"}))})
        return EvaluationState(case, read, latest, index, graph, tuple(effects), invalid,
                               recovery_unresolved, pending, in_grace, snapshot)
