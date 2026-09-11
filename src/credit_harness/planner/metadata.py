"""Provider metadata shared by investigation and remediation, without guidance dependencies."""
from typing import Annotated
from pydantic import Field
from credit_harness.domain.models import Model


class PlannerModelMetadata(Model):
    model_provider: str
    model_name: str
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
