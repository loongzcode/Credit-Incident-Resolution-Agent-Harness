import os
from alembic import context
from sqlalchemy import create_engine, pool
from credit_harness.production.schema import metadata
config = context.config

def run(connection):
    schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
    if schema and connection.dialect.name == "postgresql":
        connection.exec_driver_sql('SET search_path TO ' + connection.dialect.identifier_preparer.quote(schema) + ', public')
    context.configure(connection=connection, target_metadata=metadata(), compare_type=True,
        version_table_schema=schema, render_as_batch=connection.dialect.name == "sqlite")
    with context.begin_transaction():
        context.run_migrations()

provided = config.attributes.get("connection")
if provided is not None:
    run(provided)
else:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required")
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql("SET lock_timeout = '5s'")
            connection.exec_driver_sql("SET statement_timeout = '120s'")
            connection.commit()
        run(connection)
