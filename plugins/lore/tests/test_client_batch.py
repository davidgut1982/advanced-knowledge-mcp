"""Tests for LoreClient batch/feature-detection behavior.

The deployed Lore server may or may not expose kb_get_batch. The client
probes tools/list once (supports_batch_get), uses the batch tool when present,
and otherwise falls back to CONCURRENT kb_get via asyncio.gather. These tests
stub _call_tool / _list_tool_names so no network is touched.
"""

from __future__ import annotations

from lore.lore_client import LoreClient


def _client():
    return LoreClient("http://test", timeout=1.0)


# ---------------------------------------------------------------------------
# Feature detection (cached)
# ---------------------------------------------------------------------------


def test_supports_batch_true_when_tool_present(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_list_tool_names", lambda: {"kb_get", "kb_get_batch"})
    assert c.supports_batch_get() is True


def test_supports_batch_false_when_absent(monkeypatch):
    c = _client()
    monkeypatch.setattr(c, "_list_tool_names", lambda: {"kb_get", "kb_search"})
    assert c.supports_batch_get() is False


def test_supports_batch_probe_cached(monkeypatch):
    c = _client()
    calls = {"n": 0}

    def _names():
        calls["n"] += 1
        return {"kb_get_batch"}

    monkeypatch.setattr(c, "_list_tool_names", _names)
    assert c.supports_batch_get() is True
    assert c.supports_batch_get() is True
    assert calls["n"] == 1  # probed once, then cached


def test_supports_batch_false_on_probe_error(monkeypatch):
    c = _client()

    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(c, "_list_tool_names", _boom)
    assert c.supports_batch_get() is False  # degrades to fallback


def test_supports_batch_transient_error_then_success_reprobes(monkeypatch):
    """MEDIUM: a transient probe error must NOT be cached permanently.

    First probe raises (transient blip) -> returns False but does not persist.
    Next probe succeeds -> re-probes and returns the definitive result, which
    is then cached. Otherwise one blip downgrades the whole session to the
    concurrent fallback forever.
    """
    c = _client()
    state = {"n": 0}

    def _names():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("transient network blip")
        return {"kb_get_batch"}

    monkeypatch.setattr(c, "_list_tool_names", _names)
    # First call: transient error -> False, NOT cached.
    assert c.supports_batch_get() is False
    assert c._supports_batch is None  # nothing persisted
    # Second call: re-probes and now succeeds.
    assert c.supports_batch_get() is True
    assert c._supports_batch is True  # definitive result cached
    # Third call: served from cache, no further probe.
    assert c.supports_batch_get() is True
    assert state["n"] == 2  # exactly two probes (1 failed + 1 success)


# ---------------------------------------------------------------------------
# kb_get_batch: server batch path
# ---------------------------------------------------------------------------


def test_kb_get_batch_uses_server_tool_when_supported(monkeypatch):
    c = _client()
    c._supports_batch = True  # skip probe
    seen = {}

    def _call(name, args):
        seen["name"] = name
        seen["args"] = args
        return {"entries": [{"kb_id": "a", "content": "A"}, None], "found": 1}

    monkeypatch.setattr(c, "_call_tool", _call)
    out = c.kb_get_batch(["a", "b"])
    assert seen["name"] == "kb_get_batch"
    assert seen["args"] == {"kb_ids": ["a", "b"]}
    assert out[0]["content"] == "A"
    assert out[1] is None


def test_kb_get_batch_rekeys_reordered_and_missing_server_response(monkeypatch):
    """HIGH: result must map by kb_id, robust to server reorder/omission.

    A naive positional ``zip(kb_ids, entries)`` would mis-map content to the
    wrong id/title if the server returns entries out of order or omits misses.
    The client re-keys by kb_id, so the returned list is always aligned with
    the requested ``kb_ids`` regardless of server ordering.
    """
    c = _client()
    c._supports_batch = True

    def _call(name, args):
        # Server returns entries REORDERED ("c" before "a") and OMITS the
        # missing id "b" entirely (found-entries-only contract).
        return {
            "entries": [
                {"kb_id": "c", "content": "C-body"},
                {"kb_id": "a", "content": "A-body"},
            ],
            "found": 2,
        }

    monkeypatch.setattr(c, "_call_tool", _call)
    out = c.kb_get_batch(["a", "b", "c"])
    # Output aligned positionally with the REQUESTED ids, not the server order.
    assert out[0]["kb_id"] == "a" and out[0]["content"] == "A-body"
    assert out[1] is None  # "b" omitted by server -> None at its position
    assert out[2]["kb_id"] == "c" and out[2]["content"] == "C-body"


def test_kb_get_batch_empty_short_circuits(monkeypatch):
    c = _client()
    # Should not probe or call anything for an empty list.
    monkeypatch.setattr(
        c, "_call_tool", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called"))
    )
    assert c.kb_get_batch([]) == []


# ---------------------------------------------------------------------------
# kb_get_batch: concurrent fallback path
# ---------------------------------------------------------------------------


def test_kb_get_batch_falls_back_to_concurrent_kb_get(monkeypatch):
    c = _client()
    c._supports_batch = False  # force fallback
    got: list[str] = []

    def _kb_get(kb_id):
        got.append(kb_id)
        return {"kb_id": kb_id, "content": f"body-{kb_id}"}

    monkeypatch.setattr(c, "kb_get", _kb_get)
    out = c.kb_get_batch(["x", "y", "z"])
    # All ids fetched, results aligned by input order.
    assert {r["kb_id"] for r in out} == {"x", "y", "z"}
    assert [r["kb_id"] for r in out] == ["x", "y", "z"]
    assert sorted(got) == ["x", "y", "z"]


def test_concurrent_fallback_maps_failures_to_none(monkeypatch):
    c = _client()
    c._supports_batch = False

    def _kb_get(kb_id):
        if kb_id == "bad":
            raise RuntimeError("not found")
        return {"kb_id": kb_id, "content": "ok"}

    monkeypatch.setattr(c, "kb_get", _kb_get)
    out = c.kb_get_batch(["good", "bad"])
    assert out[0]["content"] == "ok"
    assert out[1] is None  # failed id -> None, position preserved


def test_kb_get_batch_unexpected_shape_falls_back(monkeypatch):
    c = _client()
    c._supports_batch = True

    def _call(name, args):
        return {"unexpected": True}  # no 'entries' list

    monkeypatch.setattr(c, "_call_tool", _call)
    monkeypatch.setattr(c, "kb_get", lambda kid: {"kb_id": kid, "content": "fb"})
    out = c.kb_get_batch(["a"])
    assert out[0]["content"] == "fb"  # fell back to concurrent kb_get
