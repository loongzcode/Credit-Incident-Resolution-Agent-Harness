from datetime import datetime, timezone
from typing import Protocol
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from credit_harness.context.budget import digest
from credit_harness.evidence.models import ClaimType as C
from credit_harness.hypotheses.models import PriorityClass as P
from .models import (SkillDefinition, SkillStatus as S, SkillGuidance, SkillRef, MemoryError,
    GuidanceInvariant, InvariantRule, SkillEvidenceStrategy, StrategyGoal as G, RationaleCode as R,
    SafetyLesson)
from .tables import SkillRow, GuidanceAuditRow


class SkillRepository(Protocol):
    def add(self, skill: SkillDefinition) -> None: ...
    def active(self) -> tuple[SkillDefinition, ...]: ...
    def activate(self, skill_id: str, version: str) -> None: ...
    def retire(self, skill_id: str, version: str) -> None: ...


def checked_skill(row):
    skill = SkillDefinition.model_validate(row.payload)
    if (row.content_hash != digest(skill.model_dump(mode="json")) or skill.skill_id != row.skill_id
            or skill.version != row.version or skill.status != S.DRAFT):
        raise MemoryError("skill integrity invalid")
    return skill.model_copy(update={"status": S(row.status)})


class SQLSkillRepository:
    """Trusted management API; body immutable, status transitions audited separately."""
    def __init__(self, engine, tenant_id, *, clock=lambda: datetime.now(timezone.utc)):
        self.engine, self.tenant_id, self.clock = engine, tenant_id, clock

    def add(self, skill):
        skill = SkillDefinition.model_validate(skill.model_dump())
        if skill.status != S.DRAFT:
            raise MemoryError("new skill must be DRAFT")
        with Session(self.engine) as session, session.begin():
            key = (self.tenant_id, skill.skill_id, skill.version)
            old = session.get(SkillRow, key)
            if old:
                checked_skill(old)
                if old.payload != skill.model_dump(mode="json"):
                    raise MemoryError("skill version is immutable")
                return
            session.add(SkillRow(tenant_id=self.tenant_id, skill_id=skill.skill_id, version=skill.version,
                status=S.DRAFT.value, content_hash=digest(skill.model_dump(mode="json")), payload=skill.model_dump(mode="json")))
            self._audit(session, skill.skill_id, skill.version, None, S.DRAFT)

    def _audit(self, session, skill_id, version, before, after):
        session.add(GuidanceAuditRow(tenant_id=self.tenant_id, kind="SKILL", object_id=skill_id,
            version=version, from_status=before, to_status=after.value, recorded_at=self.clock().isoformat()))

    def _transition(self, skill_id, version, before, after):
        with Session(self.engine) as session, session.begin():
            row = session.get(SkillRow, (self.tenant_id, skill_id, version))
            if row is None:
                raise MemoryError("skill unavailable")
            skill = checked_skill(row)
            if after == S.ACTIVE:
                SkillComposer().compose((skill.model_copy(update={"status": S.ACTIVE}),))
            if row.status == after.value:
                return
            result = session.execute(update(SkillRow).where(SkillRow.tenant_id == self.tenant_id,
                SkillRow.skill_id == skill_id, SkillRow.version == version, SkillRow.status == before.value)
                .values(status=after.value))
            if result.rowcount != 1:
                raise MemoryError("skill status transition denied")
            self._audit(session, skill_id, version, before.value, after)

    def activate(self, skill_id, version):
        self._transition(skill_id, version, S.DRAFT, S.ACTIVE)

    def retire(self, skill_id, version):
        self._transition(skill_id, version, S.ACTIVE, S.RETIRED)

    def active(self):
        with Session(self.engine) as session:
            return tuple(checked_skill(r) for r in session.scalars(select(SkillRow).where(
                SkillRow.tenant_id == self.tenant_id, SkillRow.status == S.ACTIVE.value)
                .order_by(SkillRow.skill_id, SkillRow.version)))


class SkillComposer:
    def compose(self, skills):
        result = []
        for skill in sorted(skills, key=lambda s: (s.skill_id, s.version)):
            skill = SkillDefinition.model_validate(skill.model_dump())
            if skill.status != S.ACTIVE:
                continue
            if any(not rule.enforced for rule in skill.safety_invariants):
                raise MemoryError("organizational safety conflict")
            # Union with baseline invariants: overlays can only add restrictions.
            rules = {rule.invariant: rule for rule in skill.safety_invariants}
            rules.update({i: InvariantRule(invariant=i) for i in GuidanceInvariant})
            result.append(SkillGuidance(skill=SkillRef(skill_id=skill.skill_id, version=skill.version),
                evidence_strategy=skill.evidence_strategy,
                safety_invariants=tuple(rules[k] for k in sorted(rules)), anti_patterns=skill.anti_patterns))
        return tuple(result)


def general_investigation_skill():
    return SkillDefinition(skill_id="tri-party-disbursement", version="1",
        investigation_goals=tuple(G), evidence_strategy=(
            SkillEvidenceStrategy(goal=G.ESTABLISH_PAYMENT_FINALITY,
                recommended_claim_types=(C.PAYMENT_FINALITY, C.PAYMENT_TRANSACTION_ID), priority=P.SAFETY_CRITICAL, rationale_code=R.MONEY_TRUTH_FIRST),
            SkillEvidenceStrategy(goal=G.ESTABLISH_PAYMENT_IDENTITY,
                recommended_claim_types=(C.PAYMENT_CUSTOMER_REF, C.PAYMENT_BENEFICIARY_REF, C.PAYMENT_ACCOUNT_REF),
                priority=P.SAFETY_CRITICAL, rationale_code=R.IDENTITY_BEFORE_EFFECT),
            SkillEvidenceStrategy(goal=G.INVESTIGATE_CALLBACK_CONSUMPTION,
                recommended_claim_types=(C.CALLBACK_GATEWAY_RECEIVED, C.MESSAGE_CONSUME_STATUS),
                priority=P.DISCRIMINATING, rationale_code=R.CURRENT_GAP_DRIVEN)),
        safety_invariants=tuple(InvariantRule(invariant=i) for i in GuidanceInvariant),
        anti_patterns=tuple(SafetyLesson), created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
