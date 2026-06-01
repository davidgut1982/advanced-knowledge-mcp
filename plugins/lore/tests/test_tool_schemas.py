"""Tests for get_tool_schemas(), config schema, save_config, and register().

For Stage 2, the provider exposes a single explicit-storage tool
(lore_remember) plus the required provider plumbing. Tool schemas must
follow the OpenAI function-calling format the Hermes registry expects:
{"name", "description", "parameters": {"type": "object", ...}}.
"""

from __future__ import annotations

import json

import pytest

from lore import LoreMemoryProvider, register


def _provider(mock_client=None):
    p = LoreMemoryProvider(config={"lore_url": "http://test"})
    if mock_client is not None:
        p._client = mock_client
        p._session_id = "sess-1"
    return p


# ---------------------------------------------------------------------------
# Basic provider identity
# ---------------------------------------------------------------------------


def test_provider_name_is_lore():
    assert _provider().name == "lore"


def test_is_available_returns_bool():
    assert isinstance(_provider().is_available(), bool)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------


def test_get_tool_schemas_returns_list():
    schemas = _provider().get_tool_schemas()
    assert isinstance(schemas, list)


def test_tool_schemas_are_valid_function_schemas():
    for schema in _provider().get_tool_schemas():
        assert "name" in schema and isinstance(schema["name"], str)
        assert "description" in schema and isinstance(schema["description"], str)
        params = schema["parameters"]
        assert params["type"] == "object"
        assert "properties" in params
        assert isinstance(params.get("required", []), list)


def test_lore_remember_tool_present():
    names = {s["name"] for s in _provider().get_tool_schemas()}
    assert "lore_remember" in names


def test_lore_remember_requires_content():
    schema = next(s for s in _provider().get_tool_schemas() if s["name"] == "lore_remember")
    assert "content" in schema["parameters"]["required"]


# ---------------------------------------------------------------------------
# handle_tool_call dispatch
# ---------------------------------------------------------------------------


def test_handle_lore_remember_stores(mock_client):
    mock_client.search_queue = [[]]  # no dup -> add
    p = _provider(mock_client)
    out = p.handle_tool_call("lore_remember", {"content": "remember this"})
    data = json.loads(out)
    assert data.get("action") == "added"
    assert len(mock_client.added) == 1


def test_handle_unknown_tool_returns_error(mock_client):
    p = _provider(mock_client)
    out = p.handle_tool_call("nope", {})
    assert "error" in json.loads(out)


def test_handle_lore_remember_missing_content_errors(mock_client):
    p = _provider(mock_client)
    out = p.handle_tool_call("lore_remember", {})
    assert "error" in json.loads(out)


# ---------------------------------------------------------------------------
# Config schema + save_config
# ---------------------------------------------------------------------------


def test_config_schema_exposes_expected_keys():
    keys = {f["key"] for f in _provider().get_config_schema()}
    assert {"recall_mode", "write_frequency", "dedup_threshold", "lore_url"} <= keys


def test_config_schema_defaults():
    by_key = {f["key"]: f for f in _provider().get_config_schema()}
    assert by_key["recall_mode"]["default"] == "hybrid"
    assert by_key["write_frequency"]["default"] == "turn"
    assert by_key["lore_url"]["default"] == "http://192.168.1.21:5555"
    # dedup_threshold default must be the calibrated float, serialized
    assert float(by_key["dedup_threshold"]["default"]) > 0.0


def test_save_config_writes_json(tmp_path):
    p = _provider()
    p.save_config({"recall_mode": "hybrid", "dedup_threshold": "0.07"}, str(tmp_path))
    cfg_file = tmp_path / "plugins" / "lore" / "config.json"
    assert cfg_file.exists()
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["recall_mode"] == "hybrid"
    assert saved["dedup_threshold"] == "0.07"


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self):
        self.registered = []

    def register_memory_provider(self, provider):
        self.registered.append(provider)


def test_register_callable():
    assert callable(register)


def test_register_registers_lore_provider():
    ctx = _Ctx()
    register(ctx)
    assert len(ctx.registered) == 1
    assert ctx.registered[0].name == "lore"
