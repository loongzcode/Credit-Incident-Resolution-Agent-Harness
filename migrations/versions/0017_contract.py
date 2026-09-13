"""Activate a validated constraint after backfill; deployment-owned, never startup DDL."""
from alembic import op
import sqlalchemy as sa
revision = "0017_contract"
down_revision = "0017_backfill"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE investigation_safe_traces ADD CONSTRAINT safe_trace_version_present CHECK (projection_version IS NOT NULL) NOT VALID")
        op.execute("ALTER TABLE investigation_safe_traces VALIDATE CONSTRAINT safe_trace_version_present")
        op.alter_column("investigation_safe_traces", "projection_version", nullable=False, server_default='2')
        op.drop_constraint("safe_trace_version_present", "investigation_safe_traces")
    else:
        with op.batch_alter_table("investigation_safe_traces") as batch:
            batch.alter_column("projection_version", existing_type=sa.String(16), nullable=False, server_default='2')
    op.execute(sa.text("INSERT INTO schema_migration_audit VALUES ('0017_contract', CURRENT_TIMESTAMP, 'contract')"))


def downgrade():
    raise RuntimeError("Forward-only migration")
