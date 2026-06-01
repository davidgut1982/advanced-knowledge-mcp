"""Schema/migration tests for the pg_trgm content GIN index (Issue #26).

The lexical ``content ILIKE '%term%'`` search paths sequentially scan a
transcript-bloated ``knowledge.kb_entries`` because the existing to_tsvector FTS
indexes cannot serve substring matches. A trigram GIN index fixes that without
rewriting the query path.

These tests need NO live PostgreSQL — they enforce, by string comparison, that:

* ``migrations/010_kb_content_trgm_index.sql`` and the code constants in
  ``db_client`` describe the SAME extension + index (the migration's
  ``CONCURRENTLY`` keyword is the only intentional difference: the standalone
  migration builds against the live populated DB without a write lock, while the
  bootstrap path runs the instant non-concurrent form on a fresh empty table).
* the code constants are exactly the expected DDL strings.
* the PostgreSQL bootstrap (``LocalPostgresClient._init_schema``) actually
  executes both constants, so fresh databases get the index too.

This mirrors the parity guarantee in ``test_trust_score_migration.py`` for
migration 009 and the telemetry migrations.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from lore import db_client

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1] / "migrations" / "010_kb_content_trgm_index.sql"
)


def _normalize_sql(text: str) -> str:
    """Collapse whitespace and drop a trailing semicolon for byte-comparison."""
    return " ".join(text.split()).rstrip(";").strip()


def _migration_statements() -> list[str]:
    """Return the executable (non-comment) statements from migration 010."""
    text = MIGRATION_PATH.read_text()
    # Strip full-line SQL comments, then split on the statement terminator.
    sql_lines = [
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    ]
    body = "\n".join(sql_lines)
    return [_normalize_sql(s) for s in body.split(";") if s.strip()]


def test_migration_010_exists() -> None:
    """The standalone migration file must exist for operators to apply."""
    assert MIGRATION_PATH.is_file()


def test_trgm_extension_constant_is_exact() -> None:
    """The extension DDL constant is the exact idempotent CREATE EXTENSION."""
    assert db_client.KB_CONTENT_TRGM_EXTENSION_DDL == (
        "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
    )


def test_trgm_index_constant_is_exact() -> None:
    """The index DDL constant is the exact idempotent non-concurrent CREATE INDEX."""
    assert db_client.KB_CONTENT_TRGM_INDEX_DDL == (
        "CREATE INDEX IF NOT EXISTS idx_kb_entries_content_trgm "
        "ON knowledge.kb_entries USING gin (content gin_trgm_ops);"
    )


def test_migration_extension_matches_constant() -> None:
    """Migration's CREATE EXTENSION statement matches the code constant."""
    statements = _migration_statements()
    assert _normalize_sql(db_client.KB_CONTENT_TRGM_EXTENSION_DDL) in statements


def test_migration_index_matches_constant_modulo_concurrently() -> None:
    """Migration's CREATE INDEX matches the constant once CONCURRENTLY is removed.

    The migration uses ``CREATE INDEX CONCURRENTLY`` (safe against the live
    populated table); the bootstrap constant uses the plain ``CREATE INDEX``
    (instant on a fresh empty table, runs in autocommit). Apart from that single
    keyword the two must be identical so the resulting index is the same object.
    """
    statements = _migration_statements()
    constant = _normalize_sql(db_client.KB_CONTENT_TRGM_INDEX_DDL)

    # Find the migration's index statement and drop the CONCURRENTLY keyword.
    index_stmts = [s for s in statements if "idx_kb_entries_content_trgm" in s]
    assert len(index_stmts) == 1, f"expected one index statement, got {index_stmts}"
    migration_index = index_stmts[0]

    assert "CONCURRENTLY" in migration_index, (
        "live-DB migration must use CREATE INDEX CONCURRENTLY to avoid a "
        "write lock on the populated table"
    )
    assert _normalize_sql(migration_index.replace("CONCURRENTLY ", "")) == constant


def test_bootstrap_executes_both_trgm_statements() -> None:
    """PostgreSQL _init_schema must wire in both constants so fresh DBs get the index."""
    source = inspect.getsource(db_client.LocalPostgresClient._init_schema)
    assert "KB_CONTENT_TRGM_EXTENSION_DDL" in source
    assert "KB_CONTENT_TRGM_INDEX_DDL" in source
