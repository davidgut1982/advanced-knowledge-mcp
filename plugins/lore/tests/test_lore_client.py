"""Tests for LoreClient body parsing (StreamableHTTP / SSE framing).

Lore's /mcp endpoint speaks JSON-RPC over StreamableHTTP. Responses come
back either as plain JSON or SSE-framed (``data: {...}``) bodies, possibly
with progress/heartbeat ``data:`` events preceding the terminal result.
``_parse_mcp_body`` must return the LAST non-empty ``data:`` frame.
"""

from __future__ import annotations

import json

import pytest

from lore.lore_client import LoreClient

_parse = LoreClient._parse_mcp_body


# ---------------------------------------------------------------------------
# Plain JSON bodies
# ---------------------------------------------------------------------------


def test_parses_plain_json_body():
    body = '{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}'
    out = _parse(body)
    assert out["result"]["ok"] is True


def test_parses_plain_json_with_leading_whitespace():
    body = '   \n  {"jsonrpc": "2.0", "id": 1, "result": {}}'
    out = _parse(body)
    assert out["jsonrpc"] == "2.0"


# ---------------------------------------------------------------------------
# SSE-framed bodies
# ---------------------------------------------------------------------------


def test_parses_single_sse_data_frame():
    body = 'event: message\ndata: {"id": 1, "result": {"value": 42}}\n\n'
    out = _parse(body)
    assert out["result"]["value"] == 42


def test_returns_last_data_frame_not_first():
    # MEDIUM #1: a progress/heartbeat frame precedes the terminal result.
    # The parser must return the LAST non-empty data frame (the real result),
    # not the first.
    body = (
        'data: {"id": 1, "result": {"progress": 0.5}}\n\n'
        'data: {"id": 1, "result": {"final": true, "value": "done"}}\n\n'
    )
    out = _parse(body)
    assert out["result"].get("final") is True
    assert out["result"]["value"] == "done"
    assert "progress" not in out["result"]


def test_skips_empty_data_frames():
    # Empty ``data:`` keep-alive lines must be ignored; last real frame wins.
    body = (
        "data:\n\n"
        'data: {"id": 1, "result": {"first": true}}\n\n'
        "data:\n\n"
        'data: {"id": 1, "result": {"last": true}}\n\n'
        "data:   \n\n"
    )
    out = _parse(body)
    assert out["result"].get("last") is True
    assert "first" not in out["result"]


def test_handles_many_heartbeats_before_result():
    frames = [f'data: {{"id": 1, "result": {{"tick": {i}}}}}' for i in range(5)]
    frames.append('data: {"id": 1, "result": {"value": "terminal"}}')
    body = "\n".join(frames) + "\n"
    out = _parse(body)
    assert out["result"]["value"] == "terminal"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_raises_on_unrecognized_body():
    with pytest.raises(RuntimeError, match="Unrecognized MCP response body"):
        _parse("not json and no data frame\nsome other line")


def test_raises_on_data_only_empty_frames():
    # Only empty data frames -> no usable payload -> raise.
    with pytest.raises(RuntimeError, match="Unrecognized MCP response body"):
        _parse("data:\n\ndata:   \n\n")


def test_roundtrip_matches_json_loads():
    payload = {"jsonrpc": "2.0", "id": 7, "result": {"data": {"kb_id": "kb_x"}}}
    body = "data: " + json.dumps(payload) + "\n\n"
    assert _parse(body) == payload
