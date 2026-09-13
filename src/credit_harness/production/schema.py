"""Explicit metadata registration for migration tooling; not application bootstrap."""
import importlib
from sqlalchemy import MetaData
from credit_harness.persistence.store import Base
from credit_harness.retrieval.tables import RetrievalBase

HEAD = "0017_1_metrics"


def metadata():
    for module in ("cases.tables", "evidence.tables", "authorization.tables", "recovery.tables",
        "evaluation.tables", "orchestration.tables", "memory.tables", "registry.tables",
        "registry_admin.tables", "adapters.synthetic_remediation", "investigation.trace_store",
        "investigation.cache", "production.tables"):
        importlib.import_module("credit_harness." + module)
    combined = MetaData()
    for source in (Base.metadata, RetrievalBase.metadata):
        for table in source.sorted_tables:
            table.to_metadata(combined)
    return combined


def runtime_managed(engine):
    return bool(engine.get_execution_options().get("production_runtime"))
