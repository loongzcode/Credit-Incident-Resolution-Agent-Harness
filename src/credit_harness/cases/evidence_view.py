from credit_harness.domain.enums import Completeness, Freshness, KnowledgeStatus, ToolName
from credit_harness.domain.models import Model
from credit_harness.evidence.models import ClaimType, Evidence, EvidenceConflict, RawObservation
from credit_harness.evidence.repository import EvidenceRepository
from credit_harness.evidence.services import EvidenceConflictDetector
from .models import Case


class PaymentEvidenceAvailability(Model):
    """Presence of current observed claims, NOT a decision about financial finality."""
    knowledge: KnowledgeStatus
    evidence_refs: tuple[str, ...]
    reason: str


class CaseEvidenceView(Model):
    case: Case
    evidence_count: int
    latest_observations: tuple[RawObservation, ...]
    evidence_by_claim_type: dict[ClaimType, tuple[Evidence, ...]]
    conflicts: tuple[EvidenceConflict, ...]
    unknown_lookups: tuple[Evidence, ...]
    payment_finality_evidence: PaymentEvidenceAvailability


class CaseEvidenceService:
    def __init__(self, repository: EvidenceRepository):
        self.repository = repository

    def get_case_evidence(self, case_id: str) -> CaseEvidenceView:
        case = self.repository.cases.get(case_id)
        evidence = self.repository.list(case_id)
        # Queries for different protocol versions/effective times retain separate slots.
        latest = {}
        for raw in self.repository.observations(case_id):
            latest[(raw.tool, raw.request.model_dump_json())] = raw
        grouped = {}
        for item in evidence:
            grouped.setdefault(item.claim_type, []).append(item)
        latest_payment_ids = {
            raw.observation_id for raw in latest.values() if raw.tool == ToolName.PAYMENT
            and raw.observation.freshness == Freshness.CURRENT
            and raw.observation.completeness == Completeness.COMPLETE
            and raw.observation.knowledge == KnowledgeStatus.OBSERVED
        }
        current_payment = tuple(
            e.evidence_id for e in evidence if e.claim_type == ClaimType.PAYMENT_FINALITY
            and e.value != "UNKNOWN" and latest_payment_ids.intersection(
                self.repository.origin_observation_ids(case_id, e.evidence_id))
        )
        return CaseEvidenceView(
            case=case, evidence_count=len(evidence), latest_observations=tuple(latest.values()),
            evidence_by_claim_type={key: tuple(items) for key, items in grouped.items()},
            conflicts=EvidenceConflictDetector().detect(case_id, evidence),
            unknown_lookups=tuple(grouped.get(ClaimType.SOURCE_LOOKUP_STATUS, ())),
            payment_finality_evidence=PaymentEvidenceAvailability(
                knowledge=KnowledgeStatus.OBSERVED if current_payment else KnowledgeStatus.UNKNOWN,
                evidence_refs=current_payment,
                reason="存在支付工具的当前观测；不代表通过资金终态验证。" if current_payment else
                       "未获得当前明确的 PAYMENT_FINALITY Evidence；资金终态保持 UNKNOWN。",
            ),
        )
