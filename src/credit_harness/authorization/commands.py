from typing import Annotated, Literal, Protocol
from pydantic import Field, TypeAdapter, model_validator
from credit_harness.domain.models import Model
from credit_harness.context.structured_values import OpaqueSubjectRef
from credit_harness.context.budget import digest
from credit_harness.remediation.models import RemediationActionType as A
from .models import AuthorizationCode as C, AuthorizationError, SideEffectReceipt


class Command(Model):
    tenant_id: OpaqueSubjectRef
    case_id: OpaqueSubjectRef
    internal_order_id: OpaqueSubjectRef

    @model_validator(mode="after")
    def no_alias(self):
        if any(isinstance(v, str) and v.startswith("EXTREF-") for v in self.model_dump().values()):
            raise ValueError("model aliases are not command addresses")
        return self


class ReplayCallbackCommand(Command):
    action_type: Literal[A.REPLAY_CALLBACK_CONSUMPTION] = A.REPLAY_CALLBACK_CONSUMPTION
    message_ref: OpaqueSubjectRef
    callback_event_ref: OpaqueSubjectRef


class RedeliverAssetNotificationCommand(Command):
    action_type: Literal[A.REDELIVER_ASSET_NOTIFICATION] = A.REDELIVER_ASSET_NOTIFICATION
    delivery_ref: OpaqueSubjectRef


class CreateReconciliationTaskCommand(Command):
    action_type: Literal[A.CREATE_RECONCILIATION_TASK] = A.CREATE_RECONCILIATION_TASK


class RequestOperatorReviewCommand(Command):
    action_type: Literal[A.REQUEST_OPERATOR_REVIEW] = A.REQUEST_OPERATOR_REVIEW


DomainCommand = Annotated[ReplayCallbackCommand | RedeliverAssetNotificationCommand |
                          CreateReconciliationTaskCommand | RequestOperatorReviewCommand,
                          Field(discriminator="action_type")]
COMMAND_ADAPTER = TypeAdapter(DomainCommand)


class DomainCommandBuilder:
    TYPES = {A.REPLAY_CALLBACK_CONSUMPTION: ReplayCallbackCommand,
             A.REDELIVER_ASSET_NOTIFICATION: RedeliverAssetNotificationCommand,
             A.CREATE_RECONCILIATION_TASK: CreateReconciliationTaskCommand,
             A.REQUEST_OPERATOR_REVIEW: RequestOperatorReviewCommand}

    def for_intent(self, intent):
        if intent.action_type == A.NO_REMEDIATION:
            return None
        cls = self.TYPES.get(intent.action_type)
        if cls is None:
            raise AuthorizationError(C.ACTION_NOT_ALLOWED)
        values = dict(tenant_id=intent.tenant_id, case_id=intent.case_id, internal_order_id=intent.internal_order_id)
        if cls is ReplayCallbackCommand:
            values.update(message_ref=intent.target.message_ref, callback_event_ref=intent.target.callback_event_ref)
        elif cls is RedeliverAssetNotificationCommand:
            values.update(delivery_ref=intent.target.delivery_ref)
        return cls(**values)

    def payload_hash(self, intent):
        command = self.for_intent(intent)
        return digest(command.model_dump(mode="json") if command else {"noop_target": intent.target.model_dump(mode="json")})

    def build(self, intent, capability):
        if (intent.intent_id != capability.intent_id or intent.case_id != capability.case_id
                or intent.tenant_id != capability.tenant_id or intent.internal_order_id != capability.internal_order_id
                or intent.action_type != capability.action_type):
            raise AuthorizationError(C.CAPABILITY_SCOPE_MISMATCH)
        if digest(intent.target.model_dump(mode="json")) != capability.target_hash:
            raise AuthorizationError(C.CAPABILITY_TARGET_MISMATCH)
        if self.payload_hash(intent) != capability.payload_hash:
            raise AuthorizationError(C.CAPABILITY_PAYLOAD_MISMATCH)
        return self.for_intent(intent)


def effect_key(capability):
    # Exclude snapshot/intent IDs: new evidence must not rename the same effect.
    return digest(dict(tenant=capability.tenant_id, case=capability.case_id, order=capability.internal_order_id,
                       action=capability.action_type.value, target_hash=capability.target_hash))


class RemediationSideEffectAdapter(Protocol):
    def dispatch(self, command: DomainCommand, correlation_id: str) -> SideEffectReceipt: ...
