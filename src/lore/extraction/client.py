"""Memory extraction client (OpenRouter or Cerebras).

Sends conversation text to a small, fast instruct model and parses the
structured JSON response into an
:class:`~lore.extraction.schema.ExtractionResult`.

Two OpenAI-compatible providers are supported, selected via the
``provider`` config key:

* ``openrouter`` (default) — Llama 3.1 8B routed through Groq/Together/Fireworks.
* ``cerebras`` — gpt-oss-120b on Cerebras Cloud (300+ TPS, 91-99% prompt cache).

Extraction is *best-effort*: every failure mode (missing API key, HTTP error,
malformed JSON, unknown provider) is logged and converted to an empty result.
``extract`` never raises, so a flaky extraction call can never break a Hermes
session teardown.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from pydantic import ValidationError

from .prompts import EXTRACTION_PROMPT_V1
from .schema import ExtractionResult, MemoryCandidate

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
DEFAULT_PROVIDER = "openrouter"
DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct"
CEREBRAS_DEFAULT_MODEL = "gpt-oss-120b"
DEFAULT_PROVIDER_ORDER = ["Groq", "Together", "Fireworks"]
# Llama 3.1 8B is the largest model this pipeline uses on purpose — extraction
# must stay fast and cheap. Do not raise this to a reasoning/larger model.
_REQUEST_TIMEOUT = 30.0


class ExtractionClient:
    """Thin async wrapper over an OpenAI-compatible chat-completions endpoint.

    Dispatches to OpenRouter (default) or Cerebras based on the ``provider``
    config key.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self._config = config or {}

    @staticmethod
    def _provider(config: dict[str, Any]) -> str:
        auto = config.get("auto_extract", {}) if config else {}
        provider = auto.get("provider") or config.get("provider") or DEFAULT_PROVIDER
        return str(provider).lower()

    def _build_messages(self, conversation_text: str) -> list[dict[str, str]]:
        user_content = (
            f"<conversation>\n{conversation_text}\n</conversation>\n\nExtract memories as JSON."
        )
        return [
            {"role": "system", "content": EXTRACTION_PROMPT_V1},
            {"role": "user", "content": user_content},
        ]

    def _build_request(
        self, conversation_text: str, config: dict[str, Any]
    ) -> tuple[str, dict[str, str], dict[str, Any]] | None:
        """Resolve (url, headers, body) for the configured provider.

        Returns ``None`` when the provider is unknown or its API key is missing;
        the caller logs a warning and returns an empty result.
        """
        auto = config.get("auto_extract", {}) if config else {}
        provider = self._provider(config)
        messages = self._build_messages(conversation_text)

        if provider == "cerebras":
            api_key = os.getenv("CEREBRAS_API_KEY") or config.get("cerebras_api_key")
            if not api_key:
                logger.warning(
                    "CEREBRAS_API_KEY not set; skipping memory extraction (returning empty result)."
                )
                return None
            model = auto.get("model") or config.get("model") or CEREBRAS_DEFAULT_MODEL
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            body = {
                "model": model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 1024,
            }
            return CEREBRAS_URL, headers, body

        if provider == "openrouter":
            api_key = os.getenv("OPENROUTER_API_KEY") or config.get("openrouter_api_key")
            if not api_key:
                logger.warning(
                    "OPENROUTER_API_KEY not set; skipping memory extraction "
                    "(returning empty result)."
                )
                return None
            model = auto.get("model") or config.get("model") or DEFAULT_MODEL
            provider_order = (
                auto.get("provider_order") or config.get("provider_order") or DEFAULT_PROVIDER_ORDER
            )
            headers = {
                "Authorization": f"Bearer {api_key}",
                "X-Title": "Lore Memory Extraction",
                "Content-Type": "application/json",
            }
            body = {
                "model": model,
                "provider": {"order": list(provider_order)},
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_tokens": 1024,
            }
            return OPENROUTER_URL, headers, body

        logger.warning(
            "Unknown extraction provider %r; skipping memory extraction (returning empty result).",
            provider,
        )
        return None

    async def extract(
        self, conversation_text: str, config: dict[str, Any] | None = None
    ) -> ExtractionResult:
        """Extract memory candidates from ``conversation_text``.

        Returns an empty :class:`ExtractionResult` on any failure. Never raises.
        """
        cfg = config if config is not None else self._config
        request = self._build_request(conversation_text, cfg)
        if request is None:
            return ExtractionResult()
        url, headers, body = request

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Memory extraction HTTP error %s: %s",
                exc.response.status_code,
                exc,
            )
            return ExtractionResult()
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers response.json() decode failures on the envelope.
            logger.warning("Memory extraction request failed: %s", exc)
            return ExtractionResult()

        return self._parse_payload(payload)

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> ExtractionResult:
        """Pull the JSON content out of the chat envelope and validate it."""
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("Unexpected extraction response shape: %s", exc)
            return ExtractionResult()

        try:
            data = json.loads(content) if isinstance(content, str) else content
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Extraction response was not valid JSON: %s", exc)
            return ExtractionResult()

        # Partial-parse fallback: validate each candidate independently so a
        # single bad enum/schema (e.g. an LLM hallucinating type="challenge")
        # drops only that candidate instead of discarding the whole batch.
        if not isinstance(data, dict):
            logger.warning("Extraction JSON was not an object: %r", data)
            return ExtractionResult()
        raw_memories = data.get("memories", [])
        if not isinstance(raw_memories, list):
            logger.warning("Extraction 'memories' field was not a list: %r", raw_memories)
            return ExtractionResult()

        valid_candidates: list[MemoryCandidate] = []
        for candidate in raw_memories:
            try:
                valid_candidates.append(MemoryCandidate.model_validate(candidate))
            except ValidationError:
                logger.debug("Skipping invalid candidate (bad type or schema): %s", candidate)
                continue
        return ExtractionResult(memories=valid_candidates)
