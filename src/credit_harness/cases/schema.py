from credit_harness.persistence.store import Base
from credit_harness.persistence.migrations import add_dispatch_correlation_columns
from . import tables as case_tables  # noqa: F401 - metadata registration
from credit_harness.evidence import tables as evidence_tables  # noqa: F401


def create_harness_schema(engine):
    """Additive local bootstrap. Existing simulator tables must already exist."""
    from credit_harness.recovery.tables import ReadDispatchRecoveryRow, AgentCheckpointRow
    Base.metadata.create_all(engine, tables=[
        case_tables.CaseRow.__table__, case_tables.CaseCallRow.__table__,
        evidence_tables.EvidenceRow.__table__, evidence_tables.EvidenceOriginRow.__table__,
        ReadDispatchRecoveryRow.__table__,
        AgentCheckpointRow.__table__,
    ])
    from credit_harness.orchestration.tables import create_orchestration_schema
    create_orchestration_schema(engine)
    add_dispatch_correlation_columns(engine)
    from credit_harness.evaluation.tables import create_evaluation_schema
    create_evaluation_schema(engine)
    from credit_harness.registry.tables import create_registry_schema
    create_registry_schema(engine)
