from credit_harness.cases.models import CaseStatus as S
from .models import (
    RemediationActionType as A, ActionRiskLevel as L, EffectScope as E,
    EvidenceCondition as C, GapRequirement as G, RejectReason as R, RemediationActionContract, SelectionRole,
)


def contract(action, risk, description, conditions, scope, role):
    l2 = risk == L.L2_SINGLE_ORDER_SIDE_EFFECT
    return RemediationActionContract(action_type=action, selection_role=role, risk_level=risk, description=description,
        required_evidence_conditions=conditions, required_identity_state="MATCH" if l2 else None,
        required_case_state=(S.INVESTIGATING, S.ESCALATED) if l2 else tuple(S) if action == A.NO_REMEDIATION
                            else (S.NEW, S.INVESTIGATING, S.WAITING, S.ESCALATED),
        forbidden_if=(R.IDENTITY_UNKNOWN, R.IDENTITY_MISMATCH, R.UNRESOLVED_SAFETY_GAP,
                      R.ACTION_ALREADY_SATISFIED) if l2 else (),
        required_open_or_resolved_gaps=((G.NO_OPEN_SAFETY_GAP,
            G.CURRENT_COMPATIBLE_DEPLOYMENT_IF_SCHEMA_FAILURE) if action == A.REPLAY_CALLBACK_CONSUMPTION
            else (G.NO_OPEN_SAFETY_GAP,) if l2 else ()),
        max_effect_scope=scope, requires_future_approval=l2,
        requires_future_capability=action != A.NO_REMEDIATION)


class RemediationActionCatalog:
    entries = (
        contract(A.NO_REMEDIATION, L.L0_READ_ONLY, "保持当前业务状态，仅记录本轮不建议修复。", (), E.NONE, SelectionRole.NO_ACTION),
        contract(A.REQUEST_OPERATOR_REVIEW, L.L1_ADMINISTRATIVE, "建议人工获取缺失事实或检查被阻断问题。", (C.OPEN_PROBLEM,), E.RECONCILIATION, SelectionRole.HUMAN_FALLBACK),
        contract(A.CREATE_RECONCILIATION_TASK, L.L1_ADMINISTRATIVE, "建议将已观察到的未收敛送入对账队列；本阶段不创建任务。", (C.OBSERVED_DIVERGENCE,), E.RECONCILIATION, SelectionRole.ADMINISTRATIVE_REMEDIATION),
        contract(A.REPLAY_CALLBACK_CONSUMPTION, L.L2_SINGLE_ORDER_SIDE_EFFECT, "建议重放一个已验签且关联明确的失败消息；不是重新放款。",
                 (C.PAYMENT_IDENTITY_MATCH, C.SIGNED_FAILED_CALLBACK), E.MESSAGE, SelectionRole.DIRECT_REMEDIATION),
        contract(A.REDELIVER_ASSET_NOTIFICATION, L.L2_SINGLE_ORDER_SIDE_EFFECT, "建议重投一条尚未成功的既有资产通知。",
                 (C.PAYMENT_IDENTITY_MATCH, C.UNCONVERGED_ASSET_DELIVERY), E.DELIVERY, SelectionRole.DIRECT_REMEDIATION),
    )

    def get(self, action):
        return next((c for c in self.entries if c.action_type == action), None)

    def selection_key(self, candidate):
        entry = self.get(candidate.action_type)
        # Roles are static catalog policy, never inferred from model rationale.
        roles = {SelectionRole.DIRECT_REMEDIATION: 0, SelectionRole.ADMINISTRATIVE_REMEDIATION: 1,
                 SelectionRole.HUMAN_FALLBACK: 2, SelectionRole.NO_ACTION: 3}
        # One message and one delivery have equal scope; neither implies bulk.
        scopes = {E.NONE: 0, E.MESSAGE: 1, E.DELIVERY: 1, E.RECONCILIATION: 2,
                  E.ORDER_STATE: 3, E.MONEY: 4, E.BULK: 5}
        risks = {risk: i for i, risk in enumerate(L)}
        return (roles[entry.selection_role], scopes[entry.max_effect_scope], risks[entry.risk_level],
                entry.action_type.value, candidate.candidate_id)


class ForbiddenRemediationPolicy:
    def check(self, entry):
        if entry is None:
            return (R.UNKNOWN_ACTION,)
        if entry.risk_level in (L.L3_MONEY_MOVEMENT, L.L4_BULK_OR_SYSTEMIC):
            return (R.MONEY_MOVEMENT_PROHIBITED,)
        if entry.max_effect_scope in (E.MONEY, E.ORDER_STATE, E.BULK):
            return (R.ACTION_NOT_ALLOWED,)
        return ()
