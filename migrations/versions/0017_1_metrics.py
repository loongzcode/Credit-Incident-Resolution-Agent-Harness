"""Add bounded operational aggregates. No audit payload rewrite or history scan."""
from alembic import op
import sqlalchemy as sa

revision = "0017_1_metrics"
down_revision = "0017_contract"
branch_labels = depends_on = None


def upgrade():
    op.create_table("retrieval_metrics",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("latency_count", sa.Integer, nullable=False),
        sa.Column("latency_sum_seconds", sa.Float, nullable=False))
    op.execute(sa.text("INSERT INTO schema_migration_audit VALUES ('0017_1_metrics', CURRENT_TIMESTAMP, 'aggregate')"))


def downgrade():
    raise RuntimeError("Forward-only migration")
