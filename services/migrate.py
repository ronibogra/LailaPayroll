"""Adds new columns to existing tables when the program is upgraded (data is kept)."""
import logging

from sqlalchemy import inspect, text

log = logging.getLogger("laila.migrate")


def _default_sql(col) -> str:
    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return ""
    v = d.arg
    if isinstance(v, bool):
        return f" DEFAULT {1 if v else 0}"
    if isinstance(v, (int, float)):
        return f" DEFAULT {v}"
    if isinstance(v, str):
        return " DEFAULT '" + v.replace("'", "''") + "'"
    return ""


def upgrade(engine, metadata) -> list[str]:
    added = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ctype = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD {col.name} {ctype}{_default_sql(col)}'))
                added.append(f"{table.name}.{col.name}")
    if added:
        log.info("Database upgraded, new columns: %s", ", ".join(added))
    return added
