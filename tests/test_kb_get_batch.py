"""Unit tests for handle_kb_get_batch (Issue #25).

Uses the same fluent-fake pattern as test_handler_kb.py: a tiny _FakeDb
stand-in replaces srv.db so handlers run without a live database. The fake
implements the ``in_("kb_id", [...])`` filter so batch lookups exercise the
real handler logic (id -> row map, input-order preservation, missing -> None).
"""

from __future__ import annotations

import pytest

import lore.server as srv
from lore.db_client import QueryResult

# ---------------------------------------------------------------------------
# Fake query builder with ``in_`` support — kb_get_batch uses .in_("kb_id", …)
# rather than the single-id .eq() used by kb_get, so the existing _FakeQuery
# from test_handler_kb.py doesn't cover this path. A dedicated minimal fake
# keeps the test self-contained without retrofitting the shared one.
# ---------------------------------------------------------------------------


class _FakeBatchQuery:
    def __init__(self, db: _FakeBatchDb):
        self._db = db
        self._in_values: list[str] | None = None

    def select(self, *_a, **_k):
        return self

    def in_(self, column: str, values: list[str]):
        # Only kb_id batch lookups are exercised here; assert defensively so
        # an accidental column rename in the handler surfaces immediately
        # rather than silently returning the wrong rows.
        assert column == "kb_id"
        self._in_values = values
        return self

    def execute(self) -> QueryResult:
        if self._db.raises is not None:
            raise self._db.raises
        # Filter the seeded rows to those whose kb_id is in the requested set,
        # mirroring what a real SELECT … WHERE kb_id IN (…) would return.
        # Order is intentionally unspecified — the handler must not rely on
        # DB-side ordering and must reorder by the caller's input list itself.
        ids = set(self._in_values or [])
        rows = [r for r in self._db.rows if r.get("kb_id") in ids]
        return QueryResult(data=rows)


class _FakeBatchDb:
    def __init__(self, rows: list[dict] | None = None, raises: Exception | None = None):
        self.rows = rows or []
        self.raises = raises

    def table(self, _name: str):
        return _FakeBatchQuery(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(kb_id: str, title: str = "T", content: str = "c") -> dict:
    """Build a minimal KB entry row with the fields a real SELECT * returns."""
    return {
        "kb_id": kb_id,
        "title": title,
        "topic": "python",
        "content": content,
        "tags": [],
        "author": None,
        "source_type": "manual",
        "verified": False,
        "trust_score": 1.0,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_kb_get_batch_all_found(monkeypatch):
    """Three IDs all exist → entries list has 3 dicts each with content."""
    rows = [_entry("kb_1"), _entry("kb_2"), _entry("kb_3")]
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=rows))

    resp = srv.handle_kb_get_batch(kb_ids=["kb_1", "kb_2", "kb_3"])

    assert resp["ok"] is True
    data = resp["data"]
    assert data["found"] == 3
    assert data["missing"] == 0
    assert len(data["entries"]) == 3
    assert all(e is not None for e in data["entries"])
    assert all("content" in e for e in data["entries"])
    # Order MUST match input order so callers can align by index.
    assert [e["kb_id"] for e in data["entries"]] == ["kb_1", "kb_2", "kb_3"]


def test_kb_get_batch_partial_miss(monkeypatch):
    """Middle ID missing → entries[1] is None, found=2, missing=1."""
    rows = [_entry("kb_1"), _entry("kb_3")]
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=rows))

    resp = srv.handle_kb_get_batch(kb_ids=["kb_1", "kb_missing", "kb_3"])

    assert resp["ok"] is True
    data = resp["data"]
    assert data["found"] == 2
    assert data["missing"] == 1
    assert len(data["entries"]) == 3
    assert data["entries"][0] is not None
    assert data["entries"][0]["kb_id"] == "kb_1"
    # Position of the missing ID is preserved as None so the index aligns
    # with the caller's input list.
    assert data["entries"][1] is None
    assert data["entries"][2] is not None
    assert data["entries"][2]["kb_id"] == "kb_3"


def test_kb_get_batch_empty_list(monkeypatch):
    """Empty kb_ids → ok envelope with empty entries, found=0, missing=0."""
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=[]))

    resp = srv.handle_kb_get_batch(kb_ids=[])

    assert resp["ok"] is True
    assert resp["data"] == {"entries": [], "found": 0, "missing": 0}


def test_kb_get_batch_over_50_cap(monkeypatch):
    """51 IDs → ok=False with error='too_many_ids' and id count in message."""
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=[]))

    ids = [f"kb_{i}" for i in range(51)]
    resp = srv.handle_kb_get_batch(kb_ids=ids)

    assert resp["ok"] is False
    assert resp["error"] == "too_many_ids"
    # Surface the offending count so the caller knows how much to trim by.
    assert "51" in resp["message"]


def test_kb_get_batch_single_id(monkeypatch):
    """Single ID present → found=1, entries[0] has content."""
    rows = [_entry("kb_abc", content="hello world")]
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=rows))

    resp = srv.handle_kb_get_batch(kb_ids=["kb_abc"])

    assert resp["ok"] is True
    data = resp["data"]
    assert data["found"] == 1
    assert data["missing"] == 0
    assert len(data["entries"]) == 1
    assert data["entries"][0]["kb_id"] == "kb_abc"
    assert data["entries"][0]["content"] == "hello world"


def test_kb_get_batch_all_missing(monkeypatch):
    """No IDs exist → entries is all None, found=0, missing=N."""
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=[]))

    resp = srv.handle_kb_get_batch(kb_ids=["kb_x", "kb_y", "kb_z"])

    assert resp["ok"] is True
    data = resp["data"]
    assert data["found"] == 0
    assert data["missing"] == 3
    assert data["entries"] == [None, None, None]


# ---------------------------------------------------------------------------
# Additional defensive tests — wrong-type input and DB exception handling.
# These aren't in the required-six list but they protect the same envelope
# contract every other handler in lore.server upholds, so omitting them
# would leave kb_get_batch as the only KB handler without graceful failure
# modes for these two common error paths.
# ---------------------------------------------------------------------------


def test_kb_get_batch_non_list_input(monkeypatch):
    """Passing a string instead of a list → invalid_input envelope."""
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=[]))

    resp = srv.handle_kb_get_batch(kb_ids="kb_1")  # type: ignore[arg-type]

    assert resp["ok"] is False
    assert resp["error"] == "invalid_input"


def test_kb_get_batch_db_error_returns_envelope(monkeypatch):
    """A DB exception surfaces as unexpected_exception, not a raise."""
    monkeypatch.setattr(srv, "db", _FakeBatchDb(raises=RuntimeError("connection refused")))

    resp = srv.handle_kb_get_batch(kb_ids=["kb_1"])

    assert resp["ok"] is False
    assert resp["error"] == "unexpected_exception"


def test_kb_get_batch_duplicate_ids_preserved(monkeypatch):
    """Duplicate IDs in input list → each position resolves independently.

    Real-world callers (LLM agents) sometimes dedupe loosely; the handler
    must not crash and must return the same row at each duplicate slot so
    index alignment still holds.
    """
    rows = [_entry("kb_1", content="dup content")]
    monkeypatch.setattr(srv, "db", _FakeBatchDb(rows=rows))

    resp = srv.handle_kb_get_batch(kb_ids=["kb_1", "kb_1", "kb_missing"])

    assert resp["ok"] is True
    data = resp["data"]
    assert len(data["entries"]) == 3
    assert data["entries"][0] is not None
    assert data["entries"][1] is not None
    assert data["entries"][0]["kb_id"] == "kb_1"
    assert data["entries"][1]["kb_id"] == "kb_1"
    assert data["entries"][2] is None
    # found counts non-None positions, NOT unique rows.
    assert data["found"] == 2
    assert data["missing"] == 1
