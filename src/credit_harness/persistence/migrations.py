"""Single additive POC upgrade, not a migration framework.

Run via schema bootstrap with service writers stopped. Old rows retain NULL;
we cannot retroactively prove which dispatch produced a historical observation.
"""
from sqlalchemy import inspect


def add_dispatch_correlation_columns(engine) -> None:
    with engine.begin() as connection:
        schema = connection.get_execution_options().get("schema_translate_map", {}).get(None)
        inspector = inspect(connection)
        quote = connection.dialect.identifier_preparer.quote
        for table in ("tool_observations", "case_tool_calls"):
            if not inspector.has_table(table, schema=schema):
                continue
            columns = {column["name"] for column in inspector.get_columns(table, schema=schema)}
            if "dispatch_correlation_id" in columns:
                continue
            qualified = f"{quote(schema)}.{quote(table)}" if schema else quote(table)
            # Table and column names are a fixed allowlist, not client input.
            connection.exec_driver_sql(
                f"ALTER TABLE {qualified} ADD COLUMN dispatch_correlation_id VARCHAR(36) NULL",
            )
