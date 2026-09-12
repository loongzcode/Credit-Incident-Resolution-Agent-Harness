from typing import Protocol
from credit_harness.domain.models import Model
from credit_harness.context.models import Hash
from pydantic import AwareDatetime
from .models import ValidatedActionProposal, RejectedCandidate, PlannerModelMetadata
from credit_harness.memory.models import SkillRef, GuidanceBuildStatus


class PlannerAuditRecord(Model):
    decision_id: Hash
    case_id: str
    snapshot_id: Hash
    planner_schema_version: str
    policy_version: str
    ranking_version: str
    model_input_schema_version: str
    model_provider: str
    model_name: str
    input_hash: Hash
    output_hash: Hash
    validated_at: AwareDatetime
    selected_action: ValidatedActionProposal | None
    rejection_summary: tuple[RejectedCandidate, ...]
    usage_metadata: PlannerModelMetadata
    guidance_fingerprint: Hash | None = None
    skill_refs: tuple[SkillRef, ...] = ()
    experience_refs: tuple[Hash, ...] = ()
    guidance_build_status: GuidanceBuildStatus = GuidanceBuildStatus.EMPTY


class PlannerAuditStore(Protocol):
    def append(self, record: PlannerAuditRecord) -> None: ...


class InMemoryPlannerAuditStore:
    """Append-only POC store; tenant/runtime binding owns an instance."""
    def __init__(self):
        self._records = []

    def append(self, record):
        self._records.append(PlannerAuditRecord.model_validate(record.model_dump()))

    @property
    def records(self):
        return tuple(self._records)
