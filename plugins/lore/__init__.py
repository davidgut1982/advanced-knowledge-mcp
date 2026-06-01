"""lore — Lore memory provider plugin for Nous Hermes.

Backs the Hermes agent (hermes-agent 0.14.0) with Lore (a KB-MCP server at
http://192.168.1.21:5555) as its persistent memory backend. Forked in
structure from the bundled Holographic provider, but talks to a remote
KB-MCP over HTTP instead of a local SQLite store.

Behavior:
  * prefetch(query)      — hybrid kb_search, format top hits into a
                           fence-wrapped recall block for the system prompt.
  * sync_turn(...)       — capture raw structured turns (no LLM call),
                           stripping any injected memory between fence
                           markers so recalled context is never re-stored.
  * on_session_end(...)  — flush captured turns to Lore under topic
                           'hermes-conversations' (the hermes-scheduler job
                           later summarizes them; the provider never calls
                           an LLM itself).
  * lore_remember tool   — explicit user-triggered storage with dedup.

Config (config.yaml plugins.lore, or plugins/lore/config.json):
  recall_mode (default "hybrid"), write_frequency (default "turn"),
  dedup_threshold (default calibrated float), lore_url.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Generic, Optional, TypeVar

_T = TypeVar("_T")

# Hermes interfaces. On CT 133 these resolve to the real hermes-agent
# modules; in local tests they resolve to tests/_hermes_stubs/ (wired by
# conftest.py). The fallback keeps the module importable even if neither is
# present at definition time.
try:  # pragma: no cover - exercised on CT 133 / via stubs
    from agent.memory_provider import MemoryProvider
    from tools.registry import tool_error
except Exception:  # pragma: no cover - last-resort shim
    from abc import ABC as MemoryProvider  # type: ignore[assignment]

    def tool_error(message, **extra) -> str:  # type: ignore[misc]
        result = {"error": str(message)}
        if extra:
            result.update(extra)
        return json.dumps(result, ensure_ascii=False)


from .lore_client import (  # noqa: E402 - after hermes import guard
    DEDUP_THRESHOLD,
    DEFAULT_LORE_URL,
    LoreClient,
    add_or_update,
)

logger = logging.getLogger(__name__)

# Context-fence markers. prefetch() wraps recalled memory in these so
# sync_turn() can strip it before persisting — recalled memory is never
# re-stored as a new "fact". HTML-comment style survives prompt assembly.
MEMORY_FENCE_START = "<!-- MEMORY_INJECT_START -->"
MEMORY_FENCE_END = "<!-- MEMORY_INJECT_END -->"

CONVERSATIONS_TOPIC = "hermes-conversations"

_FENCE_RE = re.compile(
    re.escape(MEMORY_FENCE_START) + r".*?" + re.escape(MEMORY_FENCE_END),
    re.DOTALL,
)


def strip_memory_fence(text: str) -> str:
    """Remove any fenced memory-injection block(s) from ``text``."""
    if not text or MEMORY_FENCE_START not in text:
        return text
    return _FENCE_RE.sub("", text)


# ---------------------------------------------------------------------------
# Tool schema (explicit user-triggered storage)
# ---------------------------------------------------------------------------

LORE_REMEMBER_SCHEMA = {
    "name": "lore_remember",
    "description": (
        "Store a durable memory in Lore (persistent knowledge base). Use "
        "when the user explicitly asks you to remember something, or for a "
        "fact they would expect recalled in a future session. Dedupes "
        "near-identical entries automatically (updates instead of "
        "duplicating)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact or memory to store.",
            },
            "title": {
                "type": "string",
                "description": "Short title for the memory (optional).",
            },
            "topic": {
                "type": "string",
                "description": (
                    "Topic bucket (optional; defaults to a general hermes-memory topic)."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags.",
            },
        },
        "required": ["content"],
    },
}

DEFAULT_MEMORY_TOPIC = "hermes-memory"

# User-preference recall. Prefs (name/location/vehicle/etc.) change rarely,
# so the kb_list + N×kb_get needed to surface them is wasteful to repeat every
# turn. We cache the rendered pref lines in-process with a TTL and bust the
# cache on any write that touches the prefs topic. Disabled by default so the
# baseline prefetch behavior (semantic recall only) is unchanged; flip
# ``prefs_enabled`` in config to surface prefs.
PREFS_TOPIC = "hermes-user-prefs"
PREFS_CACHE_TTL = 300.0  # seconds
PREFS_LIST_LIMIT = 20

# Number of recall hits whose full content is fetched for the prefetch block.
RECALL_TOP_K = 5


class _TTLCache(Generic[_T]):
    """Tiny single-slot TTL cache with explicit invalidation.

    Stores one value (the rendered pref lines) with a monotonic expiry. Used
    instead of functools.lru_cache so we can both honor a TTL *and*
    invalidate-on-write when a prefs entry is added/updated.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._value: _T | None = None
        self._expires_at: float = 0.0
        self._set: bool = False

    def get(self) -> tuple[bool, _T | None]:
        """Return (hit, value). ``hit`` is False on miss or expiry."""
        if not self._set:
            return False, None
        if time.monotonic() >= self._expires_at:
            self._set = False
            self._value = None
            return False, None
        return True, self._value

    def set(self, value: _T) -> None:
        self._value = value
        self._expires_at = time.monotonic() + self._ttl
        self._set = True

    def invalidate(self) -> None:
        self._set = False
        self._value = None
        self._expires_at = 0.0


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _load_plugin_config() -> dict:
    """Load plugins/lore/config.json from HERMES_HOME if present."""
    try:
        from hermes_constants import get_hermes_home

        cfg_file = get_hermes_home() / "plugins" / "lore" / "config.json"
        if cfg_file.exists():
            return json.loads(cfg_file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - missing config is fine
        logger.debug("Lore config load skipped: %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LoreMemoryProvider(MemoryProvider):
    """Memory provider backed by the Lore KB-MCP server."""

    def __init__(self, config: dict | None = None):
        self._config = config or _load_plugin_config()
        self._lore_url = self._config.get("lore_url", DEFAULT_LORE_URL)
        self._recall_mode = self._config.get("recall_mode", "hybrid")
        self._write_frequency = self._config.get("write_frequency", "turn")
        try:
            self._dedup_threshold = float(
                self._config.get("dedup_threshold", DEDUP_THRESHOLD)
            )
        except (TypeError, ValueError):
            self._dedup_threshold = DEDUP_THRESHOLD
        # Pref-cache knobs (config-driven; see plugin.yaml lore namespace).
        self._prefs_enabled = bool(self._config.get("prefs_enabled", False))
        self._prefs_topic = self._config.get("prefs_topic", PREFS_TOPIC)
        try:
            ttl = float(self._config.get("prefs_cache_ttl", PREFS_CACHE_TTL))
        except (TypeError, ValueError):
            ttl = PREFS_CACHE_TTL
        self._prefs_cache: _TTLCache[list[str]] = _TTLCache(ttl)
        self._client: LoreClient | None = None
        self._session_id: str = ""
        self._captured_turns: list[dict[str, str]] = []
        # Rolling-window extraction state. _session_turns accumulates EVERY turn
        # for the lifetime of the session (independent of write_frequency, which
        # clears _captured_turns after each per-turn flush). _last_extracted_turn
        # is the index into _session_turns at the last mid-session extraction;
        # the slice [_last_extracted_turn:] is the not-yet-extracted tail.
        self._session_turns: list[dict[str, str]] = []
        self._last_extracted_turn: int = 0

    # -- identity ------------------------------------------------------------

    @property
    def name(self) -> str:
        return "lore"

    def is_available(self) -> bool:
        # Per the ABC, this must not make network calls — just confirm we
        # have a URL configured and httpx importable. Reachability is
        # checked at write/read time via LoreClient.is_available().
        return bool(self._lore_url)

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id or self._session_id
        self._client = LoreClient(self._lore_url)

    def shutdown(self) -> None:
        # Best-effort flush of any unpersisted turns on clean exit.
        if self._captured_turns:
            try:
                self._persist_turns()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Lore shutdown flush failed: %s", exc)
        self._client = None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        # Flush the current session's turns before switching so they land
        # under the right session record.
        if self._captured_turns:
            try:
                self._persist_turns()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Lore session-switch flush failed: %s", exc)
        self._captured_turns = []
        # Reset rolling-window accumulators for the new session.
        self._session_turns = []
        self._last_extracted_turn = 0
        self._session_id = new_session_id

    # -- recall --------------------------------------------------------------

    def system_prompt_block(self) -> str:
        return (
            "# Lore Memory\n"
            "Active. Persistent recall is backed by Lore (knowledge base). "
            "Relevant memories are injected automatically before each turn. "
            "Use lore_remember to explicitly store something durable."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not query or self._client is None:
            return ""
        if not self._client.is_available():
            return ""

        # Run the recall query and the (cached) prefs lookup concurrently so
        # their round-trips overlap instead of running serially.
        recall_lines, pref_lines = self._gather_recall_and_prefs(query)

        if not recall_lines and not pref_lines:
            return ""

        # The guard above guarantees at least one of recall/pref is non-empty,
        # so ``sections`` is always populated here (no empty-body branch needed).
        sections: list[str] = []
        if pref_lines:
            sections.append("### User preferences\n\n" + "\n\n".join(pref_lines))
        if recall_lines:
            sections.append("\n\n".join(recall_lines))
        body = "## Recalled from Lore\n\n" + "\n\n".join(sections)
        return f"{MEMORY_FENCE_START}\n{body}\n{MEMORY_FENCE_END}"

    def _gather_recall_and_prefs(self, query: str) -> tuple[list[str], list[str]]:
        """Fetch recall hits and prefs concurrently; return rendered lines.

        Both branches run the blocking httpx client in worker threads under a
        single event loop, so the recall search and the prefs lookup overlap.
        Each branch degrades to an empty list on error — recall is
        best-effort and never raises into the agent.
        """

        async def _run() -> tuple[list[str], list[str]]:
            results = await asyncio.gather(
                asyncio.to_thread(self._recall_lines, query),
                asyncio.to_thread(self._pref_lines),
                return_exceptions=True,
            )
            recall = results[0] if isinstance(results[0], list) else []
            prefs = results[1] if isinstance(results[1], list) else []
            return (recall, prefs)

        try:
            return asyncio.run(_run())
        except Exception as exc:  # noqa: BLE001 - prefetch is best-effort
            logger.debug("Lore prefetch gather failed: %s", exc)
            return [], []

    def _recall_lines(self, query: str) -> list[str]:
        """Semantic/hybrid recall: search then batch-fetch content for hits."""
        client = self._client
        if client is None:
            return []
        try:
            results = client.kb_search(
                query, search_mode=self._recall_mode, topic=None, top_k=RECALL_TOP_K
            )
        except Exception as exc:  # noqa: BLE001 - recall is best-effort
            logger.debug("Lore recall search failed: %s", exc)
            return []
        if not results:
            return []
        return self._render_entries(results)

    def _pref_lines(self) -> list[str]:
        """Rendered user-preference lines, served from a TTL cache.

        Returns [] unless ``prefs_enabled`` is set. On a cache miss, lists the
        prefs topic and batch-fetches content in one shot, then caches the
        rendered lines for ``prefs_cache_ttl`` seconds. Invalidated on any
        write touching the prefs topic (see _maybe_invalidate_prefs).
        """
        if not self._prefs_enabled or self._client is None:
            return []
        hit, cached = self._prefs_cache.get()
        if hit and cached is not None:
            return cached
        try:
            entries = self._client.kb_list(self._prefs_topic, limit=PREFS_LIST_LIMIT)
        except Exception as exc:  # noqa: BLE001 - best-effort
            logger.debug("Lore prefs list failed: %s", exc)
            return []
        lines = self._render_entries(entries) if entries else []
        self._prefs_cache.set(lines)
        return lines

    def _render_entries(self, entries: list[dict[str, Any]]) -> list[str]:
        """Render KB entries to fenced-block lines, batch-fetching content.

        kb_search/kb_list results omit ``content``, so any entry lacking it has
        its full body fetched via a SINGLE kb_get_batch call (one round-trip,
        or concurrent kb_get under the hood) rather than N sequential kb_get.
        Truncation (400 chars + ellipsis) and the ``**[topic] title**`` header
        format are preserved exactly.
        """
        client = self._client
        if client is None:
            return []
        # Identify which entries still need full content fetched.
        need_ids = [
            e.get("kb_id", "")
            for e in entries
            if not e.get("content") and e.get("kb_id")
        ]
        fetched: dict[str, str] = {}
        if need_ids:
            try:
                rows = client.kb_get_batch(need_ids)
            except Exception as exc:  # noqa: BLE001 - best-effort
                logger.warning("prefetch kb_get_batch failed: %s", exc)
                rows = []
            fetched_rows: dict[str, dict[str, Any]] = {
                row["kb_id"]: row
                for row in rows
                if isinstance(row, dict) and row.get("kb_id")
            }
            for kid in need_ids:
                row = fetched_rows.get(kid)
                if row is not None:
                    fetched[kid] = (row.get("content") or "").strip()

        lines: list[str] = []
        for r in entries:
            title = r.get("title") or r.get("kb_id", "")
            topic = r.get("topic", "")
            prefix = f"[{topic}] " if topic else ""
            entry_text = f"**{prefix}{title}**"
            content = (r.get("content") or "").strip()
            if not content:
                content = fetched.get(r.get("kb_id", ""), "")
            if content:
                truncated = content[:400] + ("…" if len(content) > 400 else "")
                entry_text += f"\n{truncated}"
            lines.append(entry_text)
        return lines

    def _maybe_invalidate_prefs(self, topic: str | None) -> None:
        """Bust the prefs cache when a write touches the prefs topic."""
        if topic and topic == self._prefs_topic:
            self._prefs_cache.invalidate()

    # -- write ---------------------------------------------------------------

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None:
        # Capture raw turn for persistence. No LLM call.
        # Strip injected memory so recalled context is never re-stored.
        user_clean = strip_memory_fence(user_content or "").strip()
        assistant_clean = (assistant_content or "").strip()
        if not user_clean and not assistant_clean:
            return
        turn_record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "session_id": session_id or self._session_id,
            "user": user_clean,
            "assistant": assistant_clean,
        }
        self._captured_turns.append(turn_record)
        # Mirror into the session-long accumulator used by rolling-window
        # extraction. _captured_turns may be cleared per-turn (write_frequency
        # "turn"), so it cannot be reused as a session-long history.
        self._session_turns.append(turn_record)
        # Fire mid-session extraction if the rolling window is enabled and the
        # stride has been reached. Fully best-effort; never raises into the agent.
        try:
            self._maybe_roll_extract()
        except Exception as exc:  # noqa: BLE001 - rolling extraction is best-effort
            logger.debug("Lore rolling extraction trigger failed: %s", exc)
        # write_frequency "turn": persist immediately after each turn.
        # "session" (or any other value): defer until on_session_end. Never
        # raise into the agent — persistence is best-effort.
        if self._write_frequency == "turn":
            try:
                self._persist_turns()
            except Exception as exc:  # noqa: BLE001 - never raise into the agent
                logger.debug("Lore per-turn flush failed: %s", exc)
            finally:
                self._captured_turns = []

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        # True no-op only when the session had no turns at all. _captured_turns
        # may already be empty (write_frequency "turn" flushes per turn), but
        # _session_turns still holds the full history that rolling-window
        # extraction draws from, so guard on both.
        if not self._captured_turns and not self._session_turns:
            return
        if self._captured_turns:
            try:
                self._persist_turns()
            except Exception as exc:  # noqa: BLE001 - never raise into the agent
                logger.debug("Lore on_session_end flush failed: %s", exc)
            finally:
                self._captured_turns = []
        # Optional automatic memory extraction (off by default). Fire-and-forget
        # so it never delays session teardown; fully best-effort.
        #
        # With the rolling window on, the mid-session passes have already
        # extracted everything up to _last_extracted_turn, so the session-end
        # pass only needs the remaining tail (plus an overlap look-back for
        # context). With it off, behavior is unchanged: pass the full snapshot
        # and let filter_turns cap it to max_turns.
        auto = self._config.get("auto_extract", {})
        if auto.get("rolling_window", False):
            overlap = int(auto.get("rolling_overlap", 5))
            start = max(0, self._last_extracted_turn - overlap)
            remaining = self._session_turns[start:]
            if not remaining:
                # The last mid-session fire landed on the final turn — there is
                # nothing new to extract. Skip the redundant pass but still reset
                # rolling state so the next session starts clean.
                self._last_extracted_turn = 0
                self._session_turns = []
                return
            turns_to_extract = remaining
        else:
            # Non-rolling: use _session_turns (persists full session history
            # regardless of write_frequency), not _captured_turns — under
            # write_frequency="turn" the latter is cleared after every turn and
            # would be empty here, silently extracting zero memories.
            turns_to_extract = list(self._session_turns)  # full session history
        self._maybe_fire_extraction(turns_to_extract)
        # Reset rolling-window state so the next session starts clean.
        self._session_turns = []
        self._last_extracted_turn = 0

    def _maybe_roll_extract(self) -> None:
        """Fire mid-session extraction if rolling window is on and stride reached.

        No-op unless ``auto_extract.enabled`` AND ``auto_extract.rolling_window``
        are both set. Extracts the not-yet-extracted tail of _session_turns,
        looking back ``rolling_overlap`` turns into already-extracted territory
        for context, every ``rolling_stride`` new turns. Advances the cursor
        BEFORE firing so a slow extraction task can't double-extract.
        """
        auto = self._config.get("auto_extract", {})
        if not auto.get("enabled", False):
            return
        if not auto.get("rolling_window", False):
            return

        stride = int(auto.get("rolling_stride", 15))
        overlap = int(auto.get("rolling_overlap", 5))

        turns_since_last = len(self._session_turns) - self._last_extracted_turn
        if turns_since_last < stride:
            return

        # Slice: go back `overlap` turns into already-extracted territory so the
        # extractor has surrounding context for the new turns.
        start = max(0, self._last_extracted_turn - overlap)
        window = self._session_turns[start:]

        # Advance the cursor BEFORE firing to prevent double-extraction if the
        # background task is slow to run. Remember the pre-advance value so a
        # failed extraction task can roll the cursor back (see Fix 1) — the turns
        # it covered must not be permanently skipped.
        pre_advance = self._last_extracted_turn
        self._last_extracted_turn = len(self._session_turns)

        # Trim turns that will never be needed again, keeping ``overlap`` turns
        # before the new cursor as the look-back buffer. This bounds memory to
        # roughly ``stride + overlap`` turns regardless of session length. The
        # cursor (and the rollback target) shift left by the trimmed count so
        # they keep pointing at the same logical turns.
        trim_to = max(0, self._last_extracted_turn - overlap)
        if trim_to > 0:
            del self._session_turns[:trim_to]
            self._last_extracted_turn -= trim_to
            pre_advance = max(0, pre_advance - trim_to)

        self._maybe_fire_extraction(window, pre_advance=pre_advance)

    def _maybe_fire_extraction(
        self,
        captured_turns: list[dict[str, str]],
        *,
        pre_advance: int | None = None,
    ) -> None:
        """Kick off background memory extraction if enabled in config.

        No-op unless ``auto_extract.enabled`` is set. Imports the extraction
        pipeline lazily and degrades silently if the lore-mcp package providing
        it is not importable, so the plugin keeps working without it.

        ``pre_advance`` is the value of ``_last_extracted_turn`` *before* the
        caller advanced the cursor for this fire. When provided, a failure of
        the extraction task rolls the cursor back to it so the covered turns are
        retried rather than permanently skipped. ``None`` (the default) means no
        cursor was advanced for this fire (e.g. the non-rolling session-end
        pass), so there is nothing to roll back.
        """
        if not self._config.get("auto_extract", {}).get("enabled", False):
            return
        if self._client is None or not captured_turns:
            return
        try:
            from lore.extraction import extract_and_store
        except Exception as exc:  # noqa: BLE001 - extraction package optional
            logger.debug("Auto-extraction unavailable (import failed): %s", exc)
            return
        # Flatten captured user/assistant turns into role/content turns.
        turns: list[dict[str, str]] = []
        for turn in captured_turns:
            if turn.get("user"):
                turns.append({"role": "user", "content": turn["user"]})
            if turn.get("assistant"):
                turns.append({"role": "assistant", "content": turn["assistant"]})
        coro = extract_and_store(
            turns=turns,
            db_client=self._client,
            config=self._config,
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (synchronous teardown) — run to completion now.
            # Synchronous path: if it raises, roll back the cursor (when one was
            # advanced for this fire) so the covered turns are retried, then
            # swallow (never raise into the agent).
            try:
                asyncio.run(coro)
            except Exception as exc:  # noqa: BLE001 - never raise into the agent
                if pre_advance is not None:
                    logger.warning(
                        "Auto-extraction run failed; rolling back cursor "
                        "from %d to %d",
                        self._last_extracted_turn,
                        pre_advance,
                    )
                    self._last_extracted_turn = pre_advance
                logger.debug("Auto-extraction run failed: %s", exc)
            return

        # Async path: advancing the cursor happened in the caller before this
        # task was created. If the background task crashes, roll the cursor back
        # to ``pre_advance`` so those turns aren't permanently skipped.
        task = loop.create_task(coro)

        if pre_advance is not None:
            # Bind the narrowed (non-None) value to a local so the default
            # argument below is typed ``int`` rather than ``int | None``.
            rollback_to: int = pre_advance

            def _on_done(t: asyncio.Task, _pa: int = rollback_to) -> None:
                if t.exception() is not None:
                    logger.warning(
                        "Rolling extraction task failed; rolling back cursor "
                        "from %d to %d",
                        self._last_extracted_turn,
                        _pa,
                    )
                    self._last_extracted_turn = _pa

            task.add_done_callback(_on_done)

    def _persist_turns(self) -> None:
        """Store captured turns to Lore as one structured conversation entry."""
        if not self._captured_turns or self._client is None:
            return
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        title = f"Session {self._session_id} — {date}"
        # Render turns as a readable, structured body. The hermes-scheduler
        # job summarizes these later; the provider never calls an LLM.
        parts: list[str] = []
        for i, turn in enumerate(self._captured_turns, 1):
            parts.append(
                f"### Turn {i} ({turn['timestamp']})\n"
                f"USER: {turn['user']}\n"
                f"ASSISTANT: {turn['assistant']}"
            )
        content = "\n\n".join(parts)
        add_or_update(
            self._client,
            topic=CONVERSATIONS_TOPIC,
            title=title,
            content=content,
            tags=["hermes-session", self._session_id]
            if self._session_id
            else ["hermes-session"],
            author="hermes",
            threshold=self._dedup_threshold,
        )
        # No-op for the conversations topic, but keeps invalidation correct if
        # this path is ever pointed at the prefs topic.
        self._maybe_invalidate_prefs(CONVERSATIONS_TOPIC)

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [LORE_REMEMBER_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if tool_name == "lore_remember":
            return self._handle_lore_remember(args)
        return tool_error(f"Unknown tool: {tool_name}")

    def _handle_lore_remember(self, args: dict[str, Any]) -> str:
        if self._client is None:
            return tool_error("Lore provider not initialized")
        content = args.get("content")
        if not content:
            return tool_error("Missing required argument: content")
        title = args.get("title") or content[:60]
        topic = args.get("topic") or DEFAULT_MEMORY_TOPIC
        tags = args.get("tags")
        try:
            result = add_or_update(
                self._client,
                topic=topic,
                title=title,
                content=content,
                tags=tags,
                author="hermes",
                threshold=self._dedup_threshold,
            )
            # Bust the prefs cache if this write touched the prefs topic so the
            # next prefetch re-reads fresh prefs instead of stale cached lines.
            self._maybe_invalidate_prefs(topic)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return tool_error(str(exc))

    # -- config --------------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "lore_url",
                "description": "Base URL of the Lore KB-MCP server",
                "default": DEFAULT_LORE_URL,
            },
            {
                "key": "recall_mode",
                "description": "kb_search mode used for prefetch recall",
                "default": "hybrid",
                "choices": ["fts", "semantic", "hybrid"],
            },
            {
                "key": "write_frequency",
                "description": "When to persist turns to Lore",
                "default": "turn",
                "choices": ["turn", "session"],
            },
            {
                "key": "dedup_threshold",
                "description": (
                    "rrf_score at/above which a new entry is treated as a "
                    "near-duplicate (higher = more similar, hybrid mode)"
                ),
                "default": str(DEDUP_THRESHOLD),
            },
            {
                "key": "prefs_enabled",
                "description": (
                    "Surface cached user preferences (topic prefs_topic) in the "
                    "prefetch block. Off by default to keep recall-only behavior."
                ),
                "default": False,
            },
            {
                "key": "prefs_topic",
                "description": "KB topic holding durable user preferences",
                "default": PREFS_TOPIC,
            },
            {
                "key": "prefs_cache_ttl",
                "description": (
                    "Seconds to cache rendered user preferences in-process "
                    "(invalidated on any write to prefs_topic)"
                ),
                "default": str(PREFS_CACHE_TTL),
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        cfg_dir = Path(hermes_home) / "plugins" / "lore"
        try:
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(
                json.dumps(values, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lore save_config failed: %s", exc)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    """Register the Lore memory provider with the Hermes plugin system."""
    config = _load_plugin_config()
    provider = LoreMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
