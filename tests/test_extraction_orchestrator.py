"""Tests for the extract_and_store orchestrator."""

from __future__ import annotations

import asyncio
from unittest import mock

from lore.extraction import extract_and_store
from lore.extraction.schema import ExtractionResult, MemoryCandidate, MemoryType


class _FakeDB:
    """Records kb_add / kb_update and serves programmable kb_search hits."""

    def __init__(self, search_hits=None):
        self._search_hits = search_hits or []
        self.added: list[dict] = []
        self.updated: list[dict] = []

    def kb_search(self, query=None, *, top_k=3, topic=None):
        # Mirrors the real LoreClient.kb_search signature (top_k, not limit).
        return list(self._search_hits)

    def kb_add(self, *, topic, title, content, tags=None, author=None):
        # Mirrors LoreClient.kb_add: no trust_score kwarg. Confidence is
        # carried in tags instead.
        self.added.append(
            {
                "topic": topic,
                "title": title,
                "content": content,
                "tags": tags,
            }
        )
        return {"kb_id": f"kb_new{len(self.added)}"}

    def kb_update(self, kb_id, *, content=None, title=None, tags=None):
        self.updated.append({"kb_id": kb_id, "content": content, "tags": tags})
        return {"kb_id": kb_id}


def _config(enabled=True, **overrides):
    auto = {
        "enabled": enabled,
        "confidence_threshold": 0.75,
        # rrf_score-calibrated threshold (hybrid kb_search), not cosine.
        "dedup_similarity_threshold": 0.12,
        "min_turns": 3,
    }
    auto.update(overrides)
    return {"auto_extract": auto}


def _turns(n=3):
    return [{"role": "user", "content": f"message {i}"} for i in range(n)]


def _patch_extract(result: ExtractionResult):
    return mock.patch(
        "lore.extraction.ExtractionClient.extract",
        new=mock.AsyncMock(return_value=result),
    )


def test_returns_empty_when_disabled():
    db = _FakeDB()
    summary = asyncio.run(extract_and_store(_turns(), db, _config(enabled=False)))
    assert summary == {"extracted": 0, "inserted": 0, "merged": 0, "skipped": 0}


def test_returns_empty_below_min_turns():
    db = _FakeDB()
    with _patch_extract(ExtractionResult()) as patched:
        summary = asyncio.run(extract_and_store(_turns(2), db, _config(min_turns=3)))
    assert summary == {"extracted": 0, "inserted": 0, "merged": 0, "skipped": 0}
    patched.assert_not_called()  # extraction never even ran


def test_filters_low_confidence():
    db = _FakeDB()
    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.PREFERENCE, content="low conf", confidence=0.5
            )
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config()))
    assert summary["extracted"] == 1
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0
    assert db.added == []


def test_filters_non_durable():
    db = _FakeDB()
    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.EVENT,
                content="today I'm trying X",
                confidence=0.95,
                durable=False,
            )
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config()))
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0


def test_calls_kb_add_for_new_memory():
    db = _FakeDB(search_hits=[])  # nothing similar
    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.PREFERENCE,
                content="prefers Python",
                confidence=0.95,
                tags=["lang"],
            )
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config()))
    assert summary["inserted"] == 1
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry["topic"] == "auto-memory"
    assert entry["content"] == "prefers Python"
    assert "source:auto-extracted" in entry["tags"]
    assert "type:preference" in entry["tags"]
    assert "lang" in entry["tags"]
    # Confidence is encoded as a tag (no trust_score kwarg on the real client).
    assert "confidence:0.95" in entry["tags"]


def test_calls_kb_update_for_duplicate():
    db = _FakeDB(search_hits=[{"kb_id": "kb_existing", "rrf_score": 0.18}])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.PREFERENCE,
                content="prefers Python (refined)",
                confidence=0.95,
            )
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config()))
    assert summary["merged"] == 1
    assert summary["inserted"] == 0
    # Merge updates content AND refreshes tags (so confidence/type aren't stale).
    assert len(db.updated) == 1
    update = db.updated[0]
    assert update["kb_id"] == "kb_existing"
    assert update["content"] == "prefers Python (refined)"
    assert "source:auto-extracted" in update["tags"]
    assert "type:preference" in update["tags"]
    assert "confidence:0.95" in update["tags"]


def test_summary_counts_correct():
    # 3 candidates: one new (insert), one duplicate (merge), one low-conf (skip).
    db = _FakeDB()

    def _search(query=None, *, top_k=3, topic=None):
        # Only the "dup" candidate finds a high-rrf_score match.
        if "dup" in (query or ""):
            return [{"kb_id": "kb_dup", "rrf_score": 0.18}]
        return []

    db.kb_search = _search  # type: ignore[assignment]

    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.PREFERENCE, content="brand new fact", confidence=0.9
            ),
            MemoryCandidate(
                type=MemoryType.USER_FACT, content="dup existing fact", confidence=0.9
            ),
            MemoryCandidate(
                type=MemoryType.GOAL, content="weak signal", confidence=0.5
            ),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config()))

    assert summary == {
        "extracted": 3,
        "inserted": 1,
        "merged": 1,
        "skipped": 1,
    }
