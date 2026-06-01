"""Tests for the Point 5 control surface: dry-run, per-type thresholds, review mode."""

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
        self.searches: list[dict] = []

    def kb_search(self, query=None, *, top_k=3, topic=None):
        self.searches.append({"query": query, "topic": topic})
        return list(self._search_hits)

    def kb_add(self, *, topic, title, content, tags=None, author=None):
        self.added.append({"topic": topic, "title": title, "content": content, "tags": tags})
        return {"kb_id": f"kb_new{len(self.added)}"}

    def kb_update(self, kb_id, *, content=None, title=None, tags=None):
        self.updated.append({"kb_id": kb_id, "content": content, "tags": tags})
        return {"kb_id": kb_id}


def _config(enabled=True, **overrides):
    auto = {
        "enabled": enabled,
        "confidence_threshold": 0.75,
        "dedup_similarity_threshold": 0.12,
        "min_turns": 3,
    }
    auto.update(overrides)
    return {"auto_extract": auto}


def _turns(n=3):
    return [
        {"role": "user", "content": f"This is conversation message number {i}."} for i in range(n)
    ]


def _patch_extract(result: ExtractionResult):
    return mock.patch(
        "lore.extraction.ExtractionClient.extract",
        new=mock.AsyncMock(return_value=result),
    )


# --------------------------------------------------------------------------- #
# Feature 1: dry-run mode
# --------------------------------------------------------------------------- #


def test_dry_run_skips_kb_writes():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.PREFERENCE, content="prefers Python", confidence=0.95),
            MemoryCandidate(type=MemoryType.USER_FACT, content="lives in Riga", confidence=0.95),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config(dry_run=True)))

    assert db.added == []
    assert db.updated == []
    assert summary["dry_run"] is True
    assert summary["inserted"] == 0
    assert summary["merged"] == 0


def test_dry_run_still_runs_full_pipeline():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.PREFERENCE, content="prefers Python", confidence=0.95),
        ]
    )
    with _patch_extract(result) as patched:
        asyncio.run(extract_and_store(_turns(), db, _config(dry_run=True)))

    # LLM extraction still invoked (we want the candidate log output).
    patched.assert_awaited_once()
    # Dedup still checks the KB even though we never write.
    assert db.searches, "dedup search should still run in dry-run mode"


def test_dry_run_summary_has_flag():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.GOAL, content="learn Latvian grammar", confidence=0.95),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config(dry_run=True)))

    assert summary["dry_run"] is True
    assert summary["extracted"] == 1
    assert summary["skipped"] == 1


# --------------------------------------------------------------------------- #
# Feature 2: per-type confidence thresholds
# --------------------------------------------------------------------------- #

_TYPE_THRESHOLDS = {
    "user_fact": 0.85,
    "system_fact": 0.85,
    "preference": 0.80,
    "relationship": 0.80,
    "goal": 0.75,
    "event": 0.70,
}


def test_type_threshold_user_fact_higher():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.USER_FACT, content="user fact", confidence=0.82),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(
            extract_and_store(
                _turns(),
                db,
                _config(confidence_threshold=0.75, type_thresholds=_TYPE_THRESHOLDS),
            )
        )

    # 0.82 clears global 0.75 but fails the user_fact override (0.85).
    assert summary["skipped"] == 1
    assert summary["inserted"] == 0
    assert db.added == []


def test_type_threshold_event_lower():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.EVENT, content="event happened", confidence=0.72),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(
            extract_and_store(
                _turns(),
                db,
                _config(confidence_threshold=0.75, type_thresholds=_TYPE_THRESHOLDS),
            )
        )

    # 0.72 fails global 0.75 but clears the event override (0.70).
    assert summary["inserted"] == 1
    assert len(db.added) == 1


def test_type_threshold_falls_back_to_global():
    # Only "event" is overridden; a preference falls back to the global 0.75.
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.PREFERENCE, content="prefers tabs", confidence=0.78),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(
            extract_and_store(
                _turns(),
                db,
                _config(confidence_threshold=0.75, type_thresholds={"event": 0.70}),
            )
        )

    # 0.78 >= global 0.75 (preference not in the override dict).
    assert summary["inserted"] == 1
    assert len(db.added) == 1


# --------------------------------------------------------------------------- #
# Feature 3: review / pending mode
# --------------------------------------------------------------------------- #


def test_review_mode_new_insert_goes_to_pending():
    db = _FakeDB(search_hits=[])  # nothing similar -> new insert
    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.PREFERENCE, content="prefers dark mode", confidence=0.95
            ),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config(review_mode=True)))

    assert summary["inserted"] == 1
    assert len(db.added) == 1
    entry = db.added[0]
    assert entry["topic"] == "auto-memory-pending"
    assert "status:pending" in entry["tags"]
    assert "source:auto-extracted" in entry["tags"]


def test_review_mode_merge_bypasses_pending():
    db = _FakeDB(search_hits=[{"kb_id": "kb_existing", "rrf_score": 0.18}])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(
                type=MemoryType.PREFERENCE, content="prefers dark mode (refined)", confidence=0.95
            ),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config(review_mode=True)))

    # A merge confirms an accepted fact: it updates the existing entry directly,
    # never lands in pending, and adds nothing new.
    assert summary["merged"] == 1
    assert summary["inserted"] == 0
    assert db.added == []
    assert len(db.updated) == 1
    update = db.updated[0]
    assert update["kb_id"] == "kb_existing"
    assert "status:pending" not in (update["tags"] or [])


def test_review_mode_false_uses_normal_topic():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.PREFERENCE, content="prefers spaces", confidence=0.95),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(extract_and_store(_turns(), db, _config(review_mode=False)))

    assert summary["inserted"] == 1
    entry = db.added[0]
    assert entry["topic"] == "auto-memory"
    assert "status:pending" not in entry["tags"]


# --------------------------------------------------------------------------- #
# Combined: dry_run wins over review_mode
# --------------------------------------------------------------------------- #


def test_all_three_controls_combined():
    db = _FakeDB(search_hits=[])
    result = ExtractionResult(
        memories=[
            MemoryCandidate(type=MemoryType.EVENT, content="event happened", confidence=0.72),
            MemoryCandidate(type=MemoryType.PREFERENCE, content="prefers vim", confidence=0.95),
        ]
    )
    with _patch_extract(result):
        summary = asyncio.run(
            extract_and_store(
                _turns(),
                db,
                _config(
                    dry_run=True,
                    review_mode=True,
                    confidence_threshold=0.75,
                    type_thresholds=_TYPE_THRESHOLDS,
                ),
            )
        )

    # dry_run wins: nothing is written anywhere, not even to pending.
    assert db.added == []
    assert db.updated == []
    assert summary["dry_run"] is True
    assert summary["inserted"] == 0
    assert summary["merged"] == 0
