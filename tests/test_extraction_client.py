"""Unit tests for ``ExtractionClient`` JSON-parsing resilience.

These exercise the parse/validation path directly (no network). Following the
project convention (no ``pytest-asyncio``), async entry points are driven via
``asyncio.run`` inside ordinary sync test functions.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest import mock

from lore.extraction.client import ExtractionClient


def _envelope(memories: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap a memories list in an OpenAI-style chat-completions envelope."""
    return {"choices": [{"message": {"content": json.dumps({"memories": memories})}}]}


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response`` used by ExtractionClient.extract."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:  # no-op: always 200
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _patch_post(payload: dict[str, Any]):
    """Patch ``httpx.AsyncClient.post`` to return a fixed golden envelope."""
    return mock.patch(
        "httpx.AsyncClient.post",
        new=mock.AsyncMock(return_value=_FakeResponse(payload)),
    )


def _config() -> dict[str, Any]:
    return {
        "auto_extract": {"enabled": True, "provider": "openrouter"},
        "openrouter_api_key": "test-key",
    }


def _extract(payload: dict[str, Any]):
    client = ExtractionClient(_config())
    with _patch_post(payload):
        return asyncio.run(client.extract("hello"))


def test_all_valid_candidates_returned():
    result = _extract(
        _envelope(
            [
                {"type": "user_fact", "content": "is a backend engineer", "confidence": 0.9},
                {"type": "preference", "content": "prefers NVMe", "confidence": 0.85},
            ]
        )
    )
    assert len(result.memories) == 2


def test_invalid_type_drops_only_that_candidate():
    """One bad enum (`challenge`) must not discard the valid candidate."""
    result = _extract(
        _envelope(
            [
                {"type": "user_fact", "content": "is a backend engineer", "confidence": 0.9},
                {"type": "challenge", "content": "this type is invalid", "confidence": 0.9},
            ]
        )
    )
    assert len(result.memories) == 1
    assert result.memories[0].content == "is a backend engineer"
    assert result.memories[0].type == "user_fact"


def test_empty_memories_list_returns_empty_result():
    result = _extract(_envelope([]))
    assert result.memories == []


def test_invalid_envelope_shape_returns_empty():
    result = _extract({"unexpected": "shape"})
    assert result.memories == []
