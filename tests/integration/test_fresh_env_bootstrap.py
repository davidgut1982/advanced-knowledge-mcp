"""
Integration tests for fresh-environment bootstrap validation.

Verifies that a newly bootstrapped Lore database has the correct schema,
indexes, and search behaviour for the kb_get_batch and hybrid FTS features.

Requires a live PostgreSQL instance. Skipped automatically if TEST_POSTGRES_URL
or the DB_HOST / DB_NAME / DB_USER / DB_PASSWORD env vars are not set.

The ``server_module`` fixture (mirroring ``test_postgres_embeddings.py``)
triggers ``LocalPostgresClient._init_schema`` via the first lazy connection,
which is what installs the GIN / trigram indexes asserted here. The combined
FTS index (``idx_kb_entries_fts_english_combined``) is the regression target:
it must exist on fresh environments so the FTS leg of hybrid search uses a
Bitmap Index Scan instead of a sequential scan.
"""

from __future__ import annotations

import importlib
import os
import uuid
from collections.abc import Iterator
from types import ModuleType
from typing import cast

import pytest

pytestmark = [
    pytest.mark.slow,
    pytest.mark.integration,
]


def _pg_available() -> bool:
    """True iff a live PostgreSQL backend is configured and reachable-by-config."""
    backend = os.getenv("DB_BACKEND", "").strip().lower()
    if backend not in {"local", "postgres", "postgresql"}:
        return False
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return False
    # Either an explicit URL or the discrete connection vars must be present.
    if os.getenv("TEST_POSTGRES_URL"):
        return True
    return all(os.getenv(var) for var in ("DB_HOST", "DB_NAME", "DB_USER"))


pytestmark.append(
    pytest.mark.skipif(
        not _pg_available(),
        reason=(
            "PostgreSQL bootstrap test prerequisites missing: DB_BACKEND in "
            "{local,postgres,postgresql}, psycopg2 installed, and either "
            "TEST_POSTGRES_URL or DB_HOST/DB_NAME/DB_USER env vars set."
        ),
    )
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ``# type: ignore[misc]`` on the fixture decorators: the pre-commit mypy env
# has no pytest type stubs, so ``pytest.fixture`` is seen as an untyped
# decorator. The functions themselves are fully annotated.
@pytest.fixture(scope="module")  # type: ignore[misc]
def server_module() -> ModuleType:
    """Import the server module once and trigger ``_init_schema``.

    Reloading forces a fresh module so ``_init_schema`` runs against the
    current env; the lazy ``_get_connection`` call below is what actually
    creates the indexes asserted by these tests.
    """
    os.environ.setdefault("LORE_ENV", "staging")

    import lore.server as s

    importlib.reload(s)
    # P1-5 moved db initialisation out of module scope; wire up the global so
    # handlers have a live client before any request is dispatched.
    s.db = s.get_db_client()

    try:
        # First connection runs _init_schema, installing the GIN/trigram indexes.
        s.db._get_connection()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")

    return cast(ModuleType, s)


@pytest.fixture  # type: ignore[misc]
def test_topic() -> str:
    """Unique topic so test rows don't collide with real data."""
    return f"_test_bootstrap_{uuid.uuid4().hex[:8]}"


@pytest.fixture  # type: ignore[misc]
def cleanup_topic(server_module: ModuleType, test_topic: str) -> Iterator[str]:
    """Yield a topic, then delete every kb_entries row under it on teardown."""
    s = server_module
    yield test_topic
    try:
        conn = s.db._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "DELETE FROM knowledge.kb_entries WHERE topic = %s", (test_topic,)
            )
        finally:
            cursor.close()
    except Exception:  # noqa: BLE001
        pass


def _index_is_valid(server_module: ModuleType, index_name: str) -> bool:
    """Return True iff *index_name* exists in pg_indexes AND is marked valid.

    ``indisvalid`` is read from ``pg_index`` joined to ``pg_class`` by name so a
    half-built CONCURRENTLY index (which leaves an INVALID entry) is treated as
    missing — exactly the failure mode we want a fresh bootstrap to avoid.
    """
    conn = server_module.db._get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = %s)",
            (index_name,),
        )
        if not cursor.fetchone()[0]:
            return False
        cursor.execute(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s",
            (index_name,),
        )
        row = cursor.fetchone()
        return bool(row and row[0])
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# A. Index existence tests
# ---------------------------------------------------------------------------


def test_fts_english_combined_index_exists(server_module: ModuleType) -> None:
    """Regression: the combined English FTS GIN index must exist and be valid."""
    assert _index_is_valid(server_module, "idx_kb_entries_fts_english_combined")


def test_fts_simple_index_exists(server_module: ModuleType) -> None:
    """The simple-config FTS GIN index (Issue #10) must exist and be valid."""
    assert _index_is_valid(server_module, "idx_kb_search_simple")


def test_trgm_index_exists(server_module: ModuleType) -> None:
    """The pg_trgm content index (Issue #26) must exist and be valid."""
    assert _index_is_valid(server_module, "idx_kb_entries_content_trgm")


# ---------------------------------------------------------------------------
# B. Query plan test
# ---------------------------------------------------------------------------


def test_hybrid_fts_uses_gin_index(
    server_module: ModuleType, cleanup_topic: str
) -> None:
    """EXPLAIN on the FTS query must use a Bitmap Index Scan, not a Seq Scan.

    The SQL mirrors ``search.fts_search_postgres`` exactly (combined English
    expression + simple-config fallback) so the plan reflects the real hybrid
    FTS leg. ``random_page_cost = 1.1`` is set for the session so the planner
    treats storage as SSD and prefers the GIN index — the production default of
    4.0 (HDD) makes a small table favour a sequential scan and would mask the
    fix. We insert 20+ rows so the table is large enough for the planner to
    have a real choice.
    """
    s = server_module
    for i in range(25):
        resp = s.handle_kb_add(
            topic=cleanup_topic,
            title=f"Async topic {i}",
            content=(
                f"asyncio coroutines event loop entry {i} "
                "concurrency primitives gather task group"
            ),
        )
        assert resp["ok"] is True, resp

    conn = s.db._get_connection()
    cursor = conn.cursor()
    try:
        # SSD-appropriate planner cost so GIN indexes win over Seq Scan.
        cursor.execute("SET random_page_cost = 1.1")

        query = "asyncio coroutines"
        explain_sql = (
            "EXPLAIN "
            "SELECT kb_id, title "
            "FROM knowledge.kb_entries "
            "WHERE ("
            "    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(content,'')) "
            "    @@ websearch_to_tsquery('english', %s) "
            "    OR to_tsvector('simple', regexp_replace("
            "               coalesce(title,'') || ' ' || coalesce(content,''),"
            "               '[.,/\\\\:_-]', ' ', 'g')) "
            "       @@ plainto_tsquery('simple', regexp_replace(%s,"
            "               '[.,/\\\\:_-]', ' ', 'g'))"
            ")"
        )
        cursor.execute(explain_sql, [query, query])
        plan = "\n".join(row[0] for row in cursor.fetchall())
    finally:
        cursor.close()

    assert "Bitmap Index Scan" in plan, f"Expected GIN Bitmap Index Scan, got:\n{plan}"
    assert (
        "Seq Scan on kb_entries" not in plan
    ), f"Planner fell back to a sequential scan despite the GIN index:\n{plan}"


# ---------------------------------------------------------------------------
# C. kb_get_batch test
# ---------------------------------------------------------------------------


def test_kb_get_batch_returns_batch(
    server_module: ModuleType, cleanup_topic: str
) -> None:
    """kb_get_batch must return all requested entries with correct content.

    The handler returns ``entries`` as a list aligned to the input ``kb_ids``
    order (missing IDs become ``None`` at their position). We re-key it by
    ``kb_id`` here and assert every requested entry came back with its original
    title and content — verifying the batch fetch resolves by ID, not by
    accidental ordering.
    """
    s = server_module

    expected: dict[str, dict[str, str]] = {}
    for i in range(3):
        title = f"Batch entry {i}"
        content = f"batch content body {i} {uuid.uuid4().hex[:6]}"
        resp = s.handle_kb_add(topic=cleanup_topic, title=title, content=content)
        assert resp["ok"] is True, resp
        kb_id = resp["data"]["kb_id"]
        expected[kb_id] = {"title": title, "content": content}

    kb_ids = list(expected)
    batch = s.handle_kb_get_batch(kb_ids=kb_ids)
    assert batch["ok"] is True, batch

    data = batch["data"]
    assert data["found"] == 3
    assert data["missing"] == 0

    entries = data["entries"]
    assert len(entries) == len(kb_ids)
    # Re-key by kb_id (dict, not positional list) to assert resolution by ID.
    by_id = {row["kb_id"]: row for row in entries if row is not None}
    assert set(by_id) == set(kb_ids)
    for kb_id, want in expected.items():
        got = by_id[kb_id]
        assert got["title"] == want["title"]
        assert got["content"] == want["content"]
