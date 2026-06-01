"""Tests for rolling-window mid-session memory extraction.

The baseline auto-extraction fires once at on_session_end and caps to the last
``max_turns`` turns, so for long sessions (100+ turns) the earliest turns are
never extracted. Rolling window fixes this by firing extraction mid-session
every ``rolling_stride`` new turns, with a ``rolling_overlap`` look-back into
already-extracted territory for context.

These tests exercise the trigger/slicing/cursor logic in
LoreMemoryProvider._maybe_roll_extract and the session-end tail handoff in
on_session_end. The actual extraction call (_maybe_fire_extraction, which would
import the LLM-backed pipeline) is replaced with a recorder so the tests need
no real LLM or network — they assert WHICH turns the provider hands to the
extractor, not what the extractor does with them.
"""

from __future__ import annotations

from lore import LoreMemoryProvider


def _provider(
    mock_client,
    *,
    rolling,
    stride=15,
    overlap=5,
    enabled=True,
    session_id="sess-1",
):
    """Build a provider with rolling-window config and a recording spy.

    write_frequency defaults to the provider default ("turn"), which clears
    _captured_turns after each turn — the case rolling window must survive by
    accumulating into _session_turns instead.

    The spy replaces _maybe_fire_extraction so no LLM/extraction pipeline runs.
    Each fire records the (pre-flatten) turn list passed in, so tests can
    assert the slice (and therefore the overlap) precisely.
    """
    auto = {"enabled": enabled}
    if rolling:
        auto["rolling_window"] = True
        auto["rolling_stride"] = stride
        auto["rolling_overlap"] = overlap
    p = LoreMemoryProvider(config={"lore_url": "http://test", "auto_extract": auto})
    p._client = mock_client
    p._session_id = session_id

    fired = []

    def _spy(captured_turns, *, pre_advance=None):
        # Copy so later mutation of _session_turns can't alter recorded slices.
        # Accept (and ignore) pre_advance so the spy matches the real signature.
        fired.append(list(captured_turns))

    p._maybe_fire_extraction = _spy  # type: ignore[method-assign]
    p._fired = fired
    return p


def _add_turns(provider, n, *, start=0):
    """Drive n exchanges through sync_turn (one _session_turns entry each)."""
    for i in range(start, start + n):
        provider.sync_turn(f"user message {i}", f"assistant reply {i}")


# ---------------------------------------------------------------------------
# Rolling disabled / below stride
# ---------------------------------------------------------------------------


def test_rolling_disabled_by_default(mock_client):
    # rolling_window absent from auto_extract -> _maybe_roll_extract never fires.
    p = _provider(mock_client, rolling=False)
    _add_turns(p, 50)
    assert p._fired == []
    assert p._last_extracted_turn == 0


def test_rolling_not_triggered_below_stride(mock_client):
    # 10 turns, stride=15 -> no mid-session extraction.
    p = _provider(mock_client, rolling=True, stride=15)
    _add_turns(p, 10)
    assert p._fired == []
    assert p._last_extracted_turn == 0


# ---------------------------------------------------------------------------
# Rolling triggers at stride
# ---------------------------------------------------------------------------


def test_rolling_triggers_at_stride(mock_client):
    # Exactly stride turns added -> fires once.
    p = _provider(mock_client, rolling=True, stride=15)
    _add_turns(p, 15)
    assert len(p._fired) == 1
    # The fired window still covers every turn 0..14 (trimming happens *after*
    # the window is sliced, so it never drops turns from the fired payload).
    assert len(p._fired[0]) == 15
    assert p._fired[0][0]["user"] == "user message 0"
    assert p._fired[0][-1]["user"] == "user message 14"


def test_rolling_triggers_at_each_stride(mock_client):
    # 30 turns, stride=15 -> fires at turn 15 and again at turn 30. The first
    # fire trims the buffer (keeping `overlap` turns of look-back), so the
    # second fire is driven off the trimmed buffer but still triggers after
    # `stride` fresh turns.
    p = _provider(mock_client, rolling=True, stride=15)
    _add_turns(p, 30)
    assert len(p._fired) == 2


def test_rolling_window_includes_overlap(mock_client):
    # overlap=5: after the first window at turn 15 (cursor -> 15), the second
    # window at turn 30 must start at turn 10 (15 - 5) for context.
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    _add_turns(p, 30)
    second_window = p._fired[1]
    # Window spans turns 10..29 inclusive -> 20 exchanges.
    assert len(second_window) == 20
    # First turn in the window is exchange index 10.
    assert second_window[0]["user"] == "user message 10"
    assert second_window[-1]["user"] == "user message 29"


def test_rolling_cursor_advances_after_trigger(mock_client):
    # After firing at turn 15 the cursor advances to 15, then the post-fire
    # trim drops the 10 turns that fall outside the `overlap`=5 look-back
    # buffer and shifts the cursor left by the same amount: 15 - 10 = 5. The
    # buffer is left holding exactly the `overlap` look-back turns.
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    _add_turns(p, 15)
    assert p._last_extracted_turn == 5
    assert len(p._session_turns) == 5
    # The retained buffer is the look-back tail (turns 10..14).
    assert p._session_turns[0]["user"] == "user message 10"
    assert p._session_turns[-1]["user"] == "user message 14"


# ---------------------------------------------------------------------------
# Session-end tail handoff
# ---------------------------------------------------------------------------


def test_session_end_extracts_remaining_when_rolling(mock_client):
    # rolling on, cursor at 30, total 40 -> session-end pass gets turns 25..39
    # (start = 30 - overlap(5) = 25), i.e. 15 exchanges.
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    # Build 40 turns; the 15-stride fires at 15 and 30. After each fire the
    # buffer is trimmed to the overlap look-back, so the cursor tracks the
    # trimmed buffer rather than the absolute turn count: after the fire at 30
    # the cursor sits at 5 (buffer holds turns 25..29), then 10 more turns
    # (30..39) accumulate without re-firing (10 < stride).
    _add_turns(p, 40)
    fired_before_end = len(p._fired)
    assert p._last_extracted_turn == 5
    assert len(p._session_turns) == 15  # turns 25..39 retained
    p.on_session_end(messages=[])
    # One more fire at session end for the remaining tail.
    assert len(p._fired) == fired_before_end + 1
    tail = p._fired[-1]
    assert len(tail) == 15
    assert tail[0]["user"] == "user message 25"
    assert tail[-1]["user"] == "user message 39"


def test_session_end_extracts_all_when_not_rolling(mock_client):
    # rolling off -> session-end passes the full captured snapshot (existing
    # behavior; filter_turns caps it downstream). With write_frequency "session"
    # the snapshot equals every turn in the session.
    p = LoreMemoryProvider(
        config={
            "lore_url": "http://test",
            "write_frequency": "session",
            "auto_extract": {"enabled": True},
        }
    )
    p._client = mock_client
    p._session_id = "sess-1"
    fired = []

    def _spy(captured_turns, *, pre_advance=None):
        # Match the real _maybe_fire_extraction signature (keyword-only
        # pre_advance) so the assignment type-checks.
        fired.append(list(captured_turns))

    p._maybe_fire_extraction = _spy  # type: ignore[method-assign]
    _add_turns(p, 8)
    # No mid-session fires when rolling is off.
    assert fired == []
    p.on_session_end(messages=[])
    assert len(fired) == 1
    # Full snapshot handed over (all 8 exchanges).
    assert len(fired[0]) == 8
    assert fired[0][0]["user"] == "user message 0"
    assert fired[0][-1]["user"] == "user message 7"


# ---------------------------------------------------------------------------
# Cursor reset on new session
# ---------------------------------------------------------------------------


def test_rolling_cursor_resets_on_new_session(mock_client):
    # After on_session_end the rolling accumulators reset for the next session.
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    _add_turns(p, 30)
    # Trimming bounds the buffer: after the fire at 30 the cursor and buffer
    # track only the overlap look-back (5 turns: 25..29), not all 30 turns.
    assert p._last_extracted_turn == 5
    assert len(p._session_turns) == 5
    p.on_session_end(messages=[])
    assert p._last_extracted_turn == 0
    assert p._session_turns == []


def test_rolling_cursor_resets_on_session_switch(mock_client):
    # on_session_switch also resets rolling state (mid-stream session change).
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    _add_turns(p, 20)
    # One fire at 15; post-fire trim leaves the cursor at the overlap look-back
    # (5), and the 5 extra turns (15..19) don't reach another stride.
    assert p._last_extracted_turn == 5
    p.on_session_switch("sess-2")
    assert p._last_extracted_turn == 0
    assert p._session_turns == []


# ---------------------------------------------------------------------------
# Memory is bounded (Fix 2: trim _session_turns)
# ---------------------------------------------------------------------------


def test_session_turns_bounded_over_long_session(mock_client):
    # Over a long session the buffer must NOT grow without bound: after each
    # mid-session fire it is trimmed to roughly stride + overlap turns. Assert
    # the bound after EVERY turn (not just at the end) so a transient overshoot
    # between fires can't slip through.
    stride, overlap = 15, 5
    p = _provider(mock_client, rolling=True, stride=stride, overlap=overlap)
    for i in range(200):
        # sync_turn takes (user_content, assistant_content); one call appends a
        # single _session_turns exchange, mirroring _add_turns.
        p.sync_turn(f"user message {i}" * 5, f"assistant reply {i}")
        assert len(p._session_turns) <= stride + overlap, f"buffer too large after turn {i}"
    # And the cursor stays within the (now small) buffer — never an absolute
    # 200-turn offset.
    assert p._last_extracted_turn <= len(p._session_turns)


# ---------------------------------------------------------------------------
# Cursor rollback on extraction failure (Fix 1)
# ---------------------------------------------------------------------------


def _install_failing_extractor(monkeypatch):
    """Inject a fake lore.extraction whose extract_and_store always raises.

    The provider imports ``from lore.extraction import extract_and_store``
    lazily inside _maybe_fire_extraction, so the fake must live in sys.modules
    before that call.
    """
    import sys
    import types

    async def _boom(*, turns, db_client, config):
        raise RuntimeError("simulated extraction failure")

    fake = types.ModuleType("lore.extraction")
    fake.extract_and_store = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lore.extraction", fake)


def test_cursor_rolls_back_on_sync_extraction_failure(mock_client, monkeypatch):
    # Synchronous path (no running loop): a failing extraction must roll the
    # cursor back to its pre-advance value so the covered turns are retried,
    # not permanently skipped.
    _install_failing_extractor(monkeypatch)
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    # Use the REAL _maybe_fire_extraction (the _provider spy replaced it).
    p._maybe_fire_extraction = type(p)._maybe_fire_extraction.__get__(p)
    # Drive exactly one stride. The fire happens synchronously (asyncio.run),
    # raises, and the cursor rolls back to the trimmed pre-advance value (0).
    _add_turns(p, 15)
    assert p._last_extracted_turn == 0


def test_cursor_rolls_back_on_async_extraction_failure(mock_client, monkeypatch):
    # Async path (running loop): the failing task's done-callback rolls the
    # cursor back. Driving sync_turn from inside a running loop exercises the
    # create_task branch. Rather than relying on a fixed number of
    # ``asyncio.sleep(0)`` ticks (fragile — the callback may not have fired yet),
    # capture the extraction task as it is created and deterministically await it.
    import asyncio

    _install_failing_extractor(monkeypatch)
    p = _provider(mock_client, rolling=True, stride=15, overlap=5)
    p._maybe_fire_extraction = type(p)._maybe_fire_extraction.__get__(p)

    done = asyncio.Event()
    created: list[asyncio.Task] = []

    async def _drive():
        loop = asyncio.get_running_loop()
        orig_create_task = loop.create_task

        def _capturing_create_task(coro, **kwargs):  # type: ignore[no-untyped-def]
            task = orig_create_task(coro, **kwargs)
            created.append(task)
            # Fire AFTER the provider's own done-callback so the rollback has
            # already run by the time we stop waiting.
            task.add_done_callback(lambda _t: done.set())
            return task

        monkeypatch.setattr(loop, "create_task", _capturing_create_task)

        _add_turns(p, 15)
        # The extraction task must have been created on the running loop.
        assert created, "expected an async extraction task to be created"
        # Block until the task (and thus the rollback done-callback) has fired.
        await done.wait()

    asyncio.run(_drive())
    # Rolled back to the (trimmed) pre-advance value.
    assert p._last_extracted_turn == 0
