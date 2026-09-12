"""Deterministic orchestration state; never a financial outcome assertion."""
from sqlalchemy import select, inspect
from sqlalchemy.orm import Session
from credit_harness.context.budget import digest
from credit_harness.authorization.models import SideEffectLedger
from credit_harness.authorization.tables import EffectRow
from credit_harness.recovery.repository import RECOVERABLE
from .models import OrchestrationProgressFingerprint


def progress_fingerprint(evidence, effects=(), requirements=()):
    evidence_hash = digest(sorted(e.model_dump_json() for e in evidence))
    # Exclude receipt timestamps, attempt counts, lease and Case lifecycle.
    effects_hash = digest(sorted((e.effect_id, e.action_type.value, e.status.value,
        e.target_hash, e.payload_hash, e.status in RECOVERABLE) for e in effects))
    requirements = tuple(sorted(set(requirements)))
    return OrchestrationProgressFingerprint(evidence_fingerprint=evidence_hash,
        effect_fingerprint=effects_hash, requirements=requirements,
        fingerprint=digest(dict(evidence=evidence_hash, effects=effects_hash, requirements=requirements)))


def capture_progress(cases, evidence, case_id, evaluator, report=None):
    # Independent read-only evaluation supplies the current requirement set,
    # including before the first recovery (no synthetic empty-set baseline).
    report = report or evaluator.evaluate(case_id)
    cases.get(case_id)
    with Session(cases.engine) as session:
        connection = session.connection()
        schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
        effects = tuple(SideEffectLedger.model_validate(r.payload) for r in session.scalars(
            select(EffectRow).where(EffectRow.case_id == case_id))) if inspect(connection).has_table(
                EffectRow.__tablename__, schema=schema) else ()
    return progress_fingerprint(evidence.list(case_id), effects,
        (r.requirement for r in report.unresolved_requirements))
