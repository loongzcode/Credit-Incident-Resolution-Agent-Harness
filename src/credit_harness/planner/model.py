from typing import Protocol
from pydantic import ValidationError
from .models import ModelInputBundle, PlannerDraft, PlannerModelMetadata, PlannerProtocolError


class PlannerModel(Protocol):
    @property
    def metadata(self) -> PlannerModelMetadata: ...

    def plan(self, model_input: ModelInputBundle) -> PlannerDraft: ...


def parse_draft(value) -> PlannerDraft:
    try:
        if isinstance(value, PlannerDraft):
            value = value.model_dump()
        if isinstance(value, (str, bytes)):
            return PlannerDraft.model_validate_json(value)
        return PlannerDraft.model_validate(value)
    except (ValidationError, ValueError, TypeError):
        raise PlannerProtocolError("provider output violates PlannerDraft schema") from None


class FakePlannerModel:
    """Scripted draft or callback over ONLY the model-visible bundle; no networking."""
    def __init__(self, script):
        self.script = script

    @property
    def metadata(self):
        return PlannerModelMetadata(model_provider="fake", model_name="scripted-v1")

    def plan(self, model_input):
        result = self.script(model_input) if callable(self.script) else self.script
        return parse_draft(result)
