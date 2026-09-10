from typing import Annotated

from pydantic import AwareDatetime, Field, StrictInt, model_validator

from credit_harness.domain.enums import FaultKind, ToolName
from credit_harness.domain.models import Model


class ObservationFault(Model):
    """Server-only visibility policy, never included in an Observation."""

    tool: ToolName
    kind: FaultKind
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    lag_seconds: Annotated[StrictInt, Field(ge=0)] = 0
    cached_revision: Annotated[StrictInt, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def validate_window(self):
        if self.ends_at is not None and self.ends_at <= self.starts_at:
            raise ValueError("fault end must follow start")
        if self.kind == FaultKind.OLD_CACHE and self.cached_revision is None:
            raise ValueError("OLD_CACHE requires cached_revision")
        if self.kind == FaultKind.REPLICA_LAG and self.lag_seconds == 0:
            raise ValueError("REPLICA_LAG requires positive lag_seconds")
        return self

    def active(self, observed_at):
        return self.starts_at <= observed_at and (
            self.ends_at is None or observed_at < self.ends_at
        )

