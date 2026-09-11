from types import MappingProxyType

from credit_harness.context.models import ReasoningContextSnapshot, ContextTrustClass as T
from credit_harness.context.envelope import ContextEnvelopeInvariantValidator
from credit_harness.context.budget import snapshot_digest, fits
from .models import ModelInputBundle, PlannerDraft, PlannerProtocolError
from .aliases import ReferenceAliasProjector
from .prompt_contract import SYSTEM_CONTRACT


class ModelInputRenderer:
    def __init__(self):
        self.alias_map = MappingProxyType({})

    def render(self, snapshot: ReasoningContextSnapshot) -> ModelInputBundle:
        self.alias_map = MappingProxyType({})
        ContextEnvelopeInvariantValidator().validate(snapshot)
        if snapshot.snapshot_id != snapshot_digest(snapshot) or not fits(snapshot):
            raise PlannerProtocolError("invalid snapshot seal or context budget")
        sections = {trust: {} for trust in T}
        payload = snapshot.model_dump(mode="json")
        for field, trust in snapshot.section_trust.model_dump().items():
            sections[trust][field] = payload[field]
        sections[T.TRUSTED_CONTROL]["internal_order_id"] = snapshot.internal_order_id
        projection = ReferenceAliasProjector().project(snapshot, {
            "trusted_control": sections[T.TRUSTED_CONTROL],
            "deterministic_derived": sections[T.DETERMINISTIC_DERIVED],
            "untrusted_external_data": sections[T.UNTRUSTED_EXTERNAL_DATA],
        })
        bundle = ModelInputBundle(snapshot_id=snapshot.snapshot_id, system_contract=SYSTEM_CONTRACT,
                                  planner_output_schema=PlannerDraft.model_json_schema(), **projection.payload)
        self.alias_map = projection.alias_map
        return bundle
