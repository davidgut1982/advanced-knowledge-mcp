"""Tests for turn filtering in the memory extraction pipeline."""

from __future__ import annotations

import asyncio
from unittest import mock

from lore.extraction import extract_and_store
from lore.extraction.filters import DEFAULT_MAX_TURN_CHARS, filter_turns
from lore.extraction.schema import ExtractionResult


def _turn(content, role="user"):
    return {"role": role, "content": content}


def test_filter_caps_to_max_turns():
    turns = [_turn(f"This is meaningful message number {i}.") for i in range(30)]
    out = filter_turns(turns, max_turns=20)
    assert len(out) == 20


def test_filter_keeps_all_when_under_cap():
    turns = [_turn(f"This is meaningful message number {i}.") for i in range(10)]
    out = filter_turns(turns, max_turns=20)
    assert len(out) == 10


def test_filter_strips_code_fences():
    turns = [_turn("Here is some code:\n```python\nprint('hello world')\n```")]
    out = filter_turns(turns, min_turn_chars=1)
    assert len(out) == 1
    assert "```" not in out[0]["content"]
    assert "[code]" in out[0]["content"]
    assert "print('hello world')" not in out[0]["content"]


def test_filter_skips_short_turns():
    turns = [_turn("ok")]
    out = filter_turns(turns)
    assert out == []


def test_filter_keeps_meaningful_short_turn():
    # "I use Python." is 13 chars, below the 20-char default -> dropped.
    # "I prefer Python over JavaScript." is 32 chars -> kept.
    turns = [
        _turn("I use Python."),
        _turn("I prefer Python over JavaScript."),
    ]
    out = filter_turns(turns)
    assert len(out) == 1
    assert out[0]["content"] == "I prefer Python over JavaScript."


def test_filter_truncates_long_turns():
    long_content = "a" * 2000
    turns = [_turn(long_content)]
    out = filter_turns(turns)
    assert len(out) == 1
    content = out[0]["content"]
    # Truncated to max_turn_chars plus the ellipsis marker.
    assert content.endswith("…")
    assert len(content) == DEFAULT_MAX_TURN_CHARS + 1


def test_filter_handles_list_content():
    turns = [
        {
            "role": "assistant",
            "content": [
                {"text": "The user prefers concise answers"},
                {"text": "and dark mode interfaces always."},
            ],
        }
    ]
    out = filter_turns(turns)
    assert len(out) == 1
    assert "concise answers" in out[0]["content"]
    assert "dark mode interfaces" in out[0]["content"]


def test_filter_strips_json_lines():
    # The _is_json_line heuristic strips lines whose punctuation density
    # exceeds 60% (typical of compact tool-output / JSON blobs), while
    # leaving prose intact.
    turns = [
        _turn(
            "Here is the configuration we discussed:\n"
            '{"a":1,"b":[2,3],"c":{"d":4}}\n'
            "Let me know if that looks right to you."
        )
    ]
    out = filter_turns(turns, min_turn_chars=1)
    assert len(out) == 1
    content = out[0]["content"]
    assert '{"a"' not in content
    assert "Here is the configuration we discussed:" in content
    assert "Let me know if that looks right to you." in content


def test_filter_empty_after_cleaning_returns_empty_list():
    turns = [
        _turn("```python\nx = 1\n```"),
        _turn("```js\nconsole.log(2)\n```"),
    ]
    out = filter_turns(turns)
    assert out == []


def test_filter_takes_last_n_not_first_n():
    turns = [_turn(f"This is meaningful message number {i}.") for i in range(1, 26)]
    out = filter_turns(turns, max_turns=5)
    assert len(out) == 5
    # Should be turns 21-25, not 1-5.
    assert out[0]["content"] == "This is meaningful message number 21."
    assert out[-1]["content"] == "This is meaningful message number 25."


def test_extract_and_store_respects_max_turns():
    """filter_turns is invoked with config-supplied values before extraction."""
    db = mock.Mock()
    config = {
        "auto_extract": {
            "enabled": True,
            "min_turns": 3,
            "max_turns": 7,
            "min_turn_chars": 15,
            "max_turn_chars": 500,
        }
    }
    turns = [_turn(f"Meaningful conversation message {i}.") for i in range(10)]

    with mock.patch(
        "lore.extraction.filter_turns",
        wraps=filter_turns,
    ) as patched_filter, mock.patch(
        "lore.extraction.ExtractionClient.extract",
        new=mock.AsyncMock(return_value=ExtractionResult()),
    ):
        summary = asyncio.run(extract_and_store(turns, db, config))

    patched_filter.assert_called_once()
    _, kwargs = patched_filter.call_args
    assert kwargs["max_turns"] == 7
    assert kwargs["min_turn_chars"] == 15
    assert kwargs["max_turn_chars"] == 500
    assert summary["extracted"] == 0
