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
            qualified = f"{quote(schema)}.{quote(table)}" if schema else quote(table)
            # Table and column names are a fixed allowlist, not client input.
            if "dispatch_correlation_id" not in columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE {qualified} ADD COLUMN dispatch_correlation_id VARCHAR(36) NULL",
                )
            index_name = f"ix_{table}_dispatch_correlation"
            if index_name not in {i["name"] for i in inspector.get_indexes(table, schema=schema)}:
                # Non-unique for legacy compatibility: ambiguous history must
                # fail closed in recovery, never be guessed or silently deleted.
                connection.exec_driver_sql(
                    f"CREATE INDEX {quote(index_name)} ON {qualified} (dispatch_correlation_id)",
                )
