import hashlib
import json
from enum import StrEnum
from typing import Annotated

from pydantic import AwareDatetime, Field, StrictBool, StrictInt, StrictStr, TypeAdapter, model_validator
from credit_harness.domain.identity import AccountRef, BeneficiaryRef, CustomerRef

from credit_harness.domain.enums import Completeness, Freshness, SourceKind, ToolName
from credit_harness.domain.models import Model
from credit_harness.tools.contracts import Observation, ToolQuery


class ClaimType(StrEnum):
    REQUEST_SENT = "REQUEST_SENT"
    HTTP_RESPONSE_STATUS = "HTTP_RESPONSE_STATUS"
    FUND_BUSINESS_STATUS = "FUND_BUSINESS_STATUS"
    LOAN_NO_PRESENT = "LOAN_NO_PRESENT"
    LOAN_NOTE_REFERENCE = "LOAN_NOTE_REFERENCE"
    PAYMENT_FINALITY = "PAYMENT_FINALITY"
    PAYMENT_TRANSACTION_ID = "PAYMENT_TRANSACTION_ID"
    PAYMENT_AMOUNT = "PAYMENT_AMOUNT"
    PAYMENT_CURRENCY = "PAYMENT_CURRENCY"
    PAYMENT_CUSTOMER_REF = "PAYMENT_CUSTOMER_REF"
    PAYMENT_BENEFICIARY_REF = "PAYMENT_BENEFICIARY_REF"
    PAYMENT_ACCOUNT_REF = "PAYMENT_ACCOUNT_REF"
    TRANSACTION_FUND_REQUEST_ID = "TRANSACTION_FUND_REQUEST_ID"
    CALLBACK_GATEWAY_RECEIVED = "CALLBACK_GATEWAY_RECEIVED"
    CALLBACK_SIGNATURE_VERIFIED = "CALLBACK_SIGNATURE_VERIFIED"
    CALLBACK_PROTOCOL_VERSION = "CALLBACK_PROTOCOL_VERSION"
    MESSAGE_CONSUME_STATUS = "MESSAGE_CONSUME_STATUS"
    MESSAGE_DLQ = "MESSAGE_DLQ"
    MESSAGE_ERROR_CODE = "MESSAGE_ERROR_CODE"
    MESSAGE_ERROR_FIELD = "MESSAGE_ERROR_FIELD"
    MESSAGE_EXPECTED_FIELD_TYPE = "MESSAGE_EXPECTED_FIELD_TYPE"
    MESSAGE_ACTUAL_FIELD_TYPE = "MESSAGE_ACTUAL_FIELD_TYPE"
    ACCOUNTING_ENTRY_PRESENT = "ACCOUNTING_ENTRY_PRESENT"
    ASSET_STATUS = "ASSET_STATUS"
    GUARANTEE_STATUS = "GUARANTEE_STATUS"
    GUARANTEE_VERSION = "GUARANTEE_VERSION"
    ASSET_DELIVERY_STATUS = "ASSET_DELIVERY_STATUS"
    PROTOCOL_FIELD_TYPE = "PROTOCOL_FIELD_TYPE"
    PROTOCOL_BUSINESS_SEMANTICS = "PROTOCOL_BUSINESS_SEMANTICS"
    SOURCE_LOOKUP_STATUS = "SOURCE_LOOKUP_STATUS"


class EvidenceStrength(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    STRONG = "STRONG"
    SUPPORTING = "SUPPORTING"
    WEAK = "WEAK"
    HINT = "HINT"


class SubjectKind(StrEnum):
    ORDER = "ORDER"
    FUND_REQUEST = "FUND_REQUEST"
    TRANSACTION = "TRANSACTION"
    CALLBACK = "CALLBACK"
    MESSAGE = "MESSAGE"
    PROTOCOL = "PROTOCOL"


class EvidenceSubject(Model):
    kind: SubjectKind
    identifier: str
    internal_order_id: str
    field: str | None = None


class EvidenceMetadata(Model):
    extractor_version: str = "1"
    source_path: str
    # Query is small, immutable and scoped. Raw response stays in ObservationRow.
    scope: ToolQuery
    # Visible DTO identity links; absence in legacy Evidence means no known link.
    fund_request_id: str | None = None
    callback_event_id: str | None = None


class Evidence(Model):
    evidence_id: str
    case_id: str
    observation_id: str
    tool: ToolName
    source_kind: SourceKind
    claim_type: ClaimType
    subject: EvidenceSubject
    value: StrictBool | StrictInt | StrictStr
    event_time: AwareDatetime | None
    observed_at: AwareDatetime
    source_as_of: AwareDatetime | None
    source_version: str | None
    protocol_version: str | None
    completeness: Completeness
    freshness: Freshness
    strength: EvidenceStrength
    raw_ref: str
    # Hash of canonical raw Observation JSON, using the existing persistence format.
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    created_at: AwareDatetime
    metadata: EvidenceMetadata

    @model_validator(mode="after")
    def validate_identity_reference(self):
        reference_type = {
            ClaimType.PAYMENT_CUSTOMER_REF: CustomerRef,
            ClaimType.PAYMENT_BENEFICIARY_REF: BeneficiaryRef,
            ClaimType.PAYMENT_ACCOUNT_REF: AccountRef,
        }.get(self.claim_type)
        if reference_type is not None:
            TypeAdapter(reference_type).validate_python(self.value)
        return self


class RawObservation(Model):
    observation_id: str
    tool: ToolName
    request: ToolQuery
    content_hash: str
    observation: Observation


class EvidenceConflict(Model):
    conflict_id: str
    case_id: str
    claim_type: ClaimType
    evidence_refs: tuple[str, ...]
    reason: str
    detected_at: AwareDatetime


def json_hash(payload) -> str:
    """Matches ObservationService's canonical JSON hash (including ASCII escaping)."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def fingerprint(evidence: Evidence) -> str:
    payload = evidence.model_dump(mode="json")
    for key in ("evidence_id", "observation_id", "raw_ref", "content_hash", "created_at", "observed_at"):
        payload.pop(key)
    # Absence and failed lookup claims describe the query instant, not a business event.
    if evidence.event_time is None:
        payload["lookup_at"] = evidence.observed_at.isoformat()
    return json_hash(payload)
