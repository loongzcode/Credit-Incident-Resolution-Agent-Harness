from sqlalchemy import JSON, String, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class RegistryVersionRow(Base):
    __tablename__ = "registry_versions"
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class RegistryHeadRow(Base):
    __tablename__ = "registry_heads"
    tenant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SystemRow(Base):
    __tablename__ = "registry_systems"
    version: Mapped[str] = mapped_column(ForeignKey("registry_versions.version"), primary_key=True)
    system_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class CapabilityRow(Base):
    __tablename__ = "registry_capabilities"
    version: Mapped[str] = mapped_column(ForeignKey("registry_versions.version"), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class AuthorityRow(Base):
    __tablename__ = "registry_claim_authority_rules"
    version: Mapped[str] = mapped_column(ForeignKey("registry_versions.version"), primary_key=True)
    capability_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    claim_type: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)


class RegistryAuditRow(Base):
    __tablename__ = "registry_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(100))
    occurred_at: Mapped[str] = mapped_column(String(40))


class RouteContextRow(Base):
    __tablename__ = "registry_case_routes"
    case_id: Mapped[str] = mapped_column(ForeignKey("investigation_cases.case_id"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    actor: Mapped[str] = mapped_column(String(100))


class CapabilitySnapshotRow(Base):
    __tablename__ = "registry_case_snapshots"
    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(100))
    tenant_id: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)


class DispatchSourceRow(Base):
    __tablename__ = "registry_dispatch_sources"
    call_id: Mapped[str] = mapped_column(ForeignKey("case_tool_calls.call_id"), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(100))
    tenant_id: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)


def create_registry_schema(engine):
    Base.metadata.create_all(engine, tables=[RegistryVersionRow.__table__, RegistryHeadRow.__table__,
        SystemRow.__table__, CapabilityRow.__table__, AuthorityRow.__table__, RegistryAuditRow.__table__,
        RouteContextRow.__table__, CapabilitySnapshotRow.__table__, DispatchSourceRow.__table__])
