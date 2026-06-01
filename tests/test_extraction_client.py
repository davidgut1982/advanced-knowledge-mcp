"""Tests for the extraction client (OpenRouter and Cerebras providers).

Async coroutines are driven via ``asyncio.run()`` inside sync test functions,
matching this repo's convention (no pytest-asyncio collection).
"""

from __future__ import annotations

import asyncio
import json
from unittest import mock

import httpx
import pytest

from lore.extraction.client import ExtractionClient
from lore.extraction.schema import ExtractionResult


class _FakeResponse:
    """Minimal stand-in for httpx.Response."""

    def __init__(self, payload, *, status_code=200, raise_exc=None, text=None):
        self._payload = payload
        self.status_code = status_code
        self._raise_exc = raise_exc
        self._text = text

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        if self._text is not None:
            return json.loads(self._text)  # mimics real json() on bad envelope
        return self._payload


class _FakeAsyncClient:
    """Async context manager capturing the POST it receives."""

    def __init__(self, response, recorder):
        self._response = response
        self._recorder = recorder

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, *, headers=None, json=None):  # noqa: A002
        self._recorder["url"] = url
        self._recorder["headers"] = headers
        self._recorder["body"] = json
        return self._response


def _patch_client(response, recorder):
    def _factory(*args, **kwargs):
        return _FakeAsyncClient(response, recorder)

    return mock.patch.object(httpx, "AsyncClient", _factory)


def _valid_payload(memories_json: str):
    return {"choices": [{"message": {"content": memories_json}}]}


def test_extract_returns_empty_when_no_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    client = ExtractionClient(config={})
    result = asyncio.run(client.extract("[user]: hi\n", {}))
    assert isinstance(result, ExtractionResult)
    assert result.memories == []


def test_extract_parses_valid_response(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    memories = json.dumps(
        {
            "memories": [
                {
                    "type": "preference",
                    "subject": "user",
                    "content": "prefers Python",
                    "confidence": 0.95,
                    "durable": True,
                    "tags": ["lang"],
                }
            ]
        }
    )
    recorder: dict = {}
    response = _FakeResponse(_valid_payload(memories))
    with _patch_client(response, recorder):
        result = asyncio.run(ExtractionClient().extract("[user]: I love Python\n", {}))
    assert len(result.memories) == 1
    cand = result.memories[0]
    assert cand.content == "prefers Python"
    assert cand.confidence == 0.95
    assert cand.tags == ["lang"]


def test_extract_returns_empty_on_http_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    err_response = httpx.Response(429, request=request)
    http_error = httpx.HTTPStatusError("rate limited", request=request, response=err_response)
    response = _FakeResponse(None, status_code=429, raise_exc=http_error)
    with _patch_client(response, {}):
        result = asyncio.run(ExtractionClient().extract("[user]: hi\n", {}))
    assert result.memories == []


def test_extract_returns_empty_on_invalid_json(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # Envelope is valid, but message content is not JSON.
    response = _FakeResponse(_valid_payload("this is not json at all"))
    with _patch_client(response, {}):
        result = asyncio.run(ExtractionClient().extract("[user]: hi\n", {}))
    assert result.memories == []


def test_provider_order_in_request_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    recorder: dict = {}
    response = _FakeResponse(_valid_payload('{"memories": []}'))
    with _patch_client(response, recorder):
        asyncio.run(ExtractionClient().extract("[user]: hi\n", {}))
    assert recorder["body"]["provider"]["order"] == ["Groq", "Together", "Fireworks"]


def test_model_in_request_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    recorder: dict = {}
    response = _FakeResponse(_valid_payload('{"memories": []}'))
    with _patch_client(response, recorder):
        asyncio.run(ExtractionClient().extract("[user]: hi\n", {}))
    assert recorder["body"]["model"] == "meta-llama/llama-3.1-8b-instruct"


def test_temperature_is_low(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    recorder: dict = {}
    response = _FakeResponse(_valid_payload('{"memories": []}'))
    with _patch_client(response, recorder):
        asyncio.run(ExtractionClient().extract("[user]: hi\n", {}))
    assert recorder["body"]["temperature"] <= 0.2


_CEREBRAS_CFG = {"auto_extract": {"provider": "cerebras"}}


def test_cerebras_uses_correct_base_url(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test")
    recorder: dict = {}
    response = _FakeResponse(_valid_payload('{"memories": []}'))
    with _patch_client(response, recorder):
        asyncio.run(ExtractionClient().extract("[user]: hi\n", _CEREBRAS_CFG))
    assert recorder["url"] == "https://api.cerebras.ai/v1/chat/completions"


def test_cerebras_no_provider_routing_field(monkeypatch):
    monkeypatch.setenv("CEREBRAS_API_KEY", "csk-test")
    recorder: dict = {}
    response = _FakeResponse(_valid_payload('{"memories": []}'))
    with _patch_client(response, recorder):
        asyncio.run(ExtractionClient().extract("[user]: hi\n", _CEREBRAS_CFG))
    # The OpenRouter-specific backend routing key must not leak into Cerebras.
    assert "provider" not in recorder["body"]
    assert recorder["body"]["model"] == "gpt-oss-120b"


def test_cerebras_returns_empty_when_no_api_key(monkeypatch):
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    result = asyncio.run(ExtractionClient().extract("[user]: hi\n", _CEREBRAS_CFG))
    assert isinstance(result, ExtractionResult)
    assert result.memories == []


def test_openrouter_still_has_provider_routing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    recorder: dict = {}
    response = _FakeResponse(_valid_payload('{"memories": []}'))
    with _patch_client(response, recorder):
        asyncio.run(
            ExtractionClient().extract("[user]: hi\n", {"auto_extract": {"provider": "openrouter"}})
        )
    assert recorder["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert recorder["body"]["provider"]["order"] == ["Groq", "Together", "Fireworks"]
