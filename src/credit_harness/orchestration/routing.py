"""Requirement describes missing knowledge, not a Work type or permission."""
from enum import StrEnum
from credit_harness.domain.models import Model
from credit_harness.domain.enums import ToolName as Tool
from credit_harness.context.models import Hash
from credit_harness.authorization.models import EffectStatus
from credit_harness.evaluation.models import VerificationRequirement as Q, UnresolvedVerificationRequirement
from credit_harness.recovery.repository import RECOVERABLE


# Shared by handoff and the read worker; this is not a capability registry.
VERIFICATION_TOOLS = {
    Q.PAYMENT_FINALITY: Tool.PAYMENT, Q.PAYMENT_IDENTITY: Tool.PAYMENT,
    Q.REQUEST_ASSOCIATION: Tool.GUARANTEE, Q.FUND_FINAL_STATE: Tool.FUND,
    Q.GUARANTEE_FINAL_STATE: Tool.GUARANTEE, Q.ASSET_FINAL_STATE: Tool.ASSET,
    Q.ACCOUNTING_ENTRY: Tool.ACCOUNTING, Q.CALLBACK_CONSUMPTION: Tool.MESSAGES,
    Q.ASSET_DELIVERY: Tool.ASSET_DELIVERY, Q.POST_EFFECT_MESSAGE_STATUS: Tool.MESSAGES,
    Q.POST_EFFECT_DELIVERY_STATUS: Tool.ASSET_DELIVERY,
}


class RequirementRoute(StrEnum):
    READ_VERIFICATION = "READ_VERIFICATION"
    EFFECT_RECOVERY = "EFFECT_RECOVERY"
    OPERATOR_FOLLOWUP = "OPERATOR_FOLLOWUP"
    NO_ACTION = "NO_ACTION"


class RecoveryRouteState(Model):
    effect_ref: Hash
    status: EffectStatus
    recovery_available: bool
    requires_escalation: bool


class RoutedRequirement(Model):
    requirement: Q
    route: RequirementRoute
    effect_ref: Hash | None = None


class RequirementRouter:
    def route(self, requirement: UnresolvedVerificationRequirement, *, available_tools, effects=(),
              resolved_tools=None):
        q = requirement.requirement
        if q in (Q.EFFECT_FINALITY, Q.RECOVERY_FINALITY):
            relevant = [s for s in effects if (s.effect_ref == requirement.effect_ref
                if q == Q.EFFECT_FINALITY else s.status in RECOVERABLE)]
            if not relevant:
                return (RoutedRequirement(requirement=q, route=RequirementRoute.NO_ACTION
                    if q == Q.RECOVERY_FINALITY else RequirementRoute.OPERATOR_FOLLOWUP),)
            if q == Q.RECOVERY_FINALITY:
                recoverable = [s for s in relevant if s.recovery_available and not s.requires_escalation]
                # The aggregate is machine-resolvable while any effect can
                # continue. Individual EFFECT_FINALITY requirements remain specific.
                relevant = recoverable or relevant
            return tuple(RoutedRequirement(requirement=q, effect_ref=s.effect_ref,
                route=RequirementRoute.NO_ACTION if s.status not in RECOVERABLE else
                    RequirementRoute.EFFECT_RECOVERY if s.status in RECOVERABLE
                    and s.recovery_available and not s.requires_escalation
                    else RequirementRoute.OPERATOR_FOLLOWUP)
                for s in sorted(relevant, key=lambda s: s.effect_ref))
        return (RoutedRequirement(requirement=q,
            route=RequirementRoute.READ_VERIFICATION if (resolved_tools if resolved_tools is not None
                else VERIFICATION_TOOLS).get(q) in available_tools
                else RequirementRoute.OPERATOR_FOLLOWUP),)
