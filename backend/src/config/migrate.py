"""Idempotent in-place upgrade for databases created before packages existed.

``Base.metadata.create_all`` only creates missing tables; it never adds a
column to an existing one.  A deployment that already holds frozen runs and
approved downloads therefore needs a small additive upgrade before the new
code can read its tables.  Every statement here is additive and safe to run
on every startup: no existing column, row or value is rewritten.

After the columns exist, :func:`backfill_origin_uids` gives legacy windows
their reproducible source identity.
"""
import logging

from sqlalchemy import inspect, text

from .database import engine

# (table, column, DDL type + default).  Only additive, nullable/defaulted
# columns belong here so an old database can be upgraded without downtime.
_COLUMNS = [
    ("archive_studies", "merge_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("archive_studies", "baseline_hash", "VARCHAR NOT NULL DEFAULT ''"),
    ("climate_windows", "origin_uid", "VARCHAR"),
    ("climate_windows", "revision", "INTEGER NOT NULL DEFAULT 0"),
    ("climate_windows", "last_branch", "VARCHAR NOT NULL DEFAULT 'main'"),
    ("climate_windows", "last_package_uid", "VARCHAR NOT NULL DEFAULT ''"),
]


def upgrade_columns():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = []
    with engine.begin() as conn:
        for table, column, ddl in _COLUMNS:
            if table not in existing_tables:
                continue
            columns = {c["name"] for c in inspector.get_columns(table)}
            if column in columns:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            added.append(f"{table}.{column}")
    if added:
        logging.info("schema upgrade added: %s", ", ".join(added))
    return added


def run_upgrade():
    """Best-effort migration; failure must not stop the API from serving."""
    try:
        return upgrade_columns()
    except Exception:
        logging.exception("schema upgrade failed; run migrations/002_merge.sql manually")
        return []
