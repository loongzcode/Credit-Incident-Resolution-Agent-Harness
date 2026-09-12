from enum import StrEnum
from typing import Annotated
from pydantic import AwareDatetime, Field
from credit_harness.domain.models import Model
from credit_harness.context.models import Hash
from credit_harness.context.structured_values import OpaqueSubjectRef, StructuredVersion
from credit_harness.registry.models import BusinessDomain, Environment
from credit_harness.memory.models import GuidanceInvariant

PROJECTION_VERSION = "1"
NORMALIZATION_VERSION = "1"
EMBEDDING_CONTRACT_VERSION = "1"
MANDATORY_SAFETY = tuple(GuidanceInvariant)


class RetrievalError(RuntimeError):
    """Sanitized errors only; provider/source payloads are never logged."""


class DocumentType(StrEnum):
    SKILL = "SKILL"
    EXPERIENCE = "EXPERIENCE"


class SpaceStatus(StrEnum):
    BUILDING = "BUILDING"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EmbeddingSpace(Model):
    space_id: Hash
    provider: str = Field(min_length=1, max_length=100)
    model_id: str = Field(min_length=1, max_length=100)
    dimension: int = Field(ge=1, le=2000)
    projection_version: str = PROJECTION_VERSION
    normalization_version: str = NORMALIZATION_VERSION
    embedding_contract_version: str = EMBEDDING_CONTRACT_VERSION
    created_at: AwareDatetime
    status: SpaceStatus = SpaceStatus.BUILDING


class RetrievalScope(Model):
    # Infrastructure metadata. Never rendered into embedding text.
    tenant_id: OpaqueSubjectRef
    business_domain: BusinessDomain | None = None
    environment: Environment | None = None
    funding_partner: OpaqueSubjectRef | None = None
    asset_partner: OpaqueSubjectRef | None = None
    guarantee_partner: OpaqueSubjectRef | None = None
    product_code: OpaqueSubjectRef | None = None
    protocol_versions: tuple[StructuredVersion, ...] = ()
    applicable_since: AwareDatetime
    applicable_until: AwareDatetime | None = None


class EmbeddingIndexJob(Model):
    job_id: Hash
    tenant_id: OpaqueSubjectRef
    document_type: DocumentType
    document_id: str
    source_version: str
    content_hash: Hash
    embedding_space_id: Hash
    status: JobStatus
    attempt: int = Field(ge=0)
    lease_token: str | None = None
    lease_until: AwareDatetime | None = None
    next_eligible_at: AwareDatetime


class VectorCandidate(Model):
    document_type: DocumentType
    document_id: str
    source_version: str
    content_hash: Hash
    distance: float = Field(ge=0, le=2.00001, allow_inf_nan=False)
    specificity: int = Field(ge=0)


class RetrievalTelemetry(Model):
    embedding_space: Hash | None = None
    hard_filter_candidate_count: int = 0
    vector_candidate_count: int = 0
    reranked_count: int = 0
    selected_skill_ids: tuple[str, ...] = ()
    selected_experience_ids: tuple[Hash, ...] = ()
    degradation: str = "NONE"
    vector_latency_ms: float = 0
    rerank_latency_ms: float = 0
    latency_ms: float = 0
