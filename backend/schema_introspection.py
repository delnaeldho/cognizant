from sqlalchemy import inspect
from sqlalchemy.engine import Engine

INTERNAL_APP_TABLES = ["query_cache", "dynamic_query_cache", "cache_audit_log"]


def _reflect_all_tables(inspector, all_tables: list[str]) -> tuple[dict, dict, dict]:
    """Fetches columns/PKs/FKs for every table in as few round trips as possible.

    Prefers SQLAlchemy's batched `get_multi_*` reflection APIs (one query per
    metadata kind, regardless of table count) since per-table reflection
    calls each cost a full network round trip — on a high-latency connection
    (e.g. a remote serverless Postgres instance) that adds up to multiple
    seconds per table. Falls back to the old per-table calls for dialects
    that don't support the batched APIs.
    """
    try:
        multi_columns = inspector.get_multi_columns(schema=None)
        multi_pks = inspector.get_multi_pk_constraint(schema=None)
        multi_fks = inspector.get_multi_foreign_keys(schema=None)
        columns_by_table = {name: multi_columns.get((None, name), []) for name in all_tables}
        pks_by_table = {name: multi_pks.get((None, name), {}) for name in all_tables}
        fks_by_table = {name: multi_fks.get((None, name), []) for name in all_tables}
        return columns_by_table, pks_by_table, fks_by_table
    except NotImplementedError:
        pass

    columns_by_table, pks_by_table, fks_by_table = {}, {}, {}
    for table_name in all_tables:
        try:
            columns_by_table[table_name] = inspector.get_columns(table_name)
        except Exception:
            columns_by_table[table_name] = []
        try:
            pks_by_table[table_name] = inspector.get_pk_constraint(table_name)
        except Exception:
            pks_by_table[table_name] = {}
        try:
            fks_by_table[table_name] = inspector.get_foreign_keys(table_name)
        except Exception:
            fks_by_table[table_name] = []
    return columns_by_table, pks_by_table, fks_by_table


def get_database_schema(engine: Engine) -> dict:
    """Introspect the database and return table/column metadata, primary keys, and relationships,

    filtering out internal application tables and system tables.
    """
    inspector = inspect(engine)
    tables = {}
    relationships = []

    excluded_set = {t.lower() for t in INTERNAL_APP_TABLES}

    # Get all table names, excluding internal sqlite system tables and app cache tables
    all_tables = [
        t for t in inspector.get_table_names()
        if not t.startswith("sqlite_") and t.lower() not in excluded_set
    ]

    columns_by_table, pks_by_table, fks_by_table = _reflect_all_tables(inspector, all_tables)

    # Build relationships list from foreign keys
    for table_name in all_tables:
        for fk in fks_by_table.get(table_name, []) or []:
            referred_table = fk.get("referred_table")
            constrained_cols = fk.get("constrained_columns") or []
            referred_cols = fk.get("referred_columns") or []

            # Ignore foreign keys that point to excluded internal tables
            if (
                referred_table
                and referred_table.lower() not in excluded_set
                and not referred_table.startswith("sqlite_")
            ):
                for s_col, t_col in zip(constrained_cols, referred_cols):
                    if s_col and t_col:
                        relationships.append({
                            "id": f"{table_name}.{s_col}->{referred_table}.{t_col}",
                            "source_table": table_name,
                            "source_column": s_col,
                            "target_table": referred_table,
                            "target_column": t_col,
                            "constraint_name": fk.get("name"),
                        })

    # Extract column definitions and primary keys for each table
    for table_name in all_tables:
        columns = columns_by_table.get(table_name) or []
        pk_constraint = pks_by_table.get(table_name) or {}
        pk_columns = set(pk_constraint.get("constrained_columns") or [])

        # Set of foreign key column names in this table
        fk_columns = set()
        for fk in fks_by_table.get(table_name, []) or []:
            referred_table = fk.get("referred_table")
            if (
                referred_table
                and referred_table.lower() not in excluded_set
                and not referred_table.startswith("sqlite_")
            ):
                for col in fk.get("constrained_columns") or []:
                    fk_columns.add(col)

        table_columns = []
        for col in columns:
            col_name = col["name"]
            is_pk = (col_name in pk_columns) or bool(col.get("primary_key", False))
            is_fk = col_name in fk_columns

            table_columns.append({
                "name": col_name,
                "type": str(col["type"]),
                "primary_key": is_pk,
                "is_foreign_key": is_fk,
                "nullable": bool(col.get("nullable", True)),
            })

        tables[table_name] = table_columns

    return {
        "tables": tables,
        "relationships": relationships,
    }


def format_schema_for_context(schema_data: dict) -> str:
    """Format schema as readable text for AI context, including PKs and FK relationships."""
    if not isinstance(schema_data, dict):
        return ""

    tables = schema_data.get("tables", schema_data) if "tables" in schema_data else schema_data
    relationships = schema_data.get("relationships", []) if "relationships" in schema_data else []

    schema_text = ""
    for table_name, columns in tables.items():
        schema_text += f"\nTable: {table_name}\n"
        for col in columns:
            pk_tag = " [PRIMARY KEY]" if col.get("primary_key") else ""
            fk_tag = " [FOREIGN KEY]" if col.get("is_foreign_key") else ""
            schema_text += f"  - {col['name']} ({col['type']}){pk_tag}{fk_tag}\n"

    if relationships:
        schema_text += "\nExplicit Relationships (Foreign Keys):\n"
        for rel in relationships:
            schema_text += (
                f"  - {rel['source_table']}.{rel['source_column']} -> "
                f"{rel['target_table']}.{rel['target_column']}\n"
            )

    return schema_text
