"""Schema/migration tests for the combined English-config FTS GIN index.

The ``fts_search_postgres`` WHERE clause matches on::

    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,''))

The existing ``idx_kb_entries_content_search`` covers only
``to_tsvector('english', content)`` — a different expression — so Postgres
cannot use it and falls back to a full sequential scan (~4s per FTS query). This
migration adds the matching expression GIN index so the FTS leg of hybrid search
uses a Bitmap Index Scan.

These tests need NO live PostgreSQL — they enforce, by string comparison, that:

* ``migrations/011_kb_fts_english_combined_index.sql`` and the code constant in
  ``db_client`` describe the SAME index (the migration's ``CONCURRENTLY`` keyword
  is the only intentional difference: the standalone migration builds against the
  live populated DB without a write lock, while the bootstrap path runs the
  instant non-concurrent form on a fresh empty table).
* the migration runs OUTSIDE a transaction (no BEGIN/COMMIT), since CREATE INDEX
  CONCURRENTLY cannot run inside one.
* the bootstrap constant uses the exact combined expression matching the WHERE
  clause, and is non-concurrent for fresh-DB bootstrap.

This mirrors the parity guarantee in ``test_trgm_index_migration.py`` for
migration 010.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from lore import db_client

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "011_kb_fts_english_combined_index.sql"
)

INDEX_NAME = "idx_kb_entries_fts_english_combined"
COMBINED_EXPRESSION = "coalesce(title, '') || ' ' || coalesce(content, '')"


def _normalize_sql(text: str) -> str:
    """Collapse whitespace and drop a trailing semicolon for byte-comparison."""
    return " ".join(text.split()).rstrip(";").strip()


def _migration_statements() -> list[str]:
    """Return the executable (non-comment) statements from migration 011."""
    text = MIGRATION_PATH.read_text()
    # Strip full-line SQL comments, then split on the statement terminator.
    sql_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    ]
    body = "\n".join(sql_lines)
    return [_normalize_sql(s) for s in body.split(";") if s.strip()]


def test_migration_011_exists() -> None:
    """The standalone migration file must exist for operators to apply."""
    assert MIGRATION_PATH.is_file()


def test_migration_contains_index_name() -> None:
    """The migration SQL must create the expected combined-FTS index."""
    assert INDEX_NAME in MIGRATION_PATH.read_text()


def test_migration_uses_concurrently() -> None:
    """The live-DB migration must build CONCURRENTLY to avoid a write lock."""
    assert "CONCURRENTLY" in MIGRATION_PATH.read_text()


def test_migration_has_no_transaction_wrapper() -> None:
    """CREATE INDEX CONCURRENTLY cannot run inside a transaction block."""
    text = MIGRATION_PATH.read_text().upper()
    assert "BEGIN" not in text
    assert "COMMIT" not in text


def test_index_constant_contains_index_name() -> None:
    """The bootstrap index DDL constant references the expected index name."""
    assert INDEX_NAME in db_client.KB_FTS_ENGLISH_COMBINED_INDEX_DDL


def test_index_constant_uses_combined_expression() -> None:
    """The constant must match the fts_search_postgres WHERE expression exactly."""
    assert COMBINED_EXPRESSION in db_client.KB_FTS_ENGLISH_COMBINED_INDEX_DDL


def test_index_constant_is_non_concurrent() -> None:
    """The bootstrap constant must be non-concurrent (instant on fresh DBs)."""
    assert "CONCURRENTLY" not in db_client.KB_FTS_ENGLISH_COMBINED_INDEX_DDL


def test_migration_and_constant_share_index_name() -> None:
    """Migration SQL and the db_client constant define the same index object."""
    statements = _migration_statements()
    migration_index_stmts = [s for s in statements if INDEX_NAME in s]
    assert (
        len(migration_index_stmts) == 1
    ), f"expected one index statement, got {migration_index_stmts}"
    assert INDEX_NAME in db_client.KB_FTS_ENGLISH_COMBINED_INDEX_DDL


def test_bootstrap_executes_fts_index_statement() -> None:
    """PostgreSQL _init_schema must wire in the index constant so fresh DBs get the index."""
    source = inspect.getsource(db_client.LocalPostgresClient._init_schema)
    assert "KB_FTS_ENGLISH_COMBINED_INDEX_DDL" in source


def test_migration_index_statement_uses_combined_expression() -> None:
    """The migration SQL itself must use the same combined expression as the constant."""
    statements = _migration_statements()
    index_stmts = [s for s in statements if INDEX_NAME in s]
    assert len(index_stmts) == 1, f"expected one index statement, got {index_stmts}"
    assert COMBINED_EXPRESSION in index_stmts[0]


def test_ci_bootstrap_schema_contains_combined_index() -> None:
    """ci_bootstrap_schema.sql must include the combined FTS index so CI + fresh envs get it."""
    bootstrap_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "ci_bootstrap_schema.sql"
    )
    assert bootstrap_path.is_file()
    assert "idx_kb_entries_fts_english_combined" in bootstrap_path.read_text()
