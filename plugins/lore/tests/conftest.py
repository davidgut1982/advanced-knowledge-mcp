"""Shared pytest fixtures for the Lore memory provider plugin.

Adds two paths to sys.path so tests can import the plugin without
hermes-agent installed:
  1. tests/_hermes_stubs/  — stub ABC, tool_error, cfg_get, hermes_constants
  2. the plugin parent dir  — so ``import lore`` resolves to plugins/lore/

The stubs mirror the real Hermes interfaces (verified against CT 133's
hermes-agent 0.14.0). On CT 133 the real modules shadow these stubs.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PLUGIN_DIR = _TESTS_DIR.parent  # plugins/lore/
_PLUGINS_PARENT = _PLUGIN_DIR.parent  # plugins/  (so `import lore` works)
_STUBS_DIR = _TESTS_DIR / "_hermes_stubs"

# Stubs FIRST so they win if hermes-agent happens to be importable locally,
# but on CT 133 we never run these tests against stubs.
for p in (str(_STUBS_DIR), str(_PLUGINS_PARENT)):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------------------
# Mock LoreClient
# ---------------------------------------------------------------------------


class MockLoreClient:
    """In-memory fake of LoreClient for tests.

    Records calls and returns programmable search results so dedup,
    prefetch, and turn-capture logic can be exercised without network I/O.
    """

    def __init__(
        self,
        *,
        available: bool = True,
        search_raises: bool = False,
        supports_batch: bool = True,
    ):
        self._available = available
        # When True, kb_search raises to simulate a transport/HTTP error
        # (as the real LoreClient does via raise_for_status). Exercises the
        # dedup-probe fallback-to-add path.
        self._search_raises = search_raises
        # Mirrors LoreClient.supports_batch_get(): when False, callers using
        # the real client would fall back to concurrent kb_get. The mock's
        # kb_get_batch fans out to kb_get either way, so this just records
        # which path the provider thinks it's on.
        self._supports_batch = supports_batch
        self.added: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        self.batch_calls: list[list[str]] = []
        # search_queue: list of result-lists returned in order, one per
        # kb_search call. If exhausted, returns [].
        self.search_queue: list[list[dict[str, Any]]] = []
        # list_queue: list of entry-lists returned in order, one per kb_list.
        self.list_queue: list[list[dict[str, Any]]] = []
        # kb_id -> content map served by kb_get / kb_get_batch.
        self.content_by_id: dict[str, str] = {}
        self._kb_counter = 0

    def is_available(self) -> bool:
        return self._available

    def kb_search(self, query, *, search_mode="hybrid", topic=None, top_k=5):
        self.search_calls.append(
            {"query": query, "search_mode": search_mode, "topic": topic, "top_k": top_k}
        )
        if self._search_raises:
            raise RuntimeError("simulated Lore transport error")
        if self.search_queue:
            return self.search_queue.pop(0)
        return []

    def kb_add(self, *, topic, title, content, tags=None, author=None):
        self._kb_counter += 1
        kb_id = f"kb_mock{self._kb_counter:04d}"
        self.added.append(
            {
                "kb_id": kb_id,
                "topic": topic,
                "title": title,
                "content": content,
                "tags": tags,
                "author": author,
            }
        )
        return {"kb_id": kb_id}

    def kb_update(self, kb_id, *, content=None, title=None, tags=None):
        self.updated.append(
            {"kb_id": kb_id, "content": content, "title": title, "tags": tags}
        )
        return {"kb_id": kb_id}

    def kb_get(self, kb_id):
        self.get_calls.append(kb_id)
        return {
            "kb_id": kb_id,
            "content": self.content_by_id.get(kb_id, "full content"),
        }

    def kb_list(self, topic=None, *, limit=100, offset=0):
        self.list_calls.append({"topic": topic, "limit": limit, "offset": offset})
        if self.list_queue:
            return self.list_queue.pop(0)
        return []

    def supports_batch_get(self):
        return self._supports_batch

    def kb_get_batch(self, kb_ids):
        # Record the batch call so tests can assert a SINGLE round-trip was
        # used instead of N sequential kb_get. Returns rows aligned to input
        # order, with None for unknown IDs (mirrors the server contract).
        self.batch_calls.append(list(kb_ids))
        rows: list[dict | None] = []
        for kid in kb_ids:
            content = self.content_by_id.get(kid)
            rows.append(
                {"kb_id": kid, "content": content} if content is not None else None
            )
        return rows


@pytest.fixture
def mock_client():
    return MockLoreClient()


@pytest.fixture
def unavailable_client():
    return MockLoreClient(available=False)


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_turns():
    """A few raw turns as sync_turn would receive them."""
    return [
        (
            "What's my deployment process?",
            "You deploy via the hermes-scheduler cron job on CT 133.",
        ),
        ("Remember that I prefer dark mode.", "Noted — you prefer dark mode."),
    ]


@pytest.fixture
def hybrid_results():
    """Realistic kb_search hybrid-mode result list (rrf_score present)."""
    return [
        {
            "kb_id": "kb_aaa111",
            "title": "Deploy process",
            "topic": "hermes-conversations",
            "tags": [],
            "author": "hermes",
            "source_type": None,
            "verified": False,
            "score": 8.4,
            "rrf_score": 0.18,
        },
        {
            "kb_id": "kb_bbb222",
            "title": "Dark mode preference",
            "topic": "hermes-conversations",
            "tags": [],
            "author": "hermes",
            "source_type": None,
            "verified": False,
            "score": 6.1,
            "rrf_score": 0.10,
        },
    ]
