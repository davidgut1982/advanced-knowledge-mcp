"""Tests for turn capture, context fencing, and on_session_end persistence.

sync_turn captures raw structured turns (timestamp, session_id, user,
assistant) WITHOUT calling any LLM and WITHOUT storing injected memory
context. Anything between the fence markers is stripped first. on_session_end
flushes the captured turns to Lore under topic 'hermes-conversations'.
"""

from __future__ import annotations

from lore import (
    MEMORY_FENCE_END,
    MEMORY_FENCE_START,
    LoreMemoryProvider,
    strip_memory_fence,
)


def _provider(mock_client, session_id="sess-1", write_frequency="session"):
    # Default these tests to write_frequency "session" so turns buffer until
    # on_session_end; per-turn flush behavior is covered separately below.
    p = LoreMemoryProvider(config={"lore_url": "http://test", "write_frequency": write_frequency})
    p._client = mock_client
    p._session_id = session_id
    return p


# ---------------------------------------------------------------------------
# strip_memory_fence
# ---------------------------------------------------------------------------


def test_strip_removes_fenced_block():
    text = (
        "Here is context.\n"
        f"{MEMORY_FENCE_START}\nrecalled secret\n{MEMORY_FENCE_END}\n"
        "Real user question."
    )
    out = strip_memory_fence(text)
    assert "recalled secret" not in out
    assert "Real user question." in out
    assert MEMORY_FENCE_START not in out
    assert MEMORY_FENCE_END not in out


def test_strip_handles_no_fence():
    text = "Plain user content with no fence."
    assert strip_memory_fence(text) == text


def test_strip_handles_multiple_fences():
    text = f"{MEMORY_FENCE_START}a{MEMORY_FENCE_END}keep{MEMORY_FENCE_START}b{MEMORY_FENCE_END}"
    out = strip_memory_fence(text)
    assert out.strip() == "keep"


# ---------------------------------------------------------------------------
# sync_turn capture shape
# ---------------------------------------------------------------------------


def test_sync_turn_captures_structured_turn(mock_client):
    p = _provider(mock_client)
    p.sync_turn("user says hi", "assistant says hello")
    assert len(p._captured_turns) == 1
    turn = p._captured_turns[0]
    assert turn["user"] == "user says hi"
    assert turn["assistant"] == "assistant says hello"
    assert turn["session_id"] == "sess-1"
    assert "timestamp" in turn and isinstance(turn["timestamp"], str)


def test_sync_turn_does_not_call_backend(mock_client):
    p = _provider(mock_client)
    p.sync_turn("u", "a")
    # No LLM, no kb_add/kb_search during capture
    assert len(mock_client.added) == 0
    assert len(mock_client.search_calls) == 0


def test_sync_turn_strips_injected_memory(mock_client):
    p = _provider(mock_client)
    fenced_user = (
        f"{MEMORY_FENCE_START}\nRecalled: you prefer dark mode\n{MEMORY_FENCE_END}\n"
        "What time is it?"
    )
    p.sync_turn(fenced_user, "It's noon.")
    turn = p._captured_turns[0]
    assert "Recalled: you prefer dark mode" not in turn["user"]
    assert "What time is it?" in turn["user"]


def test_sync_turn_ignores_empty_turns(mock_client):
    p = _provider(mock_client)
    p.sync_turn("", "")
    p.sync_turn("   ", "   ")
    assert len(p._captured_turns) == 0


# ---------------------------------------------------------------------------
# on_session_end persistence
# ---------------------------------------------------------------------------


def test_on_session_end_persists_turns(mock_client):
    mock_client.search_queue = [[]]  # dedup probe -> no dup -> add
    p = _provider(mock_client)
    p.sync_turn("hello", "hi there")
    p.sync_turn("bye", "goodbye")
    p.on_session_end(messages=[])
    assert len(mock_client.added) == 1
    entry = mock_client.added[0]
    assert entry["topic"] == "hermes-conversations"
    assert "sess-1" in entry["title"]
    # Captured turn content is present in the stored body
    assert "hello" in entry["content"]
    assert "goodbye" in entry["content"]


def test_on_session_end_noop_when_no_turns(mock_client):
    p = _provider(mock_client)
    p.on_session_end(messages=[])
    assert len(mock_client.added) == 0
    assert len(mock_client.updated) == 0


def test_on_session_end_clears_buffer(mock_client):
    mock_client.search_queue = [[]]
    p = _provider(mock_client)
    p.sync_turn("u", "a")
    p.on_session_end(messages=[])
    assert p._captured_turns == []


def test_on_session_end_does_not_call_llm(mock_client):
    # Provider must never invoke an LLM; only kb_* calls are allowed.
    mock_client.search_queue = [[]]
    p = _provider(mock_client)
    p.sync_turn("u", "a")
    # No llm attribute / no llm call hook exists on the mock; success =
    # only kb_add/kb_search were used.
    p.on_session_end(messages=[])
    assert len(mock_client.added) == 1


def test_on_session_end_swallows_persist_exception(mock_client):
    # HIGH #1: on_session_end must NOT propagate exceptions into the agent.
    # If _persist_turns raises (e.g. Lore returns HTTP 5xx after the health
    # check passed), on_session_end should swallow it and still clear the
    # buffer, never raising into the Hermes agent process.
    p = _provider(mock_client)
    p.sync_turn("u", "a")

    def _boom() -> None:
        raise RuntimeError("simulated Lore 5xx during flush")

    p._persist_turns = _boom  # type: ignore[method-assign]

    # Must not raise.
    p.on_session_end(messages=[])

    # Buffer is cleared even though persistence failed.
    assert p._captured_turns == []


# ---------------------------------------------------------------------------
# write_frequency: "turn" — per-turn persistence (HIGH #2)
# ---------------------------------------------------------------------------


def test_write_frequency_turn_persists_each_turn(mock_client):
    # With write_frequency "turn", each sync_turn flushes immediately.
    # No dedup hit -> each flush adds a new entry.
    mock_client.search_queue = [[], []]  # one empty probe per flush
    p = _provider(mock_client, write_frequency="turn")
    p.sync_turn("hello", "hi there")
    assert len(mock_client.added) == 1
    assert p._captured_turns == []  # buffer cleared after per-turn flush
    p.sync_turn("bye", "goodbye")
    assert len(mock_client.added) == 2
    assert p._captured_turns == []


def test_write_frequency_turn_is_provider_default(mock_client):
    # The provider default (no explicit write_frequency in config) is "turn",
    # matching the advertised config schema / module docstring.
    mock_client.search_queue = [[]]
    p = LoreMemoryProvider(config={"lore_url": "http://test"})
    p._client = mock_client
    p._session_id = "sess-1"
    assert p._write_frequency == "turn"
    p.sync_turn("u", "a")
    assert len(mock_client.added) == 1
    assert p._captured_turns == []


def test_write_frequency_turn_does_not_double_persist_at_session_end(mock_client):
    # After per-turn flushes the buffer is empty, so on_session_end is a no-op
    # (no duplicate persistence of already-flushed turns).
    mock_client.search_queue = [[], []]
    p = _provider(mock_client, write_frequency="turn")
    p.sync_turn("hello", "hi there")
    p.sync_turn("bye", "goodbye")
    added_before = len(mock_client.added)
    p.on_session_end(messages=[])
    assert len(mock_client.added) == added_before  # no extra add


def test_write_frequency_turn_swallows_flush_exception(mock_client):
    # Per-turn flush must never raise into the agent either.
    p = _provider(mock_client, write_frequency="turn")

    def _boom() -> None:
        raise RuntimeError("simulated Lore 5xx during per-turn flush")

    p._persist_turns = _boom  # type: ignore[method-assign]

    # Must not raise.
    p.sync_turn("u", "a")

    # Buffer cleared despite failure.
    assert p._captured_turns == []


def test_write_frequency_session_buffers_until_session_end(mock_client):
    # With write_frequency "session", turns buffer and only flush at end.
    mock_client.search_queue = [[]]
    p = _provider(mock_client, write_frequency="session")
    p.sync_turn("hello", "hi there")
    p.sync_turn("bye", "goodbye")
    assert len(mock_client.added) == 0  # nothing persisted yet
    assert len(p._captured_turns) == 2
    p.on_session_end(messages=[])
    assert len(mock_client.added) == 1  # single combined entry
