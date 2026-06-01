"""HTTP client for the Lore KB-MCP server + dedup logic.

Lore (http://192.168.1.21:5555) is MCP-only — it exposes no REST API
(``/``, ``/docs``, ``/openapi.json`` all 404; only ``/health`` is 200 and
``/mcp`` speaks JSON-RPC over StreamableHTTP). So this client POSTs
JSON-RPC ``tools/call`` requests to ``/mcp`` directly via httpx, handling
both plain-JSON and SSE-framed (``data: {...}``) responses, and unwraps
Lore's ``{ok, error, message, env, data}`` envelope.

IMPORTANT — parameter and scoring facts verified live against Lore
(2026-05-25):
  * kb_search params are ``query``, ``topic``, ``top_k`` (NOT ``limit``)
    and ``search_mode`` in {"fts","semantic","hybrid"} (NOT ``mode``).
  * Result fields: kb_id, title, topic, tags, author, source_type,
    verified, score, rrf_score. ``content`` is absent in list results.
  * In hybrid mode ``rrf_score`` is present and HIGHER = more similar.
    In semantic mode score/rrf_score are None. So dedup uses hybrid +
    ``rrf_score >= DEDUP_THRESHOLD``.

Batch fetch (kb_get_batch, GitHub #25) collapses the N kb_get round-trips
after a search into one. The deployed server may not expose it yet, so
``supports_batch_get()`` probes tools/list once and ``kb_get_batch()`` falls
back to CONCURRENT kb_get (asyncio.gather over threads) when it's absent —
either way the prefetch latency drops from N serial round-trips to ~1.

This module imports nothing from hermes, so it is independently testable.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Calibrated default. rrf_score for hybrid search lands in (0, ~0.3].
# Live observation: a near-duplicate top hit fuses to rrf_score ~0.15-0.18,
# while unrelated content scores <~0.05. 0.10 cleanly separates the two
# bands with margin on both sides. Overridable via config (dedup_threshold).
DEFAULT_LORE_URL = "http://192.168.1.21:5555"
DEDUP_THRESHOLD = 0.10

_VALID_SEARCH_MODES = ("fts", "semantic", "hybrid")


class LoreClient:
    """Thin MCP-over-HTTP client for Lore's kb_* tools."""

    def __init__(self, base_url: str = DEFAULT_LORE_URL, *, timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # itertools.count() yields a thread-safe atomic increment (the next()
        # is a single C-level GIL-held op), so the concurrent kb_get fallback
        # can share one client across threads without racing the request id.
        self._req_id_counter = itertools.count(1)
        # Lazily-probed cache of whether the remote exposes kb_get_batch.
        # None = not yet probed; set by supports_batch_get().
        self._supports_batch: bool | None = None

    # -- transport -----------------------------------------------------------

    def is_available(self) -> bool:
        """Cheap reachability check against Lore's /health endpoint.

        Network call kept here (not in the provider's is_available, which
        the ABC says must avoid network I/O) — used opportunistically by
        write paths, not during agent init.
        """
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=self.timeout)
            return resp.status_code == 200
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.debug("Lore /health check failed: %s", exc)
            return False

    def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON-RPC tools/call and return the unwrapped ``data`` dict.

        Raises RuntimeError on transport/protocol/tool errors so callers
        can decide whether to degrade.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._req_id_counter),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        resp = httpx.post(
            f"{self.base_url}/mcp", json=payload, headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        envelope = self._parse_mcp_body(resp.text)

        if "error" in envelope and envelope["error"] is not None:
            raise RuntimeError(f"Lore MCP error: {envelope['error']}")

        result = envelope.get("result", {})
        inner = self._extract_structured(result)
        if inner is None:
            raise RuntimeError("Lore returned no structured content")

        if inner.get("ok") is False:
            raise RuntimeError(f"Lore tool error: {inner.get('error')}")
        # Unwrap the {ok, error, message, env, data} envelope.
        return inner.get("data", inner)

    @staticmethod
    def _parse_mcp_body(text: str) -> dict[str, Any]:
        """Parse a StreamableHTTP body: plain JSON or SSE ``data:`` frame.

        Collects all ``data:`` lines and returns the LAST non-empty one.
        Lore may emit progress/heartbeat events before the terminal result;
        the final frame carries the actual tool result.
        """
        text = text.strip()
        if text.startswith("{"):
            return json.loads(text)
        last_data: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    last_data = chunk
        if last_data is not None:
            return json.loads(last_data)
        raise RuntimeError("Unrecognized MCP response body")

    @staticmethod
    def _extract_structured(result: dict[str, Any]) -> dict[str, Any] | None:
        """Pull the structured tool result out of an MCP result block."""
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        for block in result.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except (ValueError, KeyError):
                    continue
        return None

    # -- kb_* tools ----------------------------------------------------------

    def kb_search(
        self,
        query: str,
        *,
        search_mode: str = "hybrid",
        topic: str | None = None,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        if search_mode not in _VALID_SEARCH_MODES:
            search_mode = "hybrid"
        args: dict[str, Any] = {
            "query": query,
            "search_mode": search_mode,
            "top_k": top_k,
        }
        if topic is not None:
            args["topic"] = topic
        data = self._call_tool("kb_search", args)
        results = data.get("results", [])
        return results if isinstance(results, list) else []

    def kb_add(
        self,
        *,
        topic: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
        author: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"topic": topic, "title": title, "content": content}
        if tags is not None:
            args["tags"] = tags
        if author is not None:
            args["author"] = author
        return self._call_tool("kb_add", args)

    def kb_update(
        self,
        kb_id: str,
        *,
        content: str | None = None,
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"kb_id": kb_id}
        if content is not None:
            args["content"] = content
        if title is not None:
            args["title"] = title
        if tags is not None:
            args["tags"] = tags
        return self._call_tool("kb_update", args)

    def kb_get(self, kb_id: str) -> dict[str, Any]:
        return self._call_tool("kb_get", {"kb_id": kb_id})

    def kb_list(
        self,
        topic: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List KB entries (optionally scoped to ``topic``).

        Returns the bare ``entries`` list from Lore's kb_list envelope.
        """
        args: dict[str, Any] = {"limit": limit, "offset": offset}
        if topic is not None:
            args["topic"] = topic
        data = self._call_tool("kb_list", args)
        entries = data.get("entries", data.get("results", []))
        return entries if isinstance(entries, list) else []

    # -- batch / feature detection ------------------------------------------

    def supports_batch_get(self) -> bool:
        """Return True if the *remote* server exposes ``kb_get_batch``.

        Only a DEFINITIVE present/absent result is cached: tools/list is fetched
        at most once per client once it succeeds. On a probe *exception* (e.g. a
        transient network blip) we return False (use the fallback this turn) but
        do NOT persist it, so the next turn re-probes — otherwise one transient
        error would downgrade every later turn to the concurrent fallback for the
        whole session. Used to pick the batch fast-path vs. the kb_get fallback.
        """
        if self._supports_batch is not None:
            return self._supports_batch
        try:
            names = self._list_tool_names()
        except Exception as exc:  # noqa: BLE001 - transient; re-probe next turn
            logger.debug("Lore tools/list probe failed (will re-probe): %s", exc)
            return False  # NOT cached: next call re-probes
        self._supports_batch = "kb_get_batch" in names
        return self._supports_batch

    def _list_tool_names(self) -> set[str]:
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._req_id_counter),
            "method": "tools/list",
            "params": {},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        resp = httpx.post(
            f"{self.base_url}/mcp", json=payload, headers=headers, timeout=self.timeout
        )
        resp.raise_for_status()
        envelope = self._parse_mcp_body(resp.text)
        tools = envelope.get("result", {}).get("tools", [])
        return {t.get("name", "") for t in tools if isinstance(t, dict)}

    def kb_get_batch(self, kb_ids: list[str]) -> list[dict[str, Any] | None]:
        """Fetch full content for many KB entries in one shot.

        Returns a list aligned positionally with ``kb_ids`` (missing IDs become
        ``None``). Uses the server's ``kb_get_batch`` tool when the remote
        exposes it (single round-trip); otherwise falls back to concurrent
        ``kb_get`` calls via asyncio so N fetches overlap instead of running
        serially. Either way the round-trip *latency* collapses to ~1.
        """
        if not kb_ids:
            return []
        if self.supports_batch_get():
            data = self._call_tool("kb_get_batch", {"kb_ids": kb_ids})
            entries = data.get("entries")
            if isinstance(entries, list):
                # Re-key by kb_id rather than trusting positional alignment.
                # The server (#25) documents input-order alignment with None
                # for misses, but we don't rely on that here: if a server
                # version ever returns found-entries-only or reorders, naive
                # positional zip downstream would map content to the WRONG
                # kb_id/title (a memory-integrity bug). Rebuilding the list
                # from {kb_id -> entry} makes us robust to omission/reorder.
                by_id = {
                    e["kb_id"]: e
                    for e in entries
                    if isinstance(e, dict) and e.get("kb_id")
                }
                return [by_id.get(k) for k in kb_ids]
            # Defensive: unexpected shape -> fall through to concurrent path.
            logger.debug("kb_get_batch returned unexpected shape: %s", data)
        return self._kb_get_concurrent(kb_ids)

    def _kb_get_concurrent(self, kb_ids: list[str]) -> list[dict[str, Any] | None]:
        """Concurrent kb_get fallback (overlapping round-trips via gather).

        Runs the blocking httpx ``kb_get`` calls in threads under a fresh
        event loop so the N requests fly in parallel. Failures map to ``None``
        at their position (mirrors kb_get_batch's missing-ID contract).
        """

        async def _gather() -> list[dict[str, Any] | None]:
            async def _one(kid: str) -> dict[str, Any] | None:
                try:
                    return await asyncio.to_thread(self.kb_get, kid)
                except Exception as exc:  # noqa: BLE001 - align as missing
                    logger.debug("concurrent kb_get(%s) failed: %s", kid, exc)
                    return None

            return await asyncio.gather(*(_one(k) for k in kb_ids))

        return asyncio.run(_gather())


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def add_or_update(
    client: Any,
    *,
    topic: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
    author: str | None = None,
    threshold: float = DEDUP_THRESHOLD,
) -> dict[str, Any]:
    """Add ``content`` to Lore, or update a near-duplicate instead.

    Probes Lore with a hybrid search for the content. If the top hit's
    ``rrf_score`` is at or above ``threshold`` (higher = more similar in
    hybrid mode), the existing entry is updated rather than creating a
    near-duplicate. Degrades gracefully: if the dedup probe fails (Lore
    unreachable / transport error), falls back to a plain add.

    No pre-flight ``is_available()`` /health GET: ``_call_tool`` already does
    ``raise_for_status()`` and errors propagate to the caller, so the extra
    round-trip on every write path is unnecessary.

    Returns a dict with ``action`` in {"added", "updated"} and the ``kb_id``.
    """
    try:
        hits = client.kb_search(content, search_mode="hybrid", topic=topic, top_k=3)
    except Exception as exc:  # noqa: BLE001 - dedup probe is best-effort
        logger.debug("Lore dedup probe failed, falling back to add: %s", exc)
        hits = []

    top = hits[0] if hits else None
    rrf = top.get("rrf_score") if isinstance(top, dict) else None

    if top is not None and isinstance(rrf, (int, float)) and rrf >= threshold:
        kb_id = top.get("kb_id")
        client.kb_update(kb_id, content=content, title=title, tags=tags)
        return {"action": "updated", "kb_id": kb_id}

    resp = client.kb_add(
        topic=topic, title=title, content=content, tags=tags, author=author
    )
    return {"action": "added", "kb_id": resp.get("kb_id")}
