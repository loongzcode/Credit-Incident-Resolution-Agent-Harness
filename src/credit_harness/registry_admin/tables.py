from sqlalchemy import JSON, String, Integer
from sqlalchemy.orm import Mapped, mapped_column
from credit_harness.persistence.store import Base


class ChangeRow(Base):
    __tablename__ = "registry_change_requests"
    change_request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    inventory: Mapped[list] = mapped_column(JSON, default=list)


class AdminAuditRow(Base):
    __tablename__ = "registry_admin_audit"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class InventoryHeadRow(Base):
    __tablename__ = "registry_inventory_heads"
    tenant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[list] = mapped_column(JSON, default=list)


class InventoryVersionRow(Base):
    __tablename__ = "registry_inventory_versions"
    tenant_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    change_request_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[list] = mapped_column(JSON)


class ImportPreviewRow(Base):
    __tablename__ = "registry_inventory_previews"
    preview_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(100), index=True)
    created_by: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict] = mapped_column(JSON)
    confirmed_cr: Mapped[str | None] = mapped_column(String(64), nullable=True)


def create_admin_schema(engine):
    Base.metadata.create_all(engine, tables=[ChangeRow.__table__, AdminAuditRow.__table__, InventoryHeadRow.__table__,
        InventoryVersionRow.__table__, ImportPreviewRow.__table__])
