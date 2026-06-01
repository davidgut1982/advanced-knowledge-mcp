"""Tests for extraction dedup (should_merge)."""

from __future__ import annotations

import asyncio

from lore.extraction.dedup import should_merge
from lore.extraction.schema import MemoryCandidate, MemoryType


class _FakeDB:
    """kb_search fake returning a fixed list of hits per call."""

    def __init__(self, hits):
        self._hits = hits
        self.calls: list[dict] = []

    def kb_search(self, query=None, *, top_k=3, topic=None):
        # Mirrors the real LoreClient.kb_search signature (top_k, not limit).
        self.calls.append({"query": query, "top_k": top_k, "topic": topic})
        return list(self._hits)


def _candidate():
    return MemoryCandidate(
        type=MemoryType.PREFERENCE, content="prefers dark mode", confidence=0.9
    )


# Hits carry rrf_score (the field the real Lore kb_search returns in hybrid
# mode, ~0.0-0.3) and are compared against the calibrated rrf_score threshold.


def test_should_merge_returns_false_when_no_similar():
    db = _FakeDB([{"kb_id": "kb_1", "rrf_score": 0.04}])
    merge, kb_id = asyncio.run(should_merge(_candidate(), db, rrf_threshold=0.12))
    assert merge is False
    assert kb_id is None


def test_should_merge_returns_true_above_threshold():
    db = _FakeDB([{"kb_id": "kb_42", "rrf_score": 0.18}])
    merge, kb_id = asyncio.run(should_merge(_candidate(), db, rrf_threshold=0.12))
    assert merge is True
    assert kb_id == "kb_42"


def test_should_merge_returns_false_below_threshold():
    db = _FakeDB([{"kb_id": "kb_7", "rrf_score": 0.08}])
    merge, kb_id = asyncio.run(should_merge(_candidate(), db, rrf_threshold=0.12))
    assert merge is False
    assert kb_id is None


def test_should_merge_topic_none_fallback() -> None:
    # No hit for the auto-memory topic, but a qualifying hit (rrf_score=0.15)
    # for topic=None (the whole-KB second pass) — the candidate must still merge
    # against the manually-added duplicate found in the second pass.
    class _TopicAwareDB:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def kb_search(self, query=None, *, top_k=3, topic=None):  # type: ignore[no-untyped-def]
            self.calls.append({"query": query, "top_k": top_k, "topic": topic})
            if topic is None:
                return [{"kb_id": "kb_manual", "rrf_score": 0.15}]
            return []

    db = _TopicAwareDB()
    merge, kb_id = asyncio.run(should_merge(_candidate(), db, rrf_threshold=0.12))
    assert merge is True
    assert kb_id == "kb_manual"


def test_should_merge_no_score_field() -> None:
    # A hit with no recognized score field scores 0.0 and never merges.
    db = _FakeDB([{"kb_id": "kb_x"}])
    merge, kb_id = asyncio.run(should_merge(_candidate(), db, rrf_threshold=0.12))
    assert merge is False
    assert kb_id is None
