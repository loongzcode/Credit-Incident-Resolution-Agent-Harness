from sqlalchemy import select, inspect
from sqlalchemy.orm import Session
from credit_harness.domain.models import Model
from credit_harness.authorization.tables import EffectRow
from credit_harness.authorization.models import EffectStatus as S
from .read import ReadObservationRecoveryService
from .models import RecoveryResult


class CaseRecoveryInspection(Model):
    case_id: str
    read_recoveries: tuple[RecoveryResult, ...]
    unresolved_effect_ids: tuple[str, ...]


class CaseRecoveryCoordinator:
    """Restart hook: recover reads, inspect writes; never dispatch either tool."""
    def __init__(self, cases, evidence):
        self.cases = cases
        self.read_recovery = ReadObservationRecoveryService(evidence)

    def before_investigation(self, case_id):
        self.cases.get(case_id)
        reads = tuple(self.read_recovery.recover(case_id, call_id)
                      for call_id in self.read_recovery.pending_calls(case_id))
        unresolved = ()
        # Read-only deployments do not need the authorization tables installed.
        with Session(self.cases.engine) as session:
            connection = session.connection()
            schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
            if inspect(connection).has_table(EffectRow.__tablename__, schema=schema):
                unresolved = tuple(session.scalars(select(EffectRow.effect_id).where(
                    EffectRow.case_id == case_id, EffectRow.status.in_([
                        S.PREPARED.value, S.DISPATCHED.value, S.ACCEPTED.value, S.UNKNOWN.value]))
                    .order_by(EffectRow.effect_id)))
        return CaseRecoveryInspection(case_id=case_id, read_recoveries=reads, unresolved_effect_ids=unresolved)
