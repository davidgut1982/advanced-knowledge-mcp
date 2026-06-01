"""Tests for prefetch formatting and context fencing.

prefetch(query) calls kb_search(hybrid, top_k=5) and formats the top hits
into a compact, fenced block for injection into the system prompt. The
fence markers let sync_turn strip recalled memory before persisting, so
recalled context is never re-stored as a new fact.
"""

from __future__ import annotations

from lore import (
    MEMORY_FENCE_END,
    MEMORY_FENCE_START,
    LoreMemoryProvider,
)


def _provider(mock_client):
    p = LoreMemoryProvider(config={"lore_url": "http://test"})
    p._client = mock_client  # inject mock
    p._session_id = "sess-1"
    return p


def test_prefetch_empty_when_no_results(mock_client):
    mock_client.search_queue = [[]]
    p = _provider(mock_client)
    assert p.prefetch("anything") == ""


def test_prefetch_empty_when_unavailable(unavailable_client):
    p = _provider(unavailable_client)
    assert p.prefetch("anything") == ""


def test_prefetch_empty_for_blank_query(mock_client):
    p = _provider(mock_client)
    assert p.prefetch("") == ""
    # blank query must not even hit the backend
    assert len(mock_client.search_calls) == 0


def test_prefetch_formats_results(mock_client, hybrid_results):
    mock_client.search_queue = [hybrid_results]
    p = _provider(mock_client)
    block = p.prefetch("deploy process")
    # Contains the titles of the recalled entries
    assert "Deploy process" in block
    assert "Dark mode preference" in block


def test_prefetch_wraps_in_fence_markers(mock_client, hybrid_results):
    mock_client.search_queue = [hybrid_results]
    p = _provider(mock_client)
    block = p.prefetch("deploy process")
    assert block.startswith(MEMORY_FENCE_START)
    assert block.rstrip().endswith(MEMORY_FENCE_END)


def test_prefetch_uses_hybrid_top5(mock_client, hybrid_results):
    mock_client.search_queue = [hybrid_results]
    p = _provider(mock_client)
    p.prefetch("deploy process")
    call = mock_client.search_calls[0]
    assert call["search_mode"] == "hybrid"
    assert call["top_k"] == 5
    assert call["query"] == "deploy process"


def test_prefetch_swallows_backend_errors(monkeypatch, mock_client):
    def boom(*a, **k):
        raise RuntimeError("network down")

    mock_client.kb_search = boom
    p = _provider(mock_client)
    # Must degrade to empty string, never raise.
    assert p.prefetch("q") == ""


def test_fence_markers_are_distinct_comment_style():
    # Must be HTML-comment style so they survive prompt assembly and are
    # easy to strip with a regex in sync_turn.
    assert MEMORY_FENCE_START != MEMORY_FENCE_END
    assert "MEMORY" in MEMORY_FENCE_START
    assert MEMORY_FENCE_START.startswith("<!--")
    assert MEMORY_FENCE_END.startswith("<!--")
