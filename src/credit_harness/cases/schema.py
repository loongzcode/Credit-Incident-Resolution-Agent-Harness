from credit_harness.persistence.store import Base
from . import tables as case_tables  # noqa: F401 - metadata registration
from credit_harness.evidence import tables as evidence_tables  # noqa: F401


def create_harness_schema(engine):
    """Additive local bootstrap. Existing simulator tables must already exist."""
    Base.metadata.create_all(engine, tables=[
        case_tables.CaseRow.__table__, case_tables.CaseCallRow.__table__,
        evidence_tables.EvidenceRow.__table__, evidence_tables.EvidenceOriginRow.__table__,
    ])
