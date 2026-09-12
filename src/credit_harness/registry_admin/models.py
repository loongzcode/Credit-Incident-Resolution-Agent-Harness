from enum import StrEnum
from typing import Annotated, Literal
from pydantic import AwareDatetime, Field, JsonValue
from credit_harness.domain.models import Model
from credit_harness.registry.models import (SystemDefinition, CapabilityDefinition, ClaimAuthorityRule, Hash, Ref)

Text = Annotated[str, Field(min_length=1, max_length=2000)]


class AdminError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class ChangeStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ACTIVATED = "ACTIVATED"
    CANCELED = "CANCELED"
    SUPERSEDED = "SUPERSEDED"


class ChangeKind(StrEnum):
    CONFIGURATION = "CONFIGURATION"
    ROLLBACK = "ROLLBACK"
    INVENTORY = "INVENTORY"


class EditableSystem(SystemDefinition):
    # Only a marker is accepted over HTTP. Existing server-side refs are preserved.
    credential_ref: Literal["[MASKED]"] | None = None


class EditableDefinition(Model):
    tenant_id: Ref
    systems: tuple[EditableSystem, ...] = Field(max_length=2000)
    capabilities: tuple[CapabilityDefinition, ...] = Field(max_length=5000)
    authority_rules: tuple[ClaimAuthorityRule, ...] = Field(max_length=20000)


class CreateDraft(Model):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Text
    base_registry_version: Hash | None


class EditDraft(Model):
    expected_revision: int = Field(ge=1)
    title: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Text
    definition: EditableDefinition


class Transition(Model):
    expected_revision: int = Field(ge=1)


class RollbackRequest(CreateDraft):
    target_version: Hash


class SourceMetadata(Model):
    source_type: Literal["EXCEL"] = "EXCEL"
    source_file_name: str = Field(max_length=200)
    source_file_hash: Hash
    source_row_ref: str = Field(max_length=200)
    imported_at: AwareDatetime
    imported_by: str = Field(max_length=200)


class CompanySystemInventory(Model):
    system_code: Ref
    system_name: Annotated[str, Field(min_length=1, max_length=300)]
    system_category: str = Field(max_length=300)
    description: str = Field(max_length=10000)
    main_functions: str = Field(max_length=10000)
    source_metadata: SourceMetadata


class ChangeRequest(Model):
    change_request_id: str
    tenant_id: Ref
    base_registry_version: Hash | None
    proposed_registry_version: Hash
    base_inventory_revision: int
    kind: ChangeKind
    revision: int
    status: ChangeStatus
    title: str
    reason: str
    created_by: str
    created_at: AwareDatetime
    submitted_by: str | None = None
    submitted_at: AwareDatetime | None = None
    approved_by: str | None = None
    approved_at: AwareDatetime | None = None
    rejected_by: str | None = None
    rejected_at: AwareDatetime | None = None
    activated_by: str | None = None
    activated_at: AwareDatetime | None = None


class DiffEntry(Model):
    entity_id: str
    fields: tuple[str, ...]
    before_values: dict[str, JsonValue] = Field(default_factory=dict)
    after_values: dict[str, JsonValue] = Field(default_factory=dict)


class VersionDiff(Model):
    systems_added: tuple[str, ...]
    systems_removed: tuple[str, ...]
    systems_changed: tuple[DiffEntry, ...]
    capabilities_added: tuple[str, ...]
    capabilities_removed: tuple[str, ...]
    capabilities_changed: tuple[DiffEntry, ...]
    authority_added: tuple[str, ...]
    authority_removed: tuple[str, ...]
    authority_changed: tuple[DiffEntry, ...]
    routing_scope_changed: tuple[str, ...]
    status_changed: tuple[str, ...]
    risk_level: Literal["HIGH_RISK", "STANDARD"]
    risk_reasons: tuple[str, ...]


class ImportConfirm(Model):
    preview_id: str
    title: Annotated[str, Field(min_length=1, max_length=200)]
    reason: Text
