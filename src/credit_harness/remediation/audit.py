from typing import Protocol
from credit_harness.domain.models import Model
from credit_harness.context.models import Hash
from credit_harness.planner.metadata import PlannerModelMetadata
from .models import (RemediationCandidate, RejectedRemediationCandidate, RemediationPreflightResult,
                     REMEDIATION_POLICY_VERSION, REMEDIATION_CATALOG_VERSION)


class RemediationAuditRecord(Model):
    decision_id: Hash
    case_id: str
    snapshot_id: Hash
    input_hash: Hash | None
    draft_hash: Hash | None
    selected_candidate: RemediationCandidate | None
    rejection_reasons: tuple[RejectedRemediationCandidate, ...]
    preflight_results: tuple[RemediationPreflightResult, ...]
    intent_id: Hash | None
    model_metadata: PlannerModelMetadata | None
    policy_version: str = REMEDIATION_POLICY_VERSION
    catalog_version: str = REMEDIATION_CATALOG_VERSION


class RemediationAuditStore(Protocol):
    def append(self, record: RemediationAuditRecord) -> None: ...


class InMemoryRemediationAuditStore:
    def __init__(self):
        self._records = []

    def append(self, record):
        self._records.append(record)

    @property
    def records(self):
        return tuple(self._records)
