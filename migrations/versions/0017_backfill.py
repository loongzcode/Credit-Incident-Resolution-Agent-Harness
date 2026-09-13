"""Restartable backfill and database-owned source/index generations."""
from alembic import op
import sqlalchemy as sa

revision = "0017_backfill"
down_revision = "0017_expand"
branch_labels = depends_on = None


def upgrade():
    op.execute("UPDATE investigation_safe_traces SET projection_version = '1' WHERE projection_version IS NULL")
    # Historical audit payloads remain byte-for-byte untouched.
    # Changes committed by ANY writer invalidate completeness, including source
    # revocation and index deletion. Reconcile, never retrieval, scans documents.
    for table, kind in (("organizational_skills", "SKILL"), ("verified_incident_experiences", "EXPERIENCE"),
                        ("skill_vector_index", "SKILL"), ("experience_vector_index", "EXPERIENCE")):
        if op.get_bind().dialect.name == "postgresql":
            op.execute(f"""CREATE FUNCTION bump_{table}_generation() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  INSERT INTO retrieval_source_generations(tenant_id,document_type,generation)
                    VALUES(COALESCE(NEW.tenant_id, OLD.tenant_id),'{kind}',1)
                    ON CONFLICT(tenant_id,document_type) DO UPDATE
                    SET generation=retrieval_source_generations.generation+1;
                  RETURN NULL;
                END $$""")
            op.execute(f"CREATE TRIGGER generation_{table} AFTER INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION bump_{table}_generation()")
        else:
            for event in ("INSERT", "UPDATE", "DELETE"):
                row = "OLD" if event == "DELETE" else "NEW"
                op.execute(f"""CREATE TRIGGER generation_{table}_{event.lower()} AFTER {event} ON {table}
                  BEGIN INSERT INTO retrieval_source_generations(tenant_id,document_type,generation)
                    VALUES({row}.tenant_id,'{kind}',1) ON CONFLICT(tenant_id,document_type)
                    DO UPDATE SET generation=generation+1; END""")
    op.execute(sa.text("INSERT INTO schema_migration_audit VALUES ('0017_backfill', CURRENT_TIMESTAMP, 'migrate')"))


def downgrade():
    raise RuntimeError("Forward-only migration")
