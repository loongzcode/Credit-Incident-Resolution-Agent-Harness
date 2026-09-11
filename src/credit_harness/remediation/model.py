from typing import Protocol
from pydantic import ValidationError
from credit_harness.planner.models import PlannerModelMetadata
from .models import RemediationInputBundle, RemediationDraft, RemediationProtocolError


class RemediationModel(Protocol):
    @property
    def metadata(self) -> PlannerModelMetadata: ...
    def plan(self, model_input: RemediationInputBundle) -> RemediationDraft: ...


def parse_draft(value):
    try:
        if isinstance(value, RemediationDraft):
            return RemediationDraft.model_validate(value.model_dump())
        return RemediationDraft.model_validate_json(value) if isinstance(value, str) else RemediationDraft.model_validate(value)
    except (ValidationError, TypeError, ValueError):
        raise RemediationProtocolError("invalid remediation structured proposal") from None


class FakeRemediationModel:
    metadata = PlannerModelMetadata(model_provider="fake", model_name="fake-remediation-v1")

    def __init__(self, script):
        self.script = script

    def plan(self, model_input):
        if type(model_input) is not RemediationInputBundle:
            raise TypeError("only remediation model input accepted")
        return parse_draft(self.script(model_input) if callable(self.script) else self.script)
