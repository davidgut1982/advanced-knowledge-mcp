"""Unit tests for server handler bug fixes found during QA (BUG-1/2/3/7/8).

Pure unit tests — no live database. A small fluent fake stands in for the
db_client query builder so the KB CRUD handlers can be exercised in isolation.
The PostgreSQL round-trip coverage lives in tests/integration/.
"""

from __future__ import annotations

import psycopg2
import pytest

import lore.search as srch
import lore.server as srv
from lore.db_client import QueryResult

# ---------------------------------------------------------------------------
# Fluent fake db: db.table(...).select(...).eq(...).maybe_single().execute()
# and .update(...).eq(...).execute() / .delete().eq(...).execute().
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, db: _FakeDb):
        self._db = db
        self._is_update = False

    def select(self, *_a, **_k):
        return self

    def update(self, data):
        self._is_update = True
        self._db.updates.append(data)
        return self

    def delete(self):
        self._db.deleted = True
        return self

    def eq(self, *_a, **_k):
        return self

    def maybe_single(self):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self._is_update:
            if self._db.update_raises is not None:
                raise self._db.update_raises
            return QueryResult(data=self._db.updated_row or {})
        # A read: return the (current) row for this entry.
        return QueryResult(data=self._db.current_row)


class _FakeDb:
    """Configurable fake supporting the KB CRUD handler call chains.

    ``current_row`` is returned by every read (select...maybe_single...execute).
    After an update the handler re-reads, so ``updated_row`` (if set) is returned
    on subsequent reads. ``update_raises`` lets a test simulate a SQL error.
    """

    def __init__(self, current_row=None, updated_row=None, update_raises=None):
        self.current_row = current_row if current_row is not None else {}
        self.updated_row = updated_row
        self.update_raises = update_raises
        self.updates: list[dict] = []
        self.deleted = False

    def table(self, _name):
        # Once an update has been applied, point reads at the updated row.
        if self.updated_row is not None and self.updates:
            self.current_row = self.updated_row
        return _FakeQuery(self)


@pytest.fixture(autouse=True)
def _no_semantic(monkeypatch):
    """Disable the embed-on-write path so handlers don't touch embeddings."""
    monkeypatch.setattr(srv, "_semantic_write_enabled", lambda: False)


# ---------------------------------------------------------------------------
# BUG-1: kb_update with an unsupported column -> clean invalid_input, not raise
# ---------------------------------------------------------------------------


def test_kb_update_metadata_returns_clean_error(monkeypatch):
    """Passing metadata (no such column) must surface a clean invalid_input
    error rather than leaking the raw psycopg2 ProgrammingError."""
    existing = {"kb_id": "kb_1", "title": "T", "content": "c"}
    # The update execute() raises UndefinedColumn (a psycopg2.ProgrammingError
    # subclass) — mirroring what Postgres does for an unknown column.
    err = psycopg2.errors.UndefinedColumn('column "metadata" does not exist')
    fake = _FakeDb(current_row=existing, update_raises=err)
    monkeypatch.setattr(srv, "db", fake)

    # metadata is no longer a declared param; simulate the value reaching SQL
    # by updating a (pretend-unknown) field via the supported `content` path.
    resp = srv.handle_kb_update(kb_id="kb_1", content="new content")
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert resp["message"] == "Field not supported"


def test_kb_update_metadata_param_removed_from_schema():
    """BUG-1 Fix A: the kb_update inputSchema must not expose `metadata`."""
    schema = srv._TOOL_SCHEMA_MAP["kb_update"]
    assert "metadata" not in schema["properties"]


def test_kb_update_metadata_rejected_at_call_tool_boundary():
    """End-to-end BUG-1: a kb_update call carrying `metadata` is rejected as a
    clean invalid_input at the handler level (**kwargs check), never raising an
    exception that leaks as unexpected_exception.

    Note: additionalProperties:False was removed from the schema because FastMCP
    intercepts schema rejections before call_tool executes, producing a raw
    -32603 transport error instead of our clean envelope. The handler's **kwargs
    check catches unsupported fields and returns the clean invalid_input envelope.
    """
    import asyncio
    import json

    out = asyncio.run(srv.call_tool("kb_update", {"kb_id": "kb_1", "metadata": {"foo": "bar"}}))
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    # It is a validation error, NOT an unexpected_exception.
    assert payload["error"] != "unexpected_exception"


# ---------------------------------------------------------------------------
# kb_list: unknown kwargs (hallucinated filters like created_at__gte) must
# surface a clean invalid_input envelope, never a raised TypeError. A raised
# exception leaks as a tool execution error and trips the caller's circuit
# breaker, marking the whole MCP server unreachable.
# ---------------------------------------------------------------------------


def test_kb_list_unknown_kwarg_does_not_raise():
    """An unexpected keyword argument must return a clean envelope, not raise.

    The handler's **kwargs guard runs before any DB access, so no db mock is
    needed: the unsupported field is rejected up front.
    """
    resp = srv.handle_kb_list(created_at__gte="2026-05-26T00:00:00Z")
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "created_at__gte" in resp["message"]


def test_kb_list_unknown_kwarg_rejected_at_call_tool_boundary():
    """End-to-end: a kb_list call carrying a hallucinated filter is rejected as
    a clean invalid_input at the handler level (**kwargs check), never raising
    an exception that leaks as unexpected_exception and trips the circuit
    breaker.
    """
    import asyncio
    import json

    out = asyncio.run(srv.call_tool("kb_list", {"created_at__gte": "2026-05-26T00:00:00Z"}))
    payload = json.loads(out[0].text)
    assert payload["ok"] is False
    assert payload["error"] == "invalid_input"
    # It is a validation error, NOT an unexpected_exception.
    assert payload["error"] != "unexpected_exception"


# ---------------------------------------------------------------------------
# BUG-3: kb_update / kb_delete accept kb_id (not just entry_id) + title update
# ---------------------------------------------------------------------------


def test_kb_update_accepts_kb_id_field(monkeypatch):
    existing = {"kb_id": "kb_42", "title": "Old", "content": "c"}
    updated = {"kb_id": "kb_42", "title": "Old", "content": "new"}
    fake = _FakeDb(current_row=existing, updated_row=updated)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_update(kb_id="kb_42", content="new")
    assert resp["ok"] is True
    assert resp["data"]["kb_id"] == "kb_42"
    assert "content" in resp["data"]["updated_fields"]


def test_kb_update_entry_id_fallback_still_works(monkeypatch):
    """Backward compat: entry_id is accepted when kb_id is absent."""
    existing = {"kb_id": "kb_7", "title": "T", "content": "c"}
    updated = {"kb_id": "kb_7", "title": "T", "content": "z"}
    fake = _FakeDb(current_row=existing, updated_row=updated)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_update(entry_id="kb_7", content="z")
    assert resp["ok"] is True
    assert resp["data"]["kb_id"] == "kb_7"


def test_kb_update_missing_id_returns_invalid_input(monkeypatch):
    monkeypatch.setattr(srv, "db", _FakeDb())
    resp = srv.handle_kb_update(content="x")
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"


def test_kb_update_title_is_updatable(monkeypatch):
    existing = {"kb_id": "kb_9", "title": "Old Title", "content": "c"}
    updated = {"kb_id": "kb_9", "title": "New Title", "content": "c"}
    fake = _FakeDb(current_row=existing, updated_row=updated)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_update(kb_id="kb_9", title="New Title")
    assert resp["ok"] is True
    assert "title" in resp["data"]["updated_fields"]
    # The title field was actually written to the update payload.
    assert fake.updates[0]["title"] == "New Title"


def test_kb_update_title_in_schema():
    schema = srv._TOOL_SCHEMA_MAP["kb_update"]
    assert "title" in schema["properties"]
    assert schema["required"] == ["kb_id"]


def test_kb_delete_accepts_kb_id_field(monkeypatch):
    existing = {"kb_id": "kb_del", "title": "Doomed"}
    fake = _FakeDb(current_row=existing)
    monkeypatch.setattr(srv, "db", fake)
    monkeypatch.setattr(srv, "_delete_kb_embedding", lambda _id: None)

    resp = srv.handle_kb_delete(kb_id="kb_del", confirm=True)
    assert resp["ok"] is True
    assert resp["data"]["kb_id"] == "kb_del"
    assert fake.deleted is True


def test_kb_delete_schema_uses_kb_id():
    schema = srv._TOOL_SCHEMA_MAP["kb_delete"]
    assert "kb_id" in schema["properties"]
    assert "entry_id" not in schema["properties"]
    assert schema["required"] == ["kb_id"]


# ---------------------------------------------------------------------------
# BUG-2: deduplicate_results without a content field keys on kb_id
# ---------------------------------------------------------------------------


def test_deduplicate_results_without_content_uses_kb_id():
    """Search results (no `content`, only kb_id/title/topic/score) must NOT all
    collapse to one. Distinct kb_ids are kept; a repeated kb_id is removed."""
    results = [
        {"kb_id": "kb_a", "title": "Alpha", "topic": "t", "score": 0.9},
        {"kb_id": "kb_b", "title": "Beta", "topic": "t", "score": 0.8},
        {"kb_id": "kb_a", "title": "Alpha", "topic": "t", "score": 0.7},  # dup id
    ]
    resp = srv.handle_deduplicate_results(results)
    assert resp["ok"] is True
    kept = resp["data"]["results"]
    kept_ids = [r["kb_id"] for r in kept]
    assert kept_ids == ["kb_a", "kb_b"]  # one kb_a removed, kb_b kept
    assert resp["data"]["removed_count"] == 1
    assert resp["data"]["unique_count"] == 2


def test_deduplicate_results_distinct_ids_all_kept():
    """The primary regression: distinct kb_ids must never be de-duped together."""
    results = [{"kb_id": f"kb_{i}", "title": "x", "topic": "t"} for i in range(5)]
    resp = srv.handle_deduplicate_results(results)
    assert resp["data"]["unique_count"] == 5
    assert resp["data"]["removed_count"] == 0


def test_deduplicate_results_still_dedupes_on_content():
    """Items WITH content are de-duped on normalized text as before."""
    results = [
        {"content": "Same Text"},
        {"content": "same text"},  # case/space-insensitive dup
        {"content": "different"},
    ]
    resp = srv.handle_deduplicate_results(results)
    assert resp["data"]["unique_count"] == 2
    assert resp["data"]["removed_count"] == 1


# ---------------------------------------------------------------------------
# BUG-7: log_retrieval_feedback rejects out-of-range user_feedback_score
# ---------------------------------------------------------------------------


def _enable_mining(monkeypatch):
    monkeypatch.setenv("LORE_HARD_NEGATIVE_MINING", "true")
    monkeypatch.setenv("DB_BACKEND", "local")


@pytest.mark.parametrize("bad_score", [999, 0, -1, 6])
def test_log_feedback_score_out_of_range(monkeypatch, bad_score):
    _enable_mining(monkeypatch)
    # update_retrieval_feedback must never be reached for a bad score.
    monkeypatch.setattr(
        srv.telemetry,
        "update_retrieval_feedback",
        lambda **_k: pytest.fail("DB update should not run for out-of-range score"),
    )
    resp = srv.handle_log_retrieval_feedback("qry_x", user_feedback_score=bad_score)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "between 1 and 5" in resp["message"]


@pytest.mark.parametrize("good_score", [1, 3, 5])
def test_log_feedback_score_in_range_succeeds(monkeypatch, good_score):
    _enable_mining(monkeypatch)
    monkeypatch.setattr(srv.telemetry, "update_retrieval_feedback", lambda **_k: 1)
    resp = srv.handle_log_retrieval_feedback("qry_ok", user_feedback_score=good_score)
    assert resp["ok"] is True
    assert resp["data"]["updated"] == 1


def test_log_feedback_score_schema_bounds():
    schema = srv._TOOL_SCHEMA_MAP["log_retrieval_feedback"]
    score = schema["properties"]["user_feedback_score"]
    assert score["minimum"] == 1
    assert score["maximum"] == 5


# ---------------------------------------------------------------------------
# BUG-8: kb_search with top_k=0 returns invalid_input (not 1 result)
# ---------------------------------------------------------------------------


def test_kb_search_top_k_zero_returns_error(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "")  # legacy lexical path
    resp = srv.handle_kb_search("anything", top_k=0)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "at least 1" in resp["message"]


def test_kb_search_top_k_schema_minimum():
    schema = srv._TOOL_SCHEMA_MAP["kb_search"]
    assert schema["properties"]["top_k"]["minimum"] == 1


# ---------------------------------------------------------------------------
# BUG-4: kb_list pagination (limit / offset / total_count / has_more)
# ---------------------------------------------------------------------------


class _FakeListQuery:
    """Records the limit/offset applied to a kb_list query.

    Mirrors the fluent chain handle_kb_list uses:
    db.table(...).select(..., count="exact").order(...).eq(...)?.limit(n).offset(m).execute()
    The configured rows are returned as data and total_count as QueryResult.count.
    """

    def __init__(self, db: _FakeListDb):
        self._db = db

    def select(self, *_a, **_k):
        return self

    def order(self, column, **kwargs):
        self._db.applied_order = (column, kwargs)
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, count):
        self._db.applied_limit = count
        return self

    def offset(self, count):
        self._db.applied_offset = count
        return self

    def execute(self):
        return QueryResult(data=list(self._db.rows), count=self._db.total_count)


class _FakeListDb:
    def __init__(self, rows=None, total_count=0):
        self.rows = rows if rows is not None else []
        self.total_count = total_count
        self.applied_limit = None
        self.applied_offset = None
        self.applied_order = None

    def table(self, _name):
        return _FakeListQuery(self)


def test_kb_list_default_pagination(monkeypatch):
    """No params: limit defaults to 100, offset to 0, has_more present."""
    rows = [{"kb_id": f"kb_{i}"} for i in range(3)]
    fake = _FakeListDb(rows=rows, total_count=3)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_list()
    assert resp["ok"] is True
    assert resp["data"]["limit"] == 100
    assert resp["data"]["offset"] == 0
    assert "has_more" in resp["data"]
    assert resp["data"]["has_more"] is False
    assert resp["data"]["total_count"] == 3
    assert fake.applied_limit == 100
    assert fake.applied_offset == 0
    assert fake.applied_order == ("created_at", {"desc": True})


def test_kb_list_with_limit_and_offset(monkeypatch):
    """limit=5, offset=10 are passed verbatim into the query (LIMIT/OFFSET)."""
    rows = [{"kb_id": f"kb_{i}"} for i in range(5)]
    fake = _FakeListDb(rows=rows, total_count=42)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_list(limit=5, offset=10)
    assert resp["ok"] is True
    assert fake.applied_limit == 5
    assert fake.applied_offset == 10
    assert resp["data"]["limit"] == 5
    assert resp["data"]["offset"] == 10
    assert resp["data"]["total_count"] == 42
    # offset(10) + 5 returned < 42 total -> there are more pages.
    assert resp["data"]["has_more"] is True


def test_kb_list_limit_clamped(monkeypatch):
    """limit=9999 is clamped to the 500 maximum."""
    fake = _FakeListDb(rows=[], total_count=0)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_list(limit=9999)
    assert resp["ok"] is True
    assert fake.applied_limit == 500
    assert resp["data"]["limit"] == 500


def test_kb_list_schema_has_pagination():
    schema = srv._TOOL_SCHEMA_MAP["kb_list"]
    props = schema["properties"]
    assert props["limit"]["minimum"] == 1
    assert props["limit"]["maximum"] == 500
    assert props["offset"]["minimum"] == 0


# ---------------------------------------------------------------------------
# BUG-5: multi_search description reflects cross-source (not multi-query) search
# ---------------------------------------------------------------------------


def test_multi_search_description_updated():
    schema_desc = next(t.description for t in srv._TOOL_DEFINITIONS if t.name == "multi_search")
    assert "across all configured sources" in schema_desc
    assert "multiple queries" not in schema_desc.lower()


# ---------------------------------------------------------------------------
# Issue #20: multi_search must surface the same KB results that a direct
# kb_search call returns for the same query (regression guard).
# ---------------------------------------------------------------------------


def test_multi_search_kb_path_returns_same_results_as_kb_search(monkeypatch):
    """multi_search.knowledge.kb_entries == kb_search.data.results for one query.

    Issue #20 — QA reported that multi_search returns 0 KB results while
    kb_search returns the same KB entries for the same query. This pins down
    the contract: the multi_search subpath calls handle_kb_search verbatim and
    forwards data.results into knowledge.kb_entries with no filtering.
    """

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def or_(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def execute(self):
            from lore.db_client import QueryResult

            return QueryResult(data=self._rows)

    class _Db:
        vec_extension_loaded = False
        fts5_available = False

        def __init__(self, rows):
            self._rows = rows

        def table(self, _name):
            return _Q(self._rows)

    rows = [
        {
            "kb_id": "kb_photo",
            "title": "Photosynthesis quantum effects",
            "topic": "biology",
            "trust_score": 1.0,
        },
        {
            "kb_id": "kb_other",
            "title": "Quantum tunnelling overview",
            "topic": "physics",
            "trust_score": 1.0,
        },
    ]
    monkeypatch.setattr(srv, "db", _Db(rows))
    # Disable mining so telemetry side-effects are no-ops in this unit test.
    import lore.telemetry as tel

    monkeypatch.setattr(tel, "mining_enabled", lambda: False)
    # Ensure we hit the legacy lexical path (no Postgres FTS, no embeddings).
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.delenv("LORE_SEMANTIC_SEARCH", raising=False)

    direct = srv.handle_kb_search(query="photosynthesis quantum")
    via_multi = srv.handle_multi_search(query="photosynthesis quantum")

    assert direct["ok"] is True
    assert via_multi["ok"] is True

    direct_ids = [r["kb_id"] for r in direct["data"]["results"]]
    multi_ids = [r["kb_id"] for r in via_multi["data"]["results"]["knowledge"]["kb_entries"]]

    # Must surface the same KB ids in the same order, with the same count.
    assert multi_ids == direct_ids
    assert len(multi_ids) == len(rows)
    # And the kb_entries field is never silently empty when kb_search has matches.
    assert via_multi["data"]["results"]["knowledge"]["kb_entries"]


# ---------------------------------------------------------------------------
# BUG-6: kb_sync_status dir_path optional with env-var fallback
# ---------------------------------------------------------------------------


def test_kb_sync_status_no_dir_uses_env_var(monkeypatch, tmp_path):
    """With LORE_SYNC_DIR set, calling with no dir_path is NOT a validation error."""
    monkeypatch.setenv("LORE_SYNC_DIR", str(tmp_path))
    monkeypatch.delenv("LORE_KB_DIR", raising=False)

    # Stub the DB sync-record read so the handler reaches its normal success path.
    class _SyncDb:
        def table(self, _name):
            return self

        def select(self, *_a, **_k):
            return self

        def execute(self):
            return QueryResult(data=[])

    monkeypatch.setattr(srv, "db", _SyncDb())

    resp = srv.handle_kb_sync_status()
    assert resp["ok"] is True
    assert resp.get("error") is None


def test_kb_sync_status_no_dir_no_env_returns_not_configured(monkeypatch):
    """Neither dir_path nor env var: clean not_configured error, not a crash."""
    monkeypatch.delenv("LORE_SYNC_DIR", raising=False)
    monkeypatch.delenv("LORE_KB_DIR", raising=False)

    resp = srv.handle_kb_sync_status()
    assert resp["ok"] is False
    assert resp["error"] == "not_configured"
    assert resp["error"] != "unexpected_exception"


def test_kb_sync_status_schema_dir_path_optional():
    schema = srv._TOOL_SCHEMA_MAP["kb_sync_status"]
    assert "dir_path" not in schema.get("required", [])


# ---------------------------------------------------------------------------
# Issue #16: kb_search min_score relevance threshold filter
#
# Pure query-layer addition. hybrid mode filters on rrf_score; fts/semantic
# filter on score. count in the response reflects the post-filter total.
# ---------------------------------------------------------------------------


# --- Unit tests for the _filter_by_min_score helper -----------------------


def test_min_score_none_is_noop_no_filtering():
    """min_score=None returns the list unchanged (backward compat)."""
    rows = [
        {"kb_id": "a", "score": 0.9},
        {"kb_id": "b", "score": 0.1},
        {"kb_id": "c"},  # missing score entirely
    ]
    out = srv._filter_by_min_score(rows, "fts", None)
    assert out == rows  # identical, nothing dropped


def test_min_score_zero_passes_all_results_boundary():
    """min_score=0.0 keeps every result whose score >= 0.0 (boundary)."""
    rows = [
        {"kb_id": "a", "score": 0.9},
        {"kb_id": "b", "score": 0.0},  # exactly at threshold -> kept
    ]
    out = srv._filter_by_min_score(rows, "fts", 0.0)
    assert [r["kb_id"] for r in out] == ["a", "b"]


def test_min_score_one_keeps_only_perfect_score():
    """min_score=1.0 keeps only results scoring >= 1.0 (boundary)."""
    rows = [
        {"kb_id": "a", "score": 1.0},  # exactly perfect -> kept
        {"kb_id": "b", "score": 0.99},  # just under -> dropped
        {"kb_id": "c", "score": 0.5},
    ]
    out = srv._filter_by_min_score(rows, "fts", 1.0)
    assert [r["kb_id"] for r in out] == ["a"]


def test_min_score_hybrid_filters_on_rrf_score():
    """Hybrid mode filters on rrf_score, ignoring any score field."""
    rows = [
        {"kb_id": "a", "rrf_score": 0.8, "score": 0.0},  # rrf passes
        {"kb_id": "b", "rrf_score": 0.2, "score": 0.95},  # rrf fails (score irrelevant)
    ]
    out = srv._filter_by_min_score(rows, "hybrid", 0.5)
    assert [r["kb_id"] for r in out] == ["a"]


def test_min_score_fts_filters_on_score():
    """fts mode filters on the score field."""
    rows = [
        {"kb_id": "a", "score": 0.6},
        {"kb_id": "b", "score": 0.4},
    ]
    out = srv._filter_by_min_score(rows, "fts", 0.5)
    assert [r["kb_id"] for r in out] == ["a"]


def test_min_score_semantic_filters_on_score():
    """semantic mode filters on the score field (same path as fts)."""
    rows = [
        {"kb_id": "a", "score": 0.7},
        {"kb_id": "b", "score": 0.3},
    ]
    out = srv._filter_by_min_score(rows, "semantic", 0.5)
    assert [r["kb_id"] for r in out] == ["a"]


def test_min_score_missing_score_defaults_to_zero():
    """A result with no score is treated as 0.0 and dropped by any positive min."""
    rows = [{"kb_id": "a"}, {"kb_id": "b", "score": 0.9}]
    out = srv._filter_by_min_score(rows, "fts", 0.1)
    assert [r["kb_id"] for r in out] == ["b"]


def test_min_score_all_below_threshold_returns_empty():
    """When every result is below the threshold, the list is empty."""
    rows = [{"kb_id": "a", "score": 0.2}, {"kb_id": "b", "score": 0.1}]
    out = srv._filter_by_min_score(rows, "fts", 0.9)
    assert out == []


# --- End-to-end tests through handle_kb_search ----------------------------


def _make_search_db(*, fts5=False, vec=False):
    """Minimal fake db exposing only the capability flags kb_search inspects."""

    class _SearchDb:
        fts5_available = fts5
        vec_extension_loaded = vec

    return _SearchDb()


def test_kb_search_fts_min_score_filters_and_counts(monkeypatch):
    """SQLite fts path: results below min_score excluded; count = filtered len."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.9, "content": "x"},
            {"kb_id": "b", "title": "B", "score": 0.4, "content": "y"},
            {"kb_id": "c", "title": "C", "score": 0.6, "content": "z"},
        ],
    )

    resp = srv.handle_kb_search("q", search_mode="fts", min_score=0.5)
    assert resp["ok"] is True
    kept_ids = [r["kb_id"] for r in resp["data"]["results"]]
    assert kept_ids == ["a", "c"]  # b (0.4) dropped
    # count reflects the POST-filter total, not the pre-filter 3.
    assert resp["data"]["count"] == 2
    assert resp["data"]["count"] == len(resp["data"]["results"])


def test_kb_search_fts_no_min_score_returns_all(monkeypatch):
    """Backward compat: omitting min_score returns every result."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.9, "content": "x"},
            {"kb_id": "b", "title": "B", "score": 0.1, "content": "y"},
        ],
    )

    resp = srv.handle_kb_search("q", search_mode="fts")
    assert resp["ok"] is True
    assert resp["data"]["count"] == 2
    assert [r["kb_id"] for r in resp["data"]["results"]] == ["a", "b"]


def test_kb_search_fts_min_score_all_below_returns_empty(monkeypatch):
    """Edge: min_score above every result -> empty list, count=0."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.3, "content": "x"},
            {"kb_id": "b", "title": "B", "score": 0.2, "content": "y"},
        ],
    )

    resp = srv.handle_kb_search("q", search_mode="fts", min_score=0.9)
    assert resp["ok"] is True
    assert resp["data"]["results"] == []
    assert resp["data"]["count"] == 0


def test_kb_search_hybrid_min_score_filters_on_rrf_score(monkeypatch):
    """SQLite hybrid path: filtering uses rrf_score, count = filtered len."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setenv("LORE_SEMANTIC_SEARCH", "true")
    # Hybrid path needs vec extension + fts5 (else it downgrades to semantic).
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True, vec=True))
    monkeypatch.setattr(srch, "semantic_enabled", lambda: True)
    monkeypatch.setattr(srch, "rrf_k", lambda: 60)
    # Avoid loading any embedding model.
    import lore.embeddings as _emb

    monkeypatch.setattr(_emb, "get_model_name", lambda: "fake-model")
    monkeypatch.setattr(_emb, "encode_text", lambda _q: [0.0] * 4)
    monkeypatch.setattr(
        srch,
        "hybrid_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "rrf_score": 0.05},
            {"kb_id": "b", "title": "B", "rrf_score": 0.01},
            {"kb_id": "c", "title": "C", "rrf_score": 0.03},
        ],
    )

    resp = srv.handle_kb_search("q", search_mode="hybrid", min_score=0.025)
    assert resp["ok"] is True
    assert resp["data"]["search_mode"] == "hybrid"
    kept_ids = [r["kb_id"] for r in resp["data"]["results"]]
    assert kept_ids == ["a", "c"]  # b (0.01) dropped
    assert resp["data"]["count"] == 2
    assert resp["data"]["count"] == len(resp["data"]["results"])


def test_kb_search_postgres_fts_min_score_filters_and_counts(monkeypatch):
    """PostgreSQL fts path: results below min_score excluded; count = filtered len."""
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setattr(srv, "db", _make_search_db())
    monkeypatch.setattr(
        srch,
        "fts_search_postgres",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.9, "content": "x"},
            {"kb_id": "b", "title": "B", "score": 0.4, "content": "y"},
            {"kb_id": "c", "title": "C", "score": 0.6, "content": "z"},
        ],
    )

    resp = srv.handle_kb_search("q", search_mode="fts", min_score=0.5)
    assert resp["ok"] is True
    assert resp["data"]["search_mode"] == "fts"
    assert resp["data"]["backend"] == "postgres"
    kept_ids = [r["kb_id"] for r in resp["data"]["results"]]
    assert kept_ids == ["a", "c"]  # b (0.4) dropped
    # count reflects the POST-filter total, not the pre-filter 3.
    assert resp["data"]["count"] == 2
    assert resp["data"]["count"] == len(resp["data"]["results"])


def test_kb_search_postgres_hybrid_min_score_filters_on_rrf_score(monkeypatch):
    """PostgreSQL hybrid path: filtering uses rrf_score, count = filtered len."""
    monkeypatch.setenv("DB_BACKEND", "postgres")
    monkeypatch.setenv("LORE_SEMANTIC_SEARCH", "true")
    # Postgres vector path needs semantic enabled + vec extension loaded.
    monkeypatch.setattr(srv, "db", _make_search_db(vec=True))
    monkeypatch.setattr(srch, "semantic_enabled", lambda: True)
    monkeypatch.setattr(srch, "rrf_k", lambda: 60)
    # Avoid loading any embedding model.
    import lore.embeddings as _emb

    monkeypatch.setattr(_emb, "get_model_name", lambda: "fake-model")
    monkeypatch.setattr(_emb, "encode_text", lambda _q: [0.0] * 4)
    monkeypatch.setattr(
        srch,
        "hybrid_search_postgres",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "rrf_score": 0.05},
            {"kb_id": "b", "title": "B", "rrf_score": 0.01},
            {"kb_id": "c", "title": "C", "rrf_score": 0.03},
        ],
    )

    resp = srv.handle_kb_search("q", search_mode="hybrid", min_score=0.025)
    assert resp["ok"] is True
    assert resp["data"]["search_mode"] == "hybrid"
    assert resp["data"]["backend"] == "postgres"
    kept_ids = [r["kb_id"] for r in resp["data"]["results"]]
    assert kept_ids == ["a", "c"]  # b (0.01) dropped
    assert resp["data"]["count"] == 2
    assert resp["data"]["count"] == len(resp["data"]["results"])


def test_kb_search_min_score_in_schema():
    """The kb_search inputSchema must expose an optional numeric min_score."""
    schema = srv._TOOL_SCHEMA_MAP["kb_search"]
    assert "min_score" in schema["properties"]
    assert schema["properties"]["min_score"]["type"] == "number"
    # min_score is optional (not required) for backward compatibility.
    assert "min_score" not in schema.get("required", [])


# ---------------------------------------------------------------------------
# Issue #14: trust_score field on KB entries
#
# kb_add accepts an optional, validated trust_score (default 1.0); kb_update
# updates it only when provided; kb_search returns it and supports a
# min_trust_score filter that composes with min_score. A missing trust_score
# defaults to 1.0 (fully trusted) for backward compatibility with legacy rows.
# ---------------------------------------------------------------------------


class _FakeInsertQuery:
    """Fluent fake for the kb_add chain: table(...).insert(entry).execute()."""

    def __init__(self, db: _FakeInsertDb):
        self._db = db

    def insert(self, data, upsert=False):
        self._db.inserted.append(data)
        return self

    def execute(self):
        return QueryResult(data=[])


class _FakeInsertDb:
    """Captures the entry dict handed to kb_add's INSERT."""

    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, _name):
        return _FakeInsertQuery(self)


# --- kb_add validation + pass-through -------------------------------------


def test_kb_add_default_trust_score_present_in_response(monkeypatch):
    """kb_add with no trust_score defaults to 1.0 in both the row and response."""
    fake = _FakeInsertDb()
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_add(topic="t", title="T", content="c")
    assert resp["ok"] is True
    assert resp["data"]["trust_score"] == 1.0
    assert fake.inserted[0]["trust_score"] == 1.0


def test_kb_add_explicit_trust_score_stored_and_returned(monkeypatch):
    """kb_add with trust_score=0.7 stores and echoes the value."""
    fake = _FakeInsertDb()
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_add(topic="t", title="T", content="c", trust_score=0.7)
    assert resp["ok"] is True
    assert resp["data"]["trust_score"] == 0.7
    assert fake.inserted[0]["trust_score"] == 0.7


def test_kb_add_trust_score_above_one_rejected(monkeypatch):
    """trust_score=1.5 is out of range -> invalid_input, no DB write."""
    fake = _FakeInsertDb()
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_add(topic="t", title="T", content="c", trust_score=1.5)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert fake.inserted == []  # never reached the DB


def test_kb_add_trust_score_below_zero_rejected(monkeypatch):
    """trust_score=-0.1 is out of range -> invalid_input, no DB write."""
    fake = _FakeInsertDb()
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_add(topic="t", title="T", content="c", trust_score=-0.1)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert fake.inserted == []


def test_kb_add_trust_score_boundaries_accepted(monkeypatch):
    """The inclusive bounds 0.0 and 1.0 are both valid."""
    for value in (0.0, 1.0):
        fake = _FakeInsertDb()
        monkeypatch.setattr(srv, "db", fake)
        resp = srv.handle_kb_add(topic="t", title="T", content="c", trust_score=value)
        assert resp["ok"] is True
        assert fake.inserted[0]["trust_score"] == value


# --- kb_update -------------------------------------------------------------


def test_kb_update_trust_score_updates_field(monkeypatch):
    """kb_update with trust_score=0.3 writes trust_score into the update payload."""
    existing = {"kb_id": "kb_ts", "title": "T", "content": "c", "trust_score": 1.0}
    updated = {"kb_id": "kb_ts", "title": "T", "content": "c", "trust_score": 0.3}
    fake = _FakeDb(current_row=existing, updated_row=updated)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_update(kb_id="kb_ts", trust_score=0.3)
    assert resp["ok"] is True
    assert "trust_score" in resp["data"]["updated_fields"]
    assert fake.updates[0]["trust_score"] == 0.3


def test_kb_update_without_trust_score_does_not_reset(monkeypatch):
    """Omitting trust_score must NOT write the column (preserves existing value)."""
    existing = {"kb_id": "kb_ts2", "title": "Old", "content": "c", "trust_score": 0.5}
    updated = {"kb_id": "kb_ts2", "title": "New", "content": "c", "trust_score": 0.5}
    fake = _FakeDb(current_row=existing, updated_row=updated)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_update(kb_id="kb_ts2", title="New")
    assert resp["ok"] is True
    # trust_score must be absent from the update payload (left unchanged).
    assert "trust_score" not in fake.updates[0]
    assert "trust_score" not in resp["data"]["updated_fields"]


def test_kb_update_trust_score_out_of_range_rejected(monkeypatch):
    """An out-of-range trust_score on update -> invalid_input, no DB write."""
    fake = _FakeDb(current_row={"kb_id": "kb_ts3", "title": "T", "content": "c"})
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_update(kb_id="kb_ts3", trust_score=2.0)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert fake.updates == []  # never reached the DB


# --- _filter_by_min_trust_score helper ------------------------------------


def test_min_trust_score_none_is_noop():
    """min_trust_score=None returns the list unchanged (backward compat)."""
    rows = [{"kb_id": "a", "trust_score": 0.2}, {"kb_id": "b"}]
    out = srv._filter_by_min_trust_score(rows, None)
    assert out == rows


def test_min_trust_score_missing_defaults_to_one():
    """A row with no trust_score is treated as 1.0 (fully trusted) and kept."""
    rows = [{"kb_id": "a"}, {"kb_id": "b", "trust_score": 0.1}]
    out = srv._filter_by_min_trust_score(rows, 0.7)
    assert [r["kb_id"] for r in out] == ["a"]  # legacy row kept, low-conf dropped


def test_min_trust_score_zero_passes_all():
    """min_trust_score=0.0 keeps every result (boundary)."""
    rows = [{"kb_id": "a", "trust_score": 0.0}, {"kb_id": "b", "trust_score": 0.5}]
    out = srv._filter_by_min_trust_score(rows, 0.0)
    assert [r["kb_id"] for r in out] == ["a", "b"]


def test_min_trust_score_filters_below_threshold():
    """Rows with trust_score below the threshold are excluded."""
    rows = [
        {"kb_id": "a", "trust_score": 0.9},
        {"kb_id": "b", "trust_score": 0.4},
        {"kb_id": "c", "trust_score": 0.7},
    ]
    out = srv._filter_by_min_trust_score(rows, 0.7)
    assert [r["kb_id"] for r in out] == ["a", "c"]  # b (0.4) dropped


# --- kb_search end-to-end --------------------------------------------------


def test_kb_search_returns_trust_score_field(monkeypatch):
    """kb_search results carry the trust_score column through to the response."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.9, "trust_score": 0.8, "content": "x"},
        ],
    )
    resp = srv.handle_kb_search("q", search_mode="fts")
    assert resp["ok"] is True
    assert resp["data"]["results"][0]["trust_score"] == 0.8


def test_kb_search_min_trust_score_filters_and_counts(monkeypatch):
    """min_trust_score excludes low-confidence rows; count = filtered len."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.9, "trust_score": 0.9, "content": "x"},
            {"kb_id": "b", "title": "B", "score": 0.8, "trust_score": 0.4, "content": "y"},
            {"kb_id": "c", "title": "C", "score": 0.7, "trust_score": 0.7, "content": "z"},
        ],
    )
    resp = srv.handle_kb_search("q", search_mode="fts", min_trust_score=0.7)
    assert resp["ok"] is True
    kept_ids = [r["kb_id"] for r in resp["data"]["results"]]
    assert kept_ids == ["a", "c"]  # b (0.4) dropped
    assert resp["data"]["count"] == 2
    assert resp["data"]["count"] == len(resp["data"]["results"])


def test_kb_search_min_trust_score_zero_passes_all(monkeypatch):
    """min_trust_score=0.0 keeps every result."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "a", "title": "A", "score": 0.9, "trust_score": 0.1, "content": "x"},
            {"kb_id": "b", "title": "B", "score": 0.8, "trust_score": 0.0, "content": "y"},
        ],
    )
    resp = srv.handle_kb_search("q", search_mode="fts", min_trust_score=0.0)
    assert resp["ok"] is True
    assert [r["kb_id"] for r in resp["data"]["results"]] == ["a", "b"]
    assert resp["data"]["count"] == 2


def test_kb_search_min_trust_score_and_min_score_compose(monkeypatch):
    """Both filters apply: a row must clear min_trust_score AND min_score."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            # passes both
            {"kb_id": "a", "title": "A", "score": 0.9, "trust_score": 0.9, "content": "x"},
            # high score but low trust -> dropped by min_trust_score
            {"kb_id": "b", "title": "B", "score": 0.9, "trust_score": 0.2, "content": "y"},
            # high trust but low score -> dropped by min_score
            {"kb_id": "c", "title": "C", "score": 0.1, "trust_score": 0.9, "content": "z"},
        ],
    )
    resp = srv.handle_kb_search("q", search_mode="fts", min_score=0.5, min_trust_score=0.7)
    assert resp["ok"] is True
    kept_ids = [r["kb_id"] for r in resp["data"]["results"]]
    assert kept_ids == ["a"]  # only the row clearing both thresholds survives
    assert resp["data"]["count"] == 1


def test_kb_search_missing_trust_score_defaults_to_trusted(monkeypatch):
    """Legacy rows lacking trust_score are treated as 1.0 and survive a filter."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.setattr(srv, "db", _make_search_db(fts5=True))
    monkeypatch.setattr(
        srch,
        "fts5_search_sqlite",
        lambda *_a, **_k: [
            {"kb_id": "legacy", "title": "L", "score": 0.9, "content": "x"},  # no trust_score
            {"kb_id": "low", "title": "Lo", "score": 0.9, "trust_score": 0.3, "content": "y"},
        ],
    )
    resp = srv.handle_kb_search("q", search_mode="fts", min_trust_score=0.7)
    assert resp["ok"] is True
    assert [r["kb_id"] for r in resp["data"]["results"]] == ["legacy"]


# --- schema assertions -----------------------------------------------------


def test_kb_add_trust_score_in_schema():
    """kb_add exposes an optional numeric trust_score bounded to [0.0, 1.0]."""
    schema = srv._TOOL_SCHEMA_MAP["kb_add"]
    ts = schema["properties"]["trust_score"]
    assert ts["type"] == "number"
    assert ts["minimum"] == 0.0
    assert ts["maximum"] == 1.0
    assert ts["default"] == 1.0
    assert "trust_score" not in schema.get("required", [])


def test_kb_update_trust_score_in_schema():
    """kb_update exposes an optional numeric trust_score bounded to [0.0, 1.0]."""
    schema = srv._TOOL_SCHEMA_MAP["kb_update"]
    ts = schema["properties"]["trust_score"]
    assert ts["type"] == "number"
    assert ts["minimum"] == 0.0
    assert ts["maximum"] == 1.0
    assert "trust_score" not in schema.get("required", [])


def test_kb_search_min_trust_score_in_schema():
    """kb_search exposes an optional numeric min_trust_score bounded to [0.0, 1.0]."""
    schema = srv._TOOL_SCHEMA_MAP["kb_search"]
    mts = schema["properties"]["min_trust_score"]
    assert mts["type"] == "number"
    assert mts["minimum"] == 0.0
    assert mts["maximum"] == 1.0
    assert "min_trust_score" not in schema.get("required", [])


# ---------------------------------------------------------------------------
# Code Critic WARN fixes (Issue #14):
#   1. FastMCP kb_search forwards min_score (Issue #16 regression)
#   2. kb_list SELECT includes trust_score
#   3. _validate_trust_score rejects bool (no silent float(True)==1.0)
#   4. kb_add with trust_score=None normalises to 1.0 (no float(None) crash)
# ---------------------------------------------------------------------------


# --- Finding 1: FastMCP wrapper forwards min_score ------------------------


def test_fastmcp_kb_search_accepts_min_score():
    """The FastMCP kb_search wrapper exposes a min_score parameter (Issue #16)."""
    import inspect

    import lore.server_fastmcp as fmcp

    params = inspect.signature(fmcp.kb_search).parameters
    assert "min_score" in params
    # Optional with a None default for backward compatibility.
    assert params["min_score"].default is None


def test_fastmcp_kb_search_forwards_min_score(monkeypatch):
    """The FastMCP wrapper passes min_score through to handle_kb_search."""
    import lore.server_fastmcp as fmcp

    captured: dict = {}

    def _fake_handle_kb_search(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "data": {"results": []}}

    monkeypatch.setattr(fmcp._srv, "handle_kb_search", _fake_handle_kb_search)

    fmcp.kb_search("q", min_score=0.42, min_trust_score=0.7)
    assert captured["min_score"] == 0.42
    # min_trust_score still forwarded alongside (no regression).
    assert captured["min_trust_score"] == 0.7


# --- Finding 2: kb_list SELECT includes trust_score -----------------------


class _SelectCapturingListQuery(_FakeListQuery):
    """_FakeListQuery that records the column list passed to select()."""

    def select(self, columns="*", **_k):
        self._db.selected_columns = columns
        return self


class _SelectCapturingListDb(_FakeListDb):
    def __init__(self, rows=None, total_count=0):
        super().__init__(rows=rows, total_count=total_count)
        self.selected_columns = None

    def table(self, _name):
        return _SelectCapturingListQuery(self)


def test_kb_list_selects_trust_score_column(monkeypatch):
    """kb_list's SELECT must name trust_score so clients can audit trust levels."""
    fake = _SelectCapturingListDb(rows=[], total_count=0)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_list()
    assert resp["ok"] is True
    assert "trust_score" in fake.selected_columns


def test_kb_list_returns_trust_score_in_entries(monkeypatch):
    """A row's trust_score flows through to the kb_list response entries."""
    rows = [{"kb_id": "kb_a", "title": "A", "trust_score": 0.6}]
    fake = _FakeListDb(rows=rows, total_count=1)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_kb_list()
    assert resp["ok"] is True
    assert resp["data"]["entries"][0]["trust_score"] == 0.6


# --- Finding 3: _validate_trust_score rejects bool ------------------------


def test_validate_trust_score_rejects_true():
    """True must not be silently coerced to 1.0 -> invalid_input."""
    err = srv._validate_trust_score(True)
    assert err is not None
    assert err["ok"] is False
    assert err["error"] == "invalid_input"


def test_validate_trust_score_rejects_false():
    """False must not be silently coerced to 0.0 -> invalid_input."""
    err = srv._validate_trust_score(False)
    assert err is not None
    assert err["ok"] is False
    assert err["error"] == "invalid_input"


def test_kb_add_bool_trust_score_rejected(monkeypatch):
    """kb_add with trust_score=True is rejected and never reaches the DB."""
    fake = _FakeInsertDb()
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_add(topic="t", title="T", content="c", trust_score=True)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert fake.inserted == []


# --- Finding 4: kb_add with trust_score=None normalises to 1.0 ------------


def test_kb_add_none_trust_score_normalises_to_one(monkeypatch):
    """kb_add(trust_score=None) must default to 1.0 without raising TypeError."""
    fake = _FakeInsertDb()
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_kb_add(topic="t", title="T", content="c", trust_score=None)
    assert resp["ok"] is True
    assert resp["data"]["trust_score"] == 1.0
    assert fake.inserted[0]["trust_score"] == 1.0


# ---------------------------------------------------------------------------
# _StrictArgsMiddleware: unknown kwargs on fixed-signature tools return a clean
# invalid_input business envelope instead of a raw -32603 JSON-RPC error that
# would trip Hermes's circuit breaker (Code Critic findings, issue #17/#18).
#
# Driven via asyncio.run() over the FastMCP in-memory Client to match the
# project convention (no pytest-asyncio marker; --strict-markers).
# ---------------------------------------------------------------------------


def test_fastmcp_middleware_rejects_unknown_kwarg(monkeypatch):
    """kb_list with a hallucinated filter arg returns ok=False/invalid_input.

    The middleware must short-circuit *before* pydantic validation fires, so
    the client receives a clean business envelope (with the "env" field) rather
    than a -32603 ToolError that would mark the MCP server unreachable.
    """
    import asyncio
    import json as _json

    from fastmcp import Client

    import lore.server_fastmcp as fmcp

    # Avoid touching a real DB: if the middleware ever falls through to the
    # handler, this fake makes the failure mode obvious instead of hitting disk.
    monkeypatch.setattr(
        fmcp._srv,
        "handle_kb_list",
        lambda *a, **k: pytest.fail("handler should not run for unknown kwargs"),
    )

    async def _call() -> dict:
        async with Client(fmcp.mcp) as client:
            # call_tool() raises ToolError on a -32603, so reaching the assert
            # below already proves no raw JSON-RPC error escaped.
            result = await client.call_tool("kb_list", {"created_at__gte": "2024-01-01"})
            return _json.loads(result.content[0].text)

    envelope = asyncio.run(_call())
    assert envelope["ok"] is False
    assert envelope["error"] == "invalid_input"
    # Finding 1: the hand-rolled payload previously omitted "env"; it must be
    # present now that ResponseEnvelope.error() builds the payload.
    assert "env" in envelope
    assert "created_at__gte" in envelope["message"]


def test_fastmcp_middleware_allows_known_kwargs(monkeypatch):
    """A kb_list call using only supported params passes through to the handler."""
    import asyncio
    import json as _json

    from fastmcp import Client

    import lore.server_fastmcp as fmcp

    captured: dict = {}

    def _fake_handle_kb_list(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "error": None, "message": "ok", "env": "test", "data": {}}

    monkeypatch.setattr(fmcp._srv, "handle_kb_list", _fake_handle_kb_list)

    async def _call() -> dict:
        async with Client(fmcp.mcp) as client:
            result = await client.call_tool("kb_list", {"topic": "x", "limit": 5})
            return _json.loads(result.content[0].text)

    envelope = asyncio.run(_call())
    assert envelope["ok"] is True
    assert captured["topic"] == "x"
    assert captured["limit"] == 5


# ---------------------------------------------------------------------------
# Issue #21: journal_delete + investigation_delete_note +
#            investigation_delete_experiment.
#
# Each new destructive tool follows the kb_delete safety contract:
#   * confirm=True is mandatory; missing it returns a clean invalid_input
#   * LORE_ENV=production additionally requires confirm_production=True
#   * unknown IDs return not_found (never raise)
# ---------------------------------------------------------------------------


class _DeleteDb:
    """Tiny fluent fake supporting select/maybe_single/execute + delete/eq/execute.

    Distinct from ``_FakeDb`` because the delete handlers do not perform an
    update step. Construct with the row to return on read (or None for "missing").
    """

    def __init__(self, current_row=None):
        self.current_row = current_row
        self.deleted = False
        self._in_delete = False

    def table(self, _name):
        return self

    def select(self, *_a, **_k):
        self._in_delete = False
        return self

    def eq(self, *_a, **_k):
        return self

    def maybe_single(self):
        return self

    def delete(self):
        self._in_delete = True
        self.deleted = True
        return self

    def execute(self):
        if self._in_delete:
            return QueryResult(data={})
        return QueryResult(data=self.current_row)


# ---- journal_delete -------------------------------------------------------


def test_journal_delete_success(monkeypatch):
    """confirm=True + existing entry → ok=True with deleted=True."""
    monkeypatch.setenv("LORE_ENV", "development")
    fake = _DeleteDb(current_row={"entry_id": "jrnl_1", "content": "x"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_journal_delete(entry_id="jrnl_1", confirm=True)
    assert resp["ok"] is True
    assert resp["data"]["entry_id"] == "jrnl_1"
    assert resp["data"]["deleted"] is True
    assert fake.deleted is True


def test_journal_delete_missing_confirm_returns_invalid_input(monkeypatch):
    """confirm=False (default) → invalid_input, no DB touch."""
    fake = _DeleteDb(current_row={"entry_id": "jrnl_1"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_journal_delete(entry_id="jrnl_1")
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "confirm=True" in resp["message"]
    assert fake.deleted is False


def test_journal_delete_not_found(monkeypatch):
    """Missing row → not_found envelope, no raise."""
    monkeypatch.setenv("LORE_ENV", "development")
    fake = _DeleteDb(current_row=None)
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_journal_delete(entry_id="jrnl_nope", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "not_found"
    assert "jrnl_nope" in resp["message"]


def test_journal_delete_production_guard(monkeypatch):
    """LORE_ENV=production + confirm=True but confirm_production=False → blocked."""
    monkeypatch.setenv("LORE_ENV", "production")
    fake = _DeleteDb(current_row={"entry_id": "jrnl_1"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_journal_delete(entry_id="jrnl_1", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "production_guard"
    assert fake.deleted is False

    # And lifts when confirm_production=True is provided.
    resp_ok = srv.handle_journal_delete(entry_id="jrnl_1", confirm=True, confirm_production=True)
    assert resp_ok["ok"] is True
    assert fake.deleted is True


def test_journal_delete_missing_entry_id(monkeypatch):
    """No entry_id → invalid_input."""
    monkeypatch.setenv("LORE_ENV", "development")
    monkeypatch.setattr(srv, "db", _DeleteDb())
    resp = srv.handle_journal_delete(entry_id="", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "entry_id" in resp["message"]


# ---- investigation_delete_note --------------------------------------------


def test_investigation_delete_note_success(monkeypatch):
    monkeypatch.setenv("LORE_ENV", "development")
    fake = _DeleteDb(current_row={"note_id": "note_1", "title": "t"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_investigation_delete_note(note_id="note_1", confirm=True)
    assert resp["ok"] is True
    assert resp["data"]["note_id"] == "note_1"
    assert resp["data"]["deleted"] is True
    assert fake.deleted is True


def test_investigation_delete_note_missing_confirm(monkeypatch):
    fake = _DeleteDb(current_row={"note_id": "note_1"})
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_investigation_delete_note(note_id="note_1")
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "confirm=True" in resp["message"]
    assert fake.deleted is False


def test_investigation_delete_note_not_found(monkeypatch):
    monkeypatch.setenv("LORE_ENV", "development")
    monkeypatch.setattr(srv, "db", _DeleteDb(current_row=None))
    resp = srv.handle_investigation_delete_note(note_id="note_nope", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "not_found"
    assert "note_nope" in resp["message"]


def test_investigation_delete_note_production_guard(monkeypatch):
    monkeypatch.setenv("LORE_ENV", "production")
    fake = _DeleteDb(current_row={"note_id": "note_1"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_investigation_delete_note(note_id="note_1", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "production_guard"
    assert fake.deleted is False

    resp_ok = srv.handle_investigation_delete_note(
        note_id="note_1", confirm=True, confirm_production=True
    )
    assert resp_ok["ok"] is True
    assert fake.deleted is True


# ---- investigation_delete_experiment --------------------------------------


def test_investigation_delete_experiment_success(monkeypatch):
    monkeypatch.setenv("LORE_ENV", "development")
    fake = _DeleteDb(current_row={"experiment_id": "exp_1", "title": "t"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_investigation_delete_experiment(experiment_id="exp_1", confirm=True)
    assert resp["ok"] is True
    assert resp["data"]["experiment_id"] == "exp_1"
    assert resp["data"]["deleted"] is True
    assert fake.deleted is True


def test_investigation_delete_experiment_missing_confirm(monkeypatch):
    fake = _DeleteDb(current_row={"experiment_id": "exp_1"})
    monkeypatch.setattr(srv, "db", fake)
    resp = srv.handle_investigation_delete_experiment(experiment_id="exp_1")
    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"
    assert "confirm=True" in resp["message"]
    assert fake.deleted is False


def test_investigation_delete_experiment_not_found(monkeypatch):
    monkeypatch.setenv("LORE_ENV", "development")
    monkeypatch.setattr(srv, "db", _DeleteDb(current_row=None))
    resp = srv.handle_investigation_delete_experiment(experiment_id="exp_nope", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "not_found"
    assert "exp_nope" in resp["message"]


def test_investigation_delete_experiment_production_guard(monkeypatch):
    monkeypatch.setenv("LORE_ENV", "production")
    fake = _DeleteDb(current_row={"experiment_id": "exp_1"})
    monkeypatch.setattr(srv, "db", fake)

    resp = srv.handle_investigation_delete_experiment(experiment_id="exp_1", confirm=True)
    assert resp["ok"] is False
    assert resp["error"] == "production_guard"
    assert fake.deleted is False

    resp_ok = srv.handle_investigation_delete_experiment(
        experiment_id="exp_1", confirm=True, confirm_production=True
    )
    assert resp_ok["ok"] is True
    assert fake.deleted is True


# ---- schema sanity --------------------------------------------------------


def test_new_delete_tools_registered():
    """All three new delete tools live in the canonical tool definition list."""
    names = {t.name for t in srv._TOOL_DEFINITIONS}
    assert "journal_delete" in names
    assert "investigation_delete_note" in names
    assert "investigation_delete_experiment" in names


def test_new_delete_tools_schema_shape():
    """Schemas declare the right required field and confirm/confirm_production props."""
    expected = {
        "journal_delete": "entry_id",
        "investigation_delete_note": "note_id",
        "investigation_delete_experiment": "experiment_id",
    }
    for tool_name, id_field in expected.items():
        schema = srv._TOOL_SCHEMA_MAP[tool_name]
        assert schema["required"] == [id_field]
        assert id_field in schema["properties"]
        assert "confirm" in schema["properties"]
        assert "confirm_production" in schema["properties"]
