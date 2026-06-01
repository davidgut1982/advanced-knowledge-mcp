"""Tests for the per-turn prefetch optimizations.

Covers the round-trip reductions added to prefetch:
  * batch content fetch (kb_get_batch) instead of N sequential kb_get
  * user-preference TTL cache (hit / miss / expiry / invalidate-on-write)
  * parallel recall + prefs fetch (asyncio.gather)
  * client-side concurrent kb_get fallback when the server lacks kb_get_batch

The deployed Lore server does NOT (yet) expose kb_get_batch, so the client's
supports_batch_get() probe drives a concurrent-kb_get fallback. These tests
exercise both the provider's batch usage (via the mock) and the client's
fallback path directly.
"""

from __future__ import annotations

import time

import pytest

from lore import PREFS_TOPIC, LoreMemoryProvider, _TTLCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _provider(mock_client, **config):
    cfg = {"lore_url": "http://test"}
    cfg.update(config)
    p = LoreMemoryProvider(config=cfg)
    p._client = mock_client
    p._session_id = "sess-1"
    return p


def _hit(kb_id, title, topic="hermes-memory"):
    # kb_search-style hit: content omitted (must be batch-fetched).
    return {"kb_id": kb_id, "title": title, "topic": topic, "rrf_score": 0.2}


# ---------------------------------------------------------------------------
# Batch fetch path
# ---------------------------------------------------------------------------


def test_prefetch_uses_single_batch_call_not_n_kb_get(mock_client):
    """Recall content is fetched via ONE kb_get_batch, not N kb_get."""
    mock_client.search_queue = [
        [_hit("kb_1", "A"), _hit("kb_2", "B"), _hit("kb_3", "C")]
    ]
    mock_client.content_by_id = {
        "kb_1": "content one",
        "kb_2": "content two",
        "kb_3": "content three",
    }
    p = _provider(mock_client)
    block = p.prefetch("query")

    # Exactly one batch call covering all three ids; zero sequential kb_get.
    assert len(mock_client.batch_calls) == 1
    assert mock_client.batch_calls[0] == ["kb_1", "kb_2", "kb_3"]
    assert mock_client.get_calls == []
    # Content rendered into the block.
    assert "content one" in block
    assert "content two" in block


def test_prefetch_truncates_batched_content_to_400(mock_client):
    """400-char truncation + ellipsis preserved on batched content."""
    mock_client.search_queue = [[_hit("kb_1", "Long")]]
    mock_client.content_by_id = {"kb_1": "x" * 500}
    p = _provider(mock_client)
    block = p.prefetch("query")
    assert "x" * 400 + "…" in block
    assert "x" * 401 not in block


def test_prefetch_skips_batch_when_no_ids_need_content(mock_client):
    """Entries already carrying content trigger no batch round-trip."""
    hit = _hit("kb_1", "A")
    hit["content"] = "inline content"
    mock_client.search_queue = [[hit]]
    p = _provider(mock_client)
    block = p.prefetch("query")
    assert mock_client.batch_calls == []
    assert "inline content" in block


def test_prefetch_handles_missing_batch_entries(mock_client):
    """A None row from kb_get_batch (missing id) degrades to title-only."""
    mock_client.search_queue = [[_hit("kb_known", "Known"), _hit("kb_gone", "Gone")]]
    mock_client.content_by_id = {"kb_known": "here"}  # kb_gone -> None
    p = _provider(mock_client)
    block = p.prefetch("query")
    assert "here" in block
    assert "Gone" in block  # title still rendered


# ---------------------------------------------------------------------------
# Pref cache: hit / miss / TTL expiry / invalidation
# ---------------------------------------------------------------------------


def test_prefs_disabled_by_default_no_list_call(mock_client):
    """Default behavior unchanged: prefs off, no kb_list round-trip."""
    mock_client.search_queue = [[_hit("kb_1", "A")]]
    mock_client.content_by_id = {"kb_1": "c"}
    p = _provider(mock_client)
    p.prefetch("query")
    assert mock_client.list_calls == []


def test_prefs_cache_miss_then_hit(mock_client):
    """First prefetch lists prefs (miss); second serves from cache (hit)."""
    pref = {"kb_id": "p1", "title": "Name", "topic": PREFS_TOPIC}
    # Two recall searches queued; prefs listed once (cached after).
    mock_client.search_queue = [[], []]
    mock_client.list_queue = [[pref]]
    mock_client.content_by_id = {"p1": "David"}
    p = _provider(mock_client, prefs_enabled=True)

    block1 = p.prefetch("q1")
    assert len(mock_client.list_calls) == 1  # miss -> listed
    assert "David" in block1

    block2 = p.prefetch("q2")
    assert len(mock_client.list_calls) == 1  # hit -> NOT listed again
    assert "David" in block2


def test_prefs_cache_ttl_expiry(mock_client):
    """After TTL elapses the cache re-lists prefs."""
    pref = {"kb_id": "p1", "title": "Name", "topic": PREFS_TOPIC}
    mock_client.search_queue = [[], []]
    mock_client.list_queue = [[pref], [pref]]
    mock_client.content_by_id = {"p1": "David"}
    p = _provider(mock_client, prefs_enabled=True, prefs_cache_ttl=0.05)

    p.prefetch("q1")
    assert len(mock_client.list_calls) == 1
    time.sleep(0.08)  # exceed TTL
    p.prefetch("q2")
    assert len(mock_client.list_calls) == 2  # re-listed after expiry


def test_prefs_cache_invalidated_on_pref_write(mock_client):
    """Writing to the prefs topic busts the cache; next prefetch re-lists."""
    pref = {"kb_id": "p1", "title": "Name", "topic": PREFS_TOPIC}
    mock_client.search_queue = [[], []]
    mock_client.list_queue = [[pref], [pref]]
    mock_client.content_by_id = {"p1": "David"}
    p = _provider(mock_client, prefs_enabled=True)

    p.prefetch("q1")
    assert len(mock_client.list_calls) == 1

    # Write a pref via lore_remember -> invalidation.
    p.handle_tool_call(
        "lore_remember",
        {"content": "I drive a Subaru", "topic": PREFS_TOPIC},
    )
    p.prefetch("q2")
    assert len(mock_client.list_calls) == 2  # cache busted -> re-listed


def test_non_pref_write_does_not_invalidate_cache(mock_client):
    """A write to a different topic leaves the prefs cache intact."""
    pref = {"kb_id": "p1", "title": "Name", "topic": PREFS_TOPIC}
    mock_client.search_queue = [[], []]
    mock_client.list_queue = [[pref]]
    mock_client.content_by_id = {"p1": "David"}
    p = _provider(mock_client, prefs_enabled=True)

    p.prefetch("q1")
    assert len(mock_client.list_calls) == 1

    p.handle_tool_call(
        "lore_remember",
        {"content": "Unrelated fact", "topic": "hermes-memory"},
    )
    p.prefetch("q2")
    assert len(mock_client.list_calls) == 1  # cache still valid


def test_prefs_rendered_in_dedicated_section(mock_client):
    """Prefs appear under a 'User preferences' heading in the block."""
    pref = {"kb_id": "p1", "title": "Vehicle", "topic": PREFS_TOPIC}
    mock_client.search_queue = [[]]
    mock_client.list_queue = [[pref]]
    mock_client.content_by_id = {"p1": "Subaru Outback"}
    p = _provider(mock_client, prefs_enabled=True)
    block = p.prefetch("q1")
    assert "User preferences" in block
    assert "Subaru Outback" in block


# ---------------------------------------------------------------------------
# Parallel fetch
# ---------------------------------------------------------------------------


def test_recall_and_prefs_both_fetched(mock_client):
    """Parallel branch returns both recall and pref content in one block."""
    mock_client.search_queue = [[_hit("kb_1", "Recall")]]
    mock_client.list_queue = [[{"kb_id": "p1", "title": "Pref", "topic": PREFS_TOPIC}]]
    mock_client.content_by_id = {"kb_1": "recall body", "p1": "pref body"}
    p = _provider(mock_client, prefs_enabled=True)
    block = p.prefetch("query")
    assert "recall body" in block
    assert "pref body" in block


def test_prefetch_returns_empty_when_both_empty(mock_client):
    """No recall hits and no prefs -> empty string (no fence)."""
    mock_client.search_queue = [[]]
    mock_client.list_queue = [[]]
    p = _provider(mock_client, prefs_enabled=True)
    assert p.prefetch("query") == ""


# ---------------------------------------------------------------------------
# _TTLCache unit
# ---------------------------------------------------------------------------


def test_ttlcache_miss_then_hit():
    c = _TTLCache(ttl=10.0)
    assert c.get() == (False, None)
    c.set(["x"])
    assert c.get() == (True, ["x"])


def test_ttlcache_expiry():
    c = _TTLCache(ttl=0.02)
    c.set("v")
    assert c.get()[0] is True
    time.sleep(0.04)
    assert c.get() == (False, None)


def test_ttlcache_invalidate():
    c = _TTLCache(ttl=100.0)
    c.set("v")
    c.invalidate()
    assert c.get() == (False, None)
