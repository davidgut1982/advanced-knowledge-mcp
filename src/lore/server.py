#!/usr/bin/env python3
"""
Lore MCP Server - the operational knowledge layer for engineers and AI agents.

Unified knowledge management system for:
- Knowledge Base (structured operational knowledge with attribution)
- Investigations (ops debugging notes and structured experiments)
- Journal (decision log and config snapshots)
- MCP Index (tool discovery and search)
- Search (local files, corpora, transcripts, multi-source)

Spec: Lore v0.4.0 — KG and source-tracking tool surfaces removed; research surface
renamed to investigations; attribution model added (author, source_type, verified).
"""

import decimal
import glob as glob_module
import hashlib
import json
import logging
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

import jsonschema
import sentry_sdk
import yaml
from mcp import types
from mcp.server import Server

from . import __version__ as _PACKAGE_VERSION
from . import telemetry
from .db_client import DatabaseBackend, get_db_client

# Import document processor and MCP scanner
from .doc_processor import DocumentProcessor
from .env_config import get_env, require_env
from .mcp_index_scanner import MCPIndexScanner
from .response import ErrorCodes, ResponseEnvelope

# Initialize logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Initialize Sentry
SENTRY_DSN = get_env("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=1.0,
        environment=get_env("SENTRY_ENVIRONMENT", "development"),
        release=get_env("SENTRY_RELEASE", f"lore-knowledge-mcp@{_PACKAGE_VERSION}"),
    )
    logger.info("Sentry monitoring enabled")

# Initialize MCP server
app = Server("lore")

# Database Configuration.
#
# The module-global ``db`` is initialised exactly once at startup — NOT at
# import time. Connecting at import would run before main()/the FastMCP
# lifespan can apply the ``DB_BACKEND=sqlite`` default (os.environ.setdefault),
# so it would attempt to connect against whatever DB_BACKEND happens to be in
# the environment (defaulting to Supabase) and log a spurious failure / open a
# stray connection that is immediately discarded when main() re-initialises.
#
# Initialisation happens in exactly two places, both BEFORE any handler runs:
#   - lore.server.main()                 (stdio + HTTP/SSE entry point)
#   - lore.server_fastmcp.lore_lifespan  (FastMCP startup lifespan)
# Handlers reference this module global directly, which is guaranteed to be a
# live client by the time a request is dispatched.
db = None


# Search Configuration (consolidated from search-mcp).
#
# These roots are optional, deployment-specific corpora locations. They are
# env-var driven with portable defaults so a fresh public install never points
# at a nonexistent /srv/* path:
#   - KNOWLEDGE_DATA_DIR defaults to ./knowledge-data, matching the SQLite
#     fallback (see db_client.get_db_client) so local-file search and the
#     default DB live in the same place.
#   - LATVIAN_LEARNING_ROOT / LATVIAN_XTTS_ROOT / INGEST_ROOT default to None
#     (unset). The dependent tools (search_transcripts, search_corpora,
#     search_local) detect the unset root and return a clean empty result with
#     a "not configured" message instead of scanning a path that doesn't exist.
def _optional_root(env_key: str) -> Path | None:
    """Return a Path for ``env_key`` when set/non-empty, else None."""
    raw = get_env(env_key)
    if raw is None or not raw.strip():
        return None
    return Path(raw.strip())


LATVIAN_LEARNING_ROOT: Path | None = _optional_root("LATVIAN_LEARNING_ROOT")
LATVIAN_XTTS_ROOT: Path | None = _optional_root("LATVIAN_XTTS_ROOT")
INGEST_ROOT: Path | None = _optional_root("INGEST_ROOT")
KNOWLEDGE_DATA_DIR = Path(get_env("KNOWLEDGE_DATA_DIR", "./knowledge-data"))


def json_serializer(obj):
    """Custom JSON serializer for datetime, UUID, and Decimal objects."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def format_response(response: dict) -> list[types.TextContent]:
    """Format response as MCP TextContent."""
    return [types.TextContent(type="text", text=json.dumps(response, default=json_serializer))]


def _sanitize_search_query(query: str) -> str:
    """Strip PostgREST filter metacharacters to prevent filter injection."""
    # Remove commas, dots, parentheses, and PostgREST operator tokens
    sanitized = re.sub(r"[,.()\[\]]", " ", query)
    # Strip known PostgREST operator patterns
    sanitized = re.sub(
        r"\b(wfts|plfts|fts|phfts|ilike|like|eq|neq|gt|gte|lt|lte|in|is)\b",
        "",
        sanitized,
        flags=re.IGNORECASE,
    )
    # Collapse whitespace
    return " ".join(sanitized.split()).strip()


# Above this file count, kb_ingest_dir is treated as a bulk write and
# requires confirm_production in production (issue #11).
_INGEST_DIR_GUARD_THRESHOLD = 100


def _is_production() -> bool:
    """Whether the active environment is production.

    Sourced from ``LORE_ENV`` and defaults to production when unset so an
    unconfigured deployment is treated as production (fail-safe).
    """
    return os.getenv("LORE_ENV", "production").strip().lower() == "production"


def _production_guard(
    tool_name: str, confirm_production: bool, dry_run: bool = False
) -> dict | None:
    """Return an error response if a destructive op needs confirmation in prod.

    The guard fires only when ALL of the following hold:
      - ``LORE_ENV`` is ``production`` (or unset — fail-safe default), AND
      - the caller did not pass ``confirm_production=True``, AND
      - the call is not a ``dry_run`` (dry runs never write, so always safe).

    Returns ``None`` when the call is permitted; otherwise a ready-to-return
    error envelope explaining how to proceed. Centralising the logic keeps the
    confirmation contract identical across every guarded tool.
    """
    if dry_run or confirm_production or not _is_production():
        return None
    return ResponseEnvelope.error(
        ErrorCodes.PRODUCTION_GUARD,
        f"{tool_name} requires confirm_production=true when LORE_ENV=production. "
        "This prevents accidental large-scale writes. Pass confirm_production=true to proceed.",
    )


def _coerce_arguments(arguments: dict, schema: dict) -> dict:
    """Coerce JSON-stringified array/object values into proper Python types.

    Claude Code sometimes serializes array or object parameters as JSON strings
    instead of proper arrays/objects. Three encoding formats are handled:

    1. JSON-encoded:  tags='["a","b","c"]'  → ["a","b","c"]
    2. Comma-separated: tags='a,b,c'        → ["a","b","c"]
    3. Space-separated: tags='a b c'        → ["a","b","c"]

    Formats 2 and 3 only apply to fields whose schema items type is "string".
    JSON encoding is always tried first.

    Args:
        arguments: Raw arguments dict from the MCP call.
        schema: The tool's inputSchema dict.

    Returns:
        A new dict with coerced values; original is not mutated.
    """
    if not arguments or not schema:
        return arguments

    properties = schema.get("properties", {})
    if not properties:
        return arguments

    coerced = dict(arguments)
    for field, field_schema in properties.items():
        if field not in coerced:
            continue
        value = coerced[field]
        expected_type = field_schema.get("type")
        if expected_type == "array" and isinstance(value, str):
            # Try JSON parse first (handles '["a","b"]' format)
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    coerced[field] = parsed
                    continue
            except (json.JSONDecodeError, ValueError):
                pass
            # Fall back to splitting plain-text strings (space or comma separated).
            # Only safe for string-item arrays; skip for numeric/object item arrays.
            items_schema = field_schema.get("items", {})
            if items_schema.get("type", "string") == "string":
                stripped = value.strip()
                if stripped:
                    # Prefer comma-split if commas present, else space-split
                    if "," in stripped:
                        parts = [p.strip() for p in stripped.split(",") if p.strip()]
                    else:
                        parts = stripped.split()
                    if parts:
                        coerced[field] = parts
        elif expected_type == "object" and isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    coerced[field] = parsed
            except (json.JSONDecodeError, ValueError):
                pass  # Leave the value as-is; jsonschema will report the error
        elif expected_type == "boolean" and isinstance(value, str):
            lower = value.strip().lower()
            if lower in ("true", "1", "yes"):
                coerced[field] = True
            elif lower in ("false", "0", "no"):
                coerced[field] = False
            # else: leave as-is; jsonschema validation will catch invalid values
    return coerced


# =============================================================================
# Tool Registration
# =============================================================================

# Module-level tool definitions used by both list_tools() and _get_tool_schema().
# Keeping them here avoids duplicating schemas and enables synchronous schema
# lookups inside call_tool() for the array-coercion fix.
_TOOL_DEFINITIONS = [
    # Knowledge Base Tools (6)
    types.Tool(
        name="kb_add",
        description="Add a knowledge base entry",
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic"},
                "title": {"type": "string", "description": "Entry title"},
                "content": {"type": "string", "description": "Entry content"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"},
                "author": {
                    "type": "string",
                    "description": "Who is creating this entry (your name, agent name, or system). Optional.",
                },
                "source_type": {
                    "type": "string",
                    "description": "Origin: 'human', 'agent', or 'system'. Optional, defaults to null.",
                },
                "trust_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 1.0,
                    "description": (
                        "Confidence weight for this fact (0.0–1.0). Defaults to "
                        "1.0 (fully trusted). Lower values mark low-confidence "
                        "facts that callers can later exclude via kb_search's "
                        "min_trust_score filter."
                    ),
                },
            },
            "required": ["topic", "title", "content"],
        },
    ),
    types.Tool(
        name="kb_search",
        description=(
            "Search knowledge base. Lexical FTS5 (or LIKE fallback) by default; "
            "set semantic=true / hybrid=true / search_mode=hybrid to use vector "
            "embeddings + RRF fusion (requires LORE_SEMANTIC_SEARCH=true)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "topic": {"type": "string", "description": "Filter by topic"},
                "top_k": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Number of results to return (default 20).",
                },
                "semantic": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force semantic (vector) search only. Shortcut for search_mode='semantic'.",
                },
                "hybrid": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force hybrid (FTS5 + vector + RRF). Shortcut for search_mode='hybrid'.",
                },
                "search_mode": {
                    "type": "string",
                    "enum": ["fts", "semantic", "hybrid"],
                    "description": (
                        "Explicit search mode. Overrides semantic/hybrid flags. "
                        "Falls back to FTS when semantic is unavailable."
                    ),
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional opaque id linking related searches in one session "
                        "(retrieval telemetry, issue #5). No effect unless "
                        "LORE_HARD_NEGATIVE_MINING=true on a PostgreSQL backend."
                    ),
                },
                "parent_query_id": {
                    "type": "string",
                    "description": (
                        "Optional query_id of the search this one re-queries/refines "
                        "(retrieval telemetry, issue #5)."
                    ),
                },
                "required_requery": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Optional hint that this search was a re-query after an "
                        "unsatisfying prior result (retrieval telemetry, issue #5)."
                    ),
                },
                "caller_agent": {
                    "type": "string",
                    "description": (
                        "Optional name of the agent/user issuing the search "
                        "(retrieval telemetry, issue #5)."
                    ),
                },
                "min_score": {
                    "type": "number",
                    "description": (
                        "Minimum relevance score threshold. Results below this "
                        "score are excluded. For hybrid mode uses rrf_score; for "
                        "fts/semantic uses score. Range 0.0-1.0 for "
                        "semantic/hybrid; unbounded for raw fts. Note: SQLite FTS5 "
                        "uses bm25() scores which are negative (e.g. -1.5 to 0.0); "
                        "set min_score to a negative value on the fts path, or use "
                        "hybrid/semantic modes for intuitive 0.0-1.0 scoring."
                    ),
                },
                "min_trust_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Minimum trust score threshold (0.0–1.0). Excludes "
                        "entries with trust_score below this value. Useful for "
                        "filtering out deprecated or low-confidence facts."
                    ),
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="kb_get",
        description="Get full KB entry by ID",
        inputSchema={
            "type": "object",
            "properties": {"kb_id": {"type": "string", "description": "KB entry ID"}},
            "required": ["kb_id"],
        },
    ),
    types.Tool(
        name="kb_list",
        description="List KB entries",
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Filter by topic"},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of entries to return (1–500, default 100)",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                },
                "offset": {
                    "type": "integer",
                    "description": "Number of entries to skip for pagination (default 0)",
                    "minimum": 0,
                },
            },
        },
    ),
    types.Tool(
        name="kb_update",
        description=(
            "Update existing KB entry content, title, topic, tags, verified state, and trust_score"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "kb_id": {"type": "string", "description": "kb_id of entry to update"},
                "title": {"type": "string", "description": "New title"},
                "content": {"type": "string", "description": "New content text"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Updated tags array",
                },
                "topic": {"type": "string", "description": "Updated topic/category for the entry"},
                "verified": {
                    "type": ["boolean", "null"],
                    "description": "Mark entry as human-verified (true), disputed (false), or reset to unreviewed (null).",
                },
                "trust_score": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": (
                        "Update the entry's confidence weight (0.0–1.0). Omit to "
                        "leave the existing trust_score unchanged."
                    ),
                },
            },
            "required": ["kb_id"],
        },
    ),
    types.Tool(
        name="kb_delete",
        description="Delete existing KB entry from database",
        inputSchema={
            "type": "object",
            "properties": {
                "kb_id": {"type": "string", "description": "kb_id of entry to delete"},
                "confirm": {
                    "type": "boolean",
                    "description": "Confirmation flag for safety",
                    "default": False,
                },
            },
            "required": ["kb_id"],
        },
    ),
    # Investigations Tools (5)
    types.Tool(
        name="investigation_add",
        description="Add an investigation entry (open or append to an ops investigation)",
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["topic", "title", "content"],
        },
    ),
    types.Tool(
        name="investigation_list",
        description="List investigations",
        inputSchema={
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "Filter by topic"}},
        },
    ),
    types.Tool(
        name="investigation_get",
        description="Get a single investigation entry by ID",
        inputSchema={
            "type": "object",
            "properties": {"note_id": {"type": "string"}},
            "required": ["note_id"],
        },
    ),
    types.Tool(
        name="investigation_log_experiment",
        description="Log a structured experiment within an investigation (hypothesis, methodology, results, conclusion)",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "hypothesis": {"type": "string"},
                "methodology": {"type": "string"},
                "results": {"type": "object"},
                "conclusion": {"type": "string"},
            },
            "required": ["title"],
        },
    ),
    types.Tool(
        name="investigation_list_experiments",
        description="List logged investigation experiments",
        inputSchema={"type": "object", "properties": {}},
    ),
    types.Tool(
        name="investigation_delete_note",
        description=(
            "Hard-delete an investigation note by note_id (Issue #21). Requires "
            "confirm=True; in LORE_ENV=production also requires confirm_production=True."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "string",
                    "description": "note_id of the investigation note to delete",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Confirmation flag for safety",
                    "default": False,
                },
                "confirm_production": {
                    "type": "boolean",
                    "description": "Required in LORE_ENV=production to allow destructive write",
                    "default": False,
                },
            },
            "required": ["note_id"],
        },
    ),
    types.Tool(
        name="investigation_delete_experiment",
        description=(
            "Hard-delete an investigation experiment by experiment_id (Issue #21). "
            "Requires confirm=True; in LORE_ENV=production also requires "
            "confirm_production=True."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "experiment_id": {
                    "type": "string",
                    "description": "experiment_id of the experiment to delete",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Confirmation flag for safety",
                    "default": False,
                },
                "confirm_production": {
                    "type": "boolean",
                    "description": "Required in LORE_ENV=production to allow destructive write",
                    "default": False,
                },
            },
            "required": ["experiment_id"],
        },
    ),
    # Journal Tools (4)
    types.Tool(
        name="journal_append",
        description="Append journal entry",
        inputSchema={
            "type": "object",
            "properties": {
                "entry_type": {
                    "type": "string",
                    "enum": ["daily", "milestone", "reflection", "idea"],
                },
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["entry_type", "content"],
        },
    ),
    types.Tool(
        name="journal_list",
        description="List journal entries",
        inputSchema={"type": "object", "properties": {"limit": {"type": "integer", "default": 20}}},
    ),
    types.Tool(
        name="journal_get",
        description="Get journal entry",
        inputSchema={
            "type": "object",
            "properties": {"entry_id": {"type": "string"}},
            "required": ["entry_id"],
        },
    ),
    types.Tool(
        name="journal_search",
        description="Full-text search across journal entry content (Issue #15)",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Full-text search query"},
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 200,
                    "description": "Max results",
                },
                "entry_type": {
                    "type": "string",
                    "description": "Filter by entry type (optional)",
                },
                "date_from": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date lower bound e.g. 2026-01-01 (optional)",
                },
                "date_to": {
                    "type": "string",
                    "format": "date",
                    "description": "ISO date upper bound (optional)",
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="journal_delete",
        description=(
            "Hard-delete a journal entry by entry_id (Issue #21). Requires "
            "confirm=True; in LORE_ENV=production also requires confirm_production=True."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "entry_id of the journal entry to delete",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Confirmation flag for safety",
                    "default": False,
                },
                "confirm_production": {
                    "type": "boolean",
                    "description": "Required in LORE_ENV=production to allow destructive write",
                    "default": False,
                },
            },
            "required": ["entry_id"],
        },
    ),
    types.Tool(
        name="snapshot_config",
        description="Snapshot current config",
        inputSchema={
            "type": "object",
            "properties": {"config_name": {"type": "string"}, "config_data": {"type": "object"}},
            "required": ["config_name", "config_data"],
        },
    ),
    # Document Ingestion Tools (4) - v1.3
    types.Tool(
        name="kb_ingest_doc",
        description="Ingest single markdown file into KB with change detection",
        inputSchema={
            "type": "object",
            "properties": {
                "doc_path": {"type": "string", "description": "Absolute path to markdown file"},
                "strategy": {
                    "type": "string",
                    "enum": ["full", "chunked", "summary"],
                    "default": "chunked",
                    "description": "Ingestion strategy: full (one entry) or chunked (by sections). 'summary' is not provided by Lore (LLM-free); generate summaries in the caller — see issue #19.",
                },
                "chunk_size": {
                    "type": "integer",
                    "default": 2000,
                    "description": "Max tokens per chunk (chunked strategy only)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional tags",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Replace existing KB entries from this doc",
                },
                "author": {
                    "type": "string",
                    "description": "Who is ingesting (optional, defaults to None)",
                },
                "source_type": {
                    "type": "string",
                    "default": "system",
                    "description": "Source type for attribution (defaults to 'system' since ingestion is automated)",
                },
            },
            "required": ["doc_path"],
        },
    ),
    types.Tool(
        name="kb_ingest_dir",
        description="Batch ingest directory of markdown files",
        inputSchema={
            "type": "object",
            "properties": {
                "dir_path": {"type": "string", "description": "Directory to scan"},
                "pattern": {
                    "type": "string",
                    "default": "*.md",
                    "description": "File pattern (e.g., *.md)",
                },
                "strategy": {
                    "type": "string",
                    "enum": ["full", "chunked", "summary"],
                    "default": "chunked",
                },
                "recursive": {
                    "type": "boolean",
                    "default": True,
                    "description": "Scan subdirectories",
                },
                "exclude_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Patterns to exclude",
                },
                "author": {
                    "type": "string",
                    "description": "Who is ingesting (optional, defaults to None)",
                },
                "source_type": {
                    "type": "string",
                    "default": "system",
                    "description": "Source type for attribution (defaults to 'system' since ingestion is automated)",
                },
                "confirm_production": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required (true) to ingest more than 100 files when "
                        "LORE_ENV=production. Guards against accidental bulk "
                        "writes; not required for smaller ingests or non-prod."
                    ),
                },
            },
            "required": ["dir_path"],
        },
    ),
    types.Tool(
        name="kb_sync_status",
        description="Check sync state between source docs and KB",
        inputSchema={
            "type": "object",
            "properties": {
                "dir_path": {
                    "type": "string",
                    "description": (
                        "Directory to check. Optional: defaults to LORE_SYNC_DIR "
                        "(or LORE_KB_DIR) when not provided. Returns a not_configured "
                        "error if neither is set."
                    ),
                }
            },
        },
    ),
    # Semantic Search Tools (2) - v0.6
    types.Tool(
        name="kb_backfill_embeddings",
        description=(
            "Embed any KB entries that are missing or stale (model/content "
            "changed). Idempotent: skips entries whose stored content_hash "
            "still matches. Requires LORE_SEMANTIC_SEARCH=true."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "batch_size": {
                    "type": "integer",
                    "default": 32,
                    "minimum": 1,
                    "maximum": 512,
                    "description": "How many entries to encode per batch (default 32).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional cap on entries to process this run.",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "If true, report what would be embedded without writing.",
                },
                "confirm_production": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Required (true) to run a real backfill when "
                        "LORE_ENV=production. Guards against accidental "
                        "large-scale writes; ignored for dry runs and non-prod."
                    ),
                },
            },
        },
    ),
    types.Tool(
        name="kb_embedding_status",
        description=(
            "Report embedding coverage: total entries, embedded count, missing "
            "count, current model, and per-model breakdown."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    # MCP Index Tools (5)
    types.Tool(
        name="mcp_index_scan",
        description="Scan all MCP servers and index their tools. By default, scans only configured servers (66% token savings).",
        inputSchema={
            "type": "object",
            "properties": {
                "triggered_by": {
                    "type": "string",
                    "default": "manual",
                    "description": "Source of scan (manual, cron, deployment)",
                },
                "config_filter": {
                    "type": "boolean",
                    "default": True,
                    "description": "If true (default), scan only servers in ~/.claude.json. Set false to scan all servers.",
                },
            },
        },
    ),
    types.Tool(
        name="mcp_index_search",
        description="Search for MCP tools by description/capability",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "category": {
                    "type": "string",
                    "description": "Optional category filter (search, storage, processing, etc.)",
                },
                "limit": {"type": "integer", "default": 20, "description": "Maximum results"},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="mcp_index_get_server",
        description="Get all tools for a specific MCP server",
        inputSchema={
            "type": "object",
            "properties": {
                "server_id": {"type": "string", "description": "Server ID (e.g., 'knowledge-mcp')"}
            },
            "required": ["server_id"],
        },
    ),
    types.Tool(
        name="mcp_index_get_tool",
        description="Get detailed information about a specific tool",
        inputSchema={
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "description": "Tool name (e.g., 'kb_search')"}
            },
            "required": ["tool_name"],
        },
    ),
    types.Tool(
        name="mcp_index_rebuild",
        description="Force rebuild of entire MCP index (same as mcp_index_scan)",
        inputSchema={"type": "object", "properties": {}},
    ),
    # ================================================================
    # SEARCH TOOLS (consolidated from search-mcp)
    # ================================================================
    types.Tool(
        name="search_local",
        description="Search local files by content (lexical mode)",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths to search (defaults: learning, xtts, knowledge)",
                },
                "file_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File extensions to search (default: txt, json, md, py, yaml)",
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="search_corpora",
        description="Search across corpus manifests (JSONL files)",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "corpus_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific corpus IDs to search (optional)",
                },
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="search_transcripts",
        description="Search transcript segments from Whisper outputs",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "speaker": {"type": "string", "description": "Filter by speaker (optional)"},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="multi_search",
        description=(
            "Search across all configured sources simultaneously (KB, local files, "
            "transcripts, corpora) with a single query. Returns combined results from "
            "all available sources. For searching within a specific source only, use "
            "kb_search, search_local, search_transcripts, or search_corpora instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
            "required": ["query"],
        },
    ),
    types.Tool(
        name="deduplicate_results",
        description="Remove duplicate search results based on text similarity",
        inputSchema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Array of search result objects",
                },
                "threshold": {
                    "type": "number",
                    "description": "Similarity threshold (0-1, default: 0.9)",
                },
            },
            "required": ["results"],
        },
    ),
    types.Tool(
        name="cluster_results",
        description=(
            "Groups search results by source_type (e.g. file extension, corpus, "
            "transcript). The cluster count is determined by the data, not by a "
            "parameter — see Issue #22 for why num_clusters/n_clusters were removed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Array of search result objects",
                },
            },
            "required": ["results"],
        },
    ),
    # ================================================================
    # RETRIEVAL TELEMETRY TOOLS (3) - Issue #5 Phase 2
    # No-op (ok=True, data.skipped=true) unless LORE_HARD_NEGATIVE_MINING=true
    # on a PostgreSQL backend.
    # ================================================================
    types.Tool(
        name="log_retrieval_feedback",
        description=(
            "Score or annotate a prior kb_search result by its query_id "
            "(retrieval telemetry, issue #5). Supply user_feedback_score, "
            "required_requery, notes, or any combination; an omitted field is "
            "left unchanged (cannot be reset to null). No effect unless "
            "LORE_HARD_NEGATIVE_MINING=true on a PostgreSQL backend."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query_id": {
                    "type": "string",
                    "description": "query_id returned by a prior kb_search call.",
                },
                "user_feedback_score": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": (
                        "Integer relevance/quality score for the retrieval (1-5). "
                        "Optional; omit to leave unchanged."
                    ),
                },
                "required_requery": {
                    "type": "boolean",
                    "description": (
                        "True if the user had to refine/repeat their query "
                        "because the results were insufficient. Optional; omit "
                        "to leave unchanged."
                    ),
                },
                "notes": {
                    "type": "string",
                    "description": (
                        "Free-text note about the retrieval (truncated at 4000 "
                        "chars). Optional; omit to leave unchanged."
                    ),
                },
            },
            "required": ["query_id"],
        },
    ),
    types.Tool(
        name="get_retrieval_telemetry",
        description=(
            "Read retrieval telemetry rows (issue #5). Selector precedence: "
            "query_id > session_id > topic > recent. Returns newest-first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query_id": {
                    "type": "string",
                    "description": "Return the single row for this query_id.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Return rows for this session_id (newest-first).",
                },
                "topic": {
                    "type": "string",
                    "description": "Return rows for this topic (newest-first).",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 500,
                    "description": (
                        "Max rows for session_id/topic/recent selectors "
                        "(default 50, clamped to 500). Ignored for query_id."
                    ),
                },
            },
        },
    ),
    types.Tool(
        name="get_telemetry_stats",
        description=(
            "Aggregate retrieval telemetry stats (issue #5): totals, feedback "
            "coverage, requery count, average score, oldest/newest timestamps. "
            "Optionally scoped by session_id and/or topic."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Restrict stats to this session_id (optional).",
                },
                "topic": {
                    "type": "string",
                    "description": "Restrict stats to this topic (optional).",
                },
            },
        },
    ),
    # Hard Negative Mining Tools (Issue #5 Phase 3) (2)
    types.Tool(
        name="refresh_hard_negatives",
        description=(
            "Scan retrieval_telemetry for low-scored and requery signals, then "
            "upsert hard negative (query, document) pairs into "
            "knowledge.hard_negative_pairs (issue #5). Use since= (ISO timestamp) "
            "for an incremental refresh; dry_run=true returns counts without "
            "writing. Requires LORE_HARD_NEGATIVE_MINING=true on a PostgreSQL "
            "backend."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": (
                        "ISO timestamp; only mine telemetry created after this. "
                        "Omit for a full refresh."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Return counts without persisting any pairs.",
                },
            },
        },
    ),
    types.Tool(
        name="get_hard_negatives",
        description=(
            "Read hard negative (query, document) pairs from "
            "knowledge.hard_negative_pairs (issue #5). Filter by signal_type "
            "(explicit/behavioral/all), doc_id, or query_text_like. Returns pairs "
            "sorted by occurrence_count DESC."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "signal_type": {
                    "type": "string",
                    "enum": ["explicit", "behavioral", "all"],
                    "description": (
                        "Filter by signal type: 'explicit' (low feedback score), "
                        "'behavioral' (required requery), or 'all'. Optional."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "minimum": 1,
                    "maximum": 1000,
                    "description": "Max pairs to return (default 100, clamped to 1000).",
                },
                "doc_id": {
                    "type": "string",
                    "description": "Restrict to pairs for this kb_id (optional).",
                },
                "query_text_like": {
                    "type": "string",
                    "description": "Case-insensitive substring match on query_text (optional).",
                },
            },
        },
    ),
    # Query Embedding Backfill (Issue #5 Phase 4a) (1)
    types.Tool(
        name="backfill_query_embeddings",
        description=(
            "Backfill query_embedding column in retrieval_telemetry for rows "
            "that predate Phase 4a. Processes rows in batches. Set "
            "build_index=true to also create the HNSW index after backfill "
            "(runs CONCURRENTLY)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "batch_size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,  # lower than kb_backfill (512) — embedding batches include ONNX inference overhead
                    "description": "Rows per batch (1–200, default 32)",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10000,
                    "description": "Max rows to process (1–10000, default 1000)",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, compute but do not write (default false)",
                },
                "build_index": {
                    "type": "boolean",
                    "description": "If true, CREATE INDEX CONCURRENTLY after backfill (default false)",
                },
            },
            "required": [],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    """List all available tools."""
    return _TOOL_DEFINITIONS


# Pre-built schema lookup table for O(1) access in call_tool().
_TOOL_SCHEMA_MAP: dict[str, dict] = {t.name: t.inputSchema for t in _TOOL_DEFINITIONS}


def _get_tool_schema(name: str) -> dict:
    """Return the inputSchema for a named tool, or empty dict if not found."""
    return _TOOL_SCHEMA_MAP.get(name, {})


@app.call_tool(validate_input=False)
async def call_tool(name: str, arguments: Any) -> list[types.TextContent]:
    """Route tool calls to handlers.

    validate_input is disabled at the framework level so we can coerce
    JSON-stringified array/object values (a Claude Code serialization quirk)
    before running jsonschema validation ourselves.
    """
    try:
        # Coerce JSON-stringified arrays/objects, then validate
        arguments = _coerce_arguments(arguments or {}, _get_tool_schema(name))
        schema = _get_tool_schema(name)
        if schema:
            try:
                jsonschema.validate(instance=arguments, schema=schema)
            except jsonschema.ValidationError as exc:
                return format_response(
                    ResponseEnvelope.error(
                        ErrorCodes.INVALID_INPUT, f"Input validation error: {exc.message}"
                    )
                )

        # KB Tools
        if name == "kb_add":
            return format_response(handle_kb_add(**arguments))
        elif name == "kb_search":
            return format_response(handle_kb_search(**arguments))
        elif name == "kb_get":
            return format_response(handle_kb_get(**arguments))
        elif name == "kb_list":
            return format_response(handle_kb_list(**arguments))
        elif name == "kb_update":
            return format_response(handle_kb_update(**arguments))
        elif name == "kb_delete":
            return format_response(handle_kb_delete(**arguments))

        # Investigations Tools
        elif name == "investigation_add":
            return format_response(handle_investigation_add(**arguments))
        elif name == "investigation_list":
            return format_response(handle_investigation_list(**arguments))
        elif name == "investigation_get":
            return format_response(handle_investigation_get(**arguments))
        elif name == "investigation_log_experiment":
            return format_response(handle_investigation_log_experiment(**arguments))
        elif name == "investigation_list_experiments":
            return format_response(handle_investigation_list_experiments(**arguments))
        elif name == "investigation_delete_note":
            return format_response(handle_investigation_delete_note(**arguments))
        elif name == "investigation_delete_experiment":
            return format_response(handle_investigation_delete_experiment(**arguments))

        # Journal Tools
        elif name == "journal_append":
            return format_response(handle_journal_append(**arguments))
        elif name == "journal_list":
            return format_response(handle_journal_list(**arguments))
        elif name == "journal_get":
            return format_response(handle_journal_get(**arguments))
        elif name == "journal_search":
            return format_response(handle_journal_search(**arguments))
        elif name == "journal_delete":
            return format_response(handle_journal_delete(**arguments))
        elif name == "snapshot_config":
            return format_response(handle_snapshot_config(**arguments))

        # Document Ingestion Tools (v1.3)
        elif name == "kb_ingest_doc":
            return format_response(handle_kb_ingest_doc(**arguments))
        elif name == "kb_ingest_dir":
            return format_response(handle_kb_ingest_dir(**arguments))
        elif name == "kb_sync_status":
            return format_response(handle_kb_sync_status(**arguments))

        # Semantic Search Tools (v0.6)
        elif name == "kb_backfill_embeddings":
            return format_response(handle_kb_backfill_embeddings(**arguments))
        elif name == "kb_embedding_status":
            return format_response(handle_kb_embedding_status(**arguments))

        # MCP Index Tools
        elif name == "mcp_index_scan":
            return format_response(handle_mcp_index_scan(**arguments))
        elif name == "mcp_index_search":
            return format_response(handle_mcp_index_search(**arguments))
        elif name == "mcp_index_get_server":
            return format_response(handle_mcp_index_get_server(**arguments))
        elif name == "mcp_index_get_tool":
            return format_response(handle_mcp_index_get_tool(**arguments))
        elif name == "mcp_index_rebuild":
            return format_response(handle_mcp_index_rebuild(**arguments))

        # Search Tools (consolidated from search-mcp)
        elif name == "search_local":
            return format_response(handle_search_local(**arguments))
        elif name == "search_corpora":
            return format_response(handle_search_corpora(**arguments))
        elif name == "search_transcripts":
            return format_response(handle_search_transcripts(**arguments))
        elif name == "multi_search":
            return format_response(handle_multi_search(**arguments))
        elif name == "deduplicate_results":
            return format_response(handle_deduplicate_results(**arguments))
        elif name == "cluster_results":
            return format_response(handle_cluster_results(**arguments))

        # Retrieval Telemetry Tools (Issue #5 Phase 2)
        elif name == "log_retrieval_feedback":
            return format_response(handle_log_retrieval_feedback(**arguments))
        elif name == "get_retrieval_telemetry":
            return format_response(handle_get_retrieval_telemetry(**arguments))
        elif name == "get_telemetry_stats":
            return format_response(handle_get_telemetry_stats(**arguments))

        # Hard Negative Mining Tools (Issue #5 Phase 3)
        elif name == "refresh_hard_negatives":
            return format_response(handle_refresh_hard_negatives(**arguments))
        elif name == "get_hard_negatives":
            return format_response(handle_get_hard_negatives(**arguments))

        # Query Embedding Backfill (Issue #5 Phase 4a)
        elif name == "backfill_query_embeddings":
            # NOTE: takes a single `params: dict` — intentionally not **arguments (unlike other handlers)
            return format_response(handle_backfill_query_embeddings(arguments))

        else:
            return format_response(
                ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Unknown tool: {name}")
            )
    except Exception as e:
        logger.error(f"Error in {name}: {e}", exc_info=True)
        return format_response(ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e)))


# =============================================================================
# Knowledge Base Handlers
# =============================================================================


def _semantic_write_enabled() -> bool:
    """Whether to embed on the KB write path.

    True iff:
      - ``LORE_SEMANTIC_SEARCH=true``, AND
      - the backend is SQLite with sqlite-vec loaded, OR
      - the backend is local/postgres with pgvector loaded.
    """
    flag = os.getenv("LORE_SEMANTIC_SEARCH", "false").strip().lower()
    if flag != "true":
        return False
    backend = os.getenv("DB_BACKEND", "").strip().lower()
    if backend == "sqlite":
        return bool(getattr(db, "vec_extension_loaded", False))
    if backend in {"local", "postgres", "postgresql"}:
        return bool(getattr(db, "vec_extension_loaded", False))
    return False


def _backend_kind() -> str:
    """Return ``"sqlite"`` | ``"postgres"`` | ``""`` based on DB_BACKEND."""
    backend = os.getenv("DB_BACKEND", "").strip().lower()
    if backend == "sqlite":
        return "sqlite"
    if backend in {"local", "postgres", "postgresql"}:
        return "postgres"
    return ""


def _pg_format_vector_literal(vector: list[float]) -> str:
    """Format a Python list of floats as a pgvector literal string.

    pgvector parses ``"[0.1,0.2,...]"`` for both ``vector`` and ``halfvec``
    types when an explicit cast (``::vector`` / ``::halfvec``) is applied.
    We format the string ourselves so the code path doesn't depend on
    ``pgvector[psycopg2]`` adapter registration at runtime.
    """
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _embed_kb_entry(
    kb_id: str,
    title: str,
    content: str,
    *,
    expected_hash: str | None = None,
) -> tuple[bool, str | None]:
    """Embed a KB entry and upsert into vec0 + meta tables.

    Best-effort: failures are logged but do not raise. Returns ``(ok, content_hash)``.
    When ``expected_hash`` matches the meta row, the embed is skipped and the
    function returns ``(True, expected_hash)``.
    """
    try:
        from lore.embeddings import (
            EMBEDDING_DIM,
            EmbeddingUnavailableError,
            compute_content_hash,
            encode_text,
        )
        from lore.embeddings import _model_name as _embedder_model_name
    except ImportError as exc:
        logger.warning("Embeddings module not importable: %s", exc)
        return False, None

    content_hash = compute_content_hash(title, content)
    if expected_hash is not None and expected_hash == content_hash:
        return True, content_hash

    try:
        vector = encode_text(f"{title}\n\n{content}")
    except EmbeddingUnavailableError as exc:
        logger.warning("Skipping embed for %s: %s", kb_id, exc)
        return False, content_hash
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to encode kb entry %s: %s", kb_id, exc)
        return False, content_hash

    if len(vector) != EMBEDDING_DIM:
        logger.error(
            "Embedding dim mismatch for %s: got %d, expected %d",
            kb_id,
            len(vector),
            EMBEDDING_DIM,
        )
        return False, content_hash

    backend = _backend_kind()
    if backend == "postgres":
        return _persist_embedding_postgres(
            kb_id,
            vector,
            content_hash,
            _embedder_model_name(),
            EMBEDDING_DIM,
        )

    # SQLite (default).
    try:
        import sqlite_vec
    except ImportError as exc:
        logger.warning("sqlite-vec not importable at write time: %s", exc)
        return False, content_hash

    try:
        conn = db._get_connection()
        blob = sqlite_vec.serialize_float32(vector)
        # Upsert into vec0: delete-then-insert; vec0 doesn't support INSERT OR REPLACE.
        conn.execute("DELETE FROM knowledge_kb_vec_embeddings WHERE kb_id = ?", (kb_id,))
        conn.execute(
            "INSERT INTO knowledge_kb_vec_embeddings(kb_id, embedding) VALUES(?, ?)",
            (kb_id, blob),
        )
        conn.execute(
            "INSERT OR REPLACE INTO knowledge_kb_embedding_meta "
            "(kb_id, model_name, embedding_dim, content_hash, updated_at) "
            "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
            (kb_id, _embedder_model_name(), EMBEDDING_DIM, content_hash),
        )
        conn.commit()
        return True, content_hash
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist embedding for %s: %s", kb_id, exc)
        return False, content_hash


def _persist_embedding_postgres(
    kb_id: str,
    vector: list[float],
    content_hash: str,
    model_name: str,
    dims: int,
) -> tuple[bool, str | None]:
    """UPSERT a single embedding into ``knowledge.kb_embeddings`` on PostgreSQL.

    Returns ``(ok, content_hash)``. Best-effort: failures are logged but
    don't raise. Uses ``ON CONFLICT (kb_id) DO UPDATE`` so concurrent writers
    converge on the latest vector.
    """
    if not getattr(db, "vec_extension_loaded", False):
        return False, content_hash

    vt = getattr(db, "vector_type", "vector")
    vec_literal = _pg_format_vector_literal(vector)

    try:
        conn = db._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"""
                INSERT INTO knowledge.kb_embeddings
                    (kb_id, embedding, content_hash, model_name, model_dims)
                VALUES (%s, %s::{vt}, %s, %s, %s)
                ON CONFLICT (kb_id) DO UPDATE SET
                    embedding    = EXCLUDED.embedding,
                    content_hash = EXCLUDED.content_hash,
                    model_name   = EXCLUDED.model_name,
                    model_dims   = EXCLUDED.model_dims,
                    embedded_at  = NOW()
                """,
                (kb_id, vec_literal, content_hash, model_name, int(dims)),
            )
        finally:
            cursor.close()
        return True, content_hash
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist PG embedding for %s: %s", kb_id, exc)
        return False, content_hash


def _delete_kb_embedding(kb_id: str) -> None:
    """Remove embedding row(s) for ``kb_id``. Idempotent / best-effort.

    SQLite: clears the vec0 row and the meta row explicitly because vec0
    doesn't participate in FK cascades.

    PostgreSQL: ``knowledge.kb_embeddings`` has ``ON DELETE CASCADE`` from
    ``kb_entries`` so the row is already gone by the time this runs after
    a successful kb_entries DELETE. The explicit DELETE here is defensive —
    safe to issue even when the cascade already cleared it.
    """
    backend = _backend_kind()
    if not getattr(db, "vec_extension_loaded", False):
        return
    try:
        if backend == "postgres":
            conn = db._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "DELETE FROM knowledge.kb_embeddings WHERE kb_id = %s",
                    (kb_id,),
                )
            finally:
                cursor.close()
            return
        if backend == "sqlite":
            conn = db._get_connection()
            conn.execute("DELETE FROM knowledge_kb_vec_embeddings WHERE kb_id = ?", (kb_id,))
            # FK cascade should clear knowledge_kb_embedding_meta on its own when the
            # KB row is gone — but the row is gone before we get here, so be explicit.
            conn.execute("DELETE FROM knowledge_kb_embedding_meta WHERE kb_id = ?", (kb_id,))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete embedding for %s: %s", kb_id, exc)


def _get_embedding_meta(kb_id: str) -> dict | None:
    """Return embedding metadata for ``kb_id`` or None.

    On SQLite this reads ``knowledge_kb_embedding_meta``. On PostgreSQL it
    reads ``knowledge.kb_embeddings`` (which carries the same metadata
    columns inline with the vector). Returns a dict with stable keys
    regardless of backend: ``kb_id, model_name, embedding_dim, content_hash``
    (plus ``updated_at`` on SQLite / ``embedded_at`` on PG, both surfaced as
    ``updated_at`` for parity).
    """
    if not getattr(db, "vec_extension_loaded", False):
        return None
    backend = _backend_kind()
    try:
        if backend == "postgres":
            conn = db._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "SELECT kb_id, model_name, model_dims, content_hash, "
                    "       created_at, embedded_at "
                    "FROM knowledge.kb_embeddings WHERE kb_id = %s",
                    (kb_id,),
                )
                row = cursor.fetchone()
            finally:
                cursor.close()
            if row is None:
                return None
            return {
                "kb_id": row[0],
                "model_name": row[1],
                "embedding_dim": row[2],
                "content_hash": row[3],
                "created_at": row[4].isoformat() if row[4] is not None else None,
                "updated_at": row[5].isoformat() if row[5] is not None else None,
            }
        if backend == "sqlite":
            conn = db._get_connection()
            cur = conn.execute(
                "SELECT kb_id, model_name, embedding_dim, content_hash, created_at, updated_at "
                "FROM knowledge_kb_embedding_meta WHERE kb_id = ?",
                (kb_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "kb_id": row[0],
                "model_name": row[1],
                "embedding_dim": row[2],
                "content_hash": row[3],
                "created_at": row[4],
                "updated_at": row[5],
            }
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read embedding meta for %s: %s", kb_id, exc)
        return None


def handle_kb_add(
    topic: str,
    title: str,
    content: str,
    tags: list = None,
    author: str = None,
    source_type: str = None,
    trust_score: float = 1.0,
) -> dict:
    """Add KB entry.

    Optional attribution fields (author, source_type) support multi-agent
    provenance tracking. See `verified` flag (set via kb_update) for human review state.

    ``trust_score`` (Issue #14) is a confidence signal in [0.0, 1.0] (default
    1.0). Out-of-range values are rejected with an invalid_input envelope.
    """
    # Issue #14: normalise None -> 1.0 on the add path. _validate_trust_score
    # treats None as "not provided" (a passthrough for the kb_update path), but
    # for kb_add a None would slip past validation and then crash at
    # float(None). Defaulting here keeps the documented default of 1.0.
    if trust_score is None:
        trust_score = 1.0
    # Issue #14: validate the confidence bound before touching the database.
    err = _validate_trust_score(trust_score)
    if err is not None:
        return err
    try:
        kb_id = f"kb_{uuid.uuid4().hex[:12]}"

        entry = {
            "kb_id": kb_id,
            "topic": topic,
            "title": title,
            "content": content,
            # kb_entries.tags is a text[] column. Pass the Python list through
            # raw so the db layer adapts it natively (psycopg2 -> array literal
            # on Postgres, json string on SQLite). json.dumps() here would send
            # '[...]' to text[] and raise "malformed array literal".
            "tags": tags or [],
            "author": author,
            "source_type": source_type,
            # Issue #14: per-entry confidence weight (already validated above).
            "trust_score": float(trust_score),
        }

        db.table("knowledge.kb_entries").insert(entry).execute()

        # Best-effort embed at write time. The KB entry succeeds even if the
        # embedder fails (e.g. model download in flight). Backfill recovers.
        embedded = False
        if _semantic_write_enabled():
            embedded, _ = _embed_kb_entry(kb_id, title, content)

        return ResponseEnvelope.success(
            f"Added KB entry: {title}",
            {
                "kb_id": kb_id,
                "topic": topic,
                "author": author,
                "source_type": source_type,
                "trust_score": float(trust_score),
                "embedded": embedded,
            },
        )
    except Exception as e:
        logger.error(f"Error adding KB entry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def _apply_reranking_penalties(results: list, query_embedding: list, db) -> list:
    """
    Soft-penalise docs that were historically poor matches for similar queries.
    Returns good_docs + bad_docs (bad docs moved to end, not excluded).
    """
    threshold = float(os.environ.get("LORE_RERANKING_THRESHOLD", "0.15"))
    bad_doc_ids = set(telemetry.fetch_reranking_bad_docs(query_embedding, db, threshold))
    if not bad_doc_ids:
        return results
    good = [r for r in results if r.get("kb_id") not in bad_doc_ids]
    bad = [r for r in results if r.get("kb_id") in bad_doc_ids]
    return good + bad


def _finalize_search_response(
    resp_data: dict,
    *,
    query: str,
    topic: str | None,
    session_id: str | None,
    parent_query_id: str | None,
    required_requery: bool,
    caller_agent: str | None,
    query_embedding=None,
) -> dict:
    """Attach a ``query_id`` and fire-and-forget telemetry for one kb_search.

    Called immediately before every ``kb_search`` success return so all result
    paths are covered (Issue #5, Fix 2). When mining is disabled this is a
    near-zero-cost no-op: the ``resp_data`` dict is returned unchanged and no
    telemetry is written. Mutates ``resp_data`` in place and returns it so the
    caller can pass the same object to ``ResponseEnvelope.success``.

    Phase 4 (Issue #5): when mining is enabled the query embedding is captured
    here (best-effort) and threaded into the telemetry write so Phase 4b
    re-ranking has stored embeddings to compare against. Re-ranking itself
    (reordering ``resp_data["results"]``) is applied when LORE_RERANKING_ENABLED
    is set and a query embedding was captured; it never excludes docs, only
    soft-penalises historically-poor ones to the end.
    """
    if not telemetry.mining_enabled():
        return resp_data

    from lore import __version__ as _lore_version

    qid = telemetry.generate_query_id()
    resp_data["query_id"] = qid

    # Phase 4a: capture query embedding for telemetry (best-effort).
    if query_embedding is None:
        try:
            from .embeddings import encode_text

            query_embedding = encode_text(query)
        except Exception:  # noqa: BLE001 — embedding capture is best-effort
            query_embedding = None

    # Phase 4b: soft-penalise historically-poor docs for similar queries.
    if telemetry.reranking_enabled() and resp_data.get("results") and query_embedding is not None:
        resp_data["results"] = _apply_reranking_penalties(resp_data["results"], query_embedding, db)

    results = resp_data.get("results") or []
    doc_ids = [r.get("kb_id") for r in results if r.get("kb_id")]

    telemetry.write_retrieval_telemetry_async(
        query_id=qid,
        query_text=query,
        topic=topic,
        search_mode=resp_data.get("search_mode"),
        retrieved_document_ids=doc_ids,
        result_count=resp_data.get("count", 0),
        session_id=session_id,
        parent_query_id=parent_query_id,
        required_requery=required_requery,
        caller_agent=caller_agent,
        model_version=_lore_version,
        db=db,
        query_embedding=query_embedding,
    )
    return resp_data


def _filter_by_min_score(
    results: list[dict], search_mode: str, min_score: float | None
) -> list[dict]:
    """Drop results scoring below ``min_score`` (Issue #16).

    Pure query-layer filter applied after results are fetched and scored.
    ``hybrid`` mode filters on each result's ``rrf_score``; ``fts`` and
    ``semantic`` modes filter on ``score``. Missing scores default to 0.0.
    When ``min_score`` is None this is a no-op (returns the list unchanged),
    preserving backward compatibility.
    """
    if min_score is None:
        return results
    score_field = "rrf_score" if search_mode == "hybrid" else "score"
    filtered: list[dict] = []
    for r in results:
        if score_field not in r:
            logger.debug(
                "result missing expected score field %r, defaulting to 0.0: %s",
                score_field,
                r,
            )
        if r.get(score_field, 0.0) >= min_score:
            filtered.append(r)
    return filtered


def _validate_trust_score(trust_score: float | None) -> dict | None:
    """Validate a ``trust_score`` is a number in [0.0, 1.0] (Issue #14).

    Returns ``None`` when valid (including when ``trust_score`` is ``None``,
    which means "not provided" for the update path). Otherwise returns an
    ``invalid_input`` ResponseEnvelope the caller should return verbatim.
    """
    if trust_score is None:
        return None
    # Issue #14: bool is a subclass of int, so float(True)==1.0 / float(False)==0.0
    # would silently pass. Reject booleans explicitly before coercion.
    if isinstance(trust_score, bool):
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            "trust_score must be a float, not bool",
        )
    try:
        value = float(trust_score)
    except (TypeError, ValueError):
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            "trust_score must be a number between 0.0 and 1.0",
        )
    if not (0.0 <= value <= 1.0):
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            "trust_score must be between 0.0 and 1.0",
        )
    return None


def _filter_by_min_trust_score(results: list[dict], min_trust_score: float | None) -> list[dict]:
    """Drop results whose ``trust_score`` is below ``min_trust_score`` (Issue #14).

    Pure query-layer filter applied after results are fetched. A missing
    ``trust_score`` defaults to 1.0 so that legacy rows (created before the
    column existed) are never excluded — they are treated as fully trusted.
    When ``min_trust_score`` is None this is a no-op (returns the list
    unchanged), preserving backward compatibility.
    """
    if min_trust_score is None:
        return results
    return [r for r in results if r.get("trust_score", 1.0) >= min_trust_score]


def handle_kb_search(
    query: str,
    topic: str = None,
    top_k: int = 20,
    semantic: bool = False,
    hybrid: bool = False,
    search_mode: str = None,
    session_id: str = None,
    parent_query_id: str = None,
    required_requery: bool = False,
    caller_agent: str = None,
    min_score: float = None,
    min_trust_score: float = None,
) -> dict:
    """Search KB entries.

    Routing (in order):
      1. If ``search_mode`` is given, use it verbatim ("fts" | "semantic" | "hybrid").
      2. Else if ``semantic=True``, use "semantic".
      3. Else if ``hybrid=True``, use "hybrid".
      4. Else fall back to legacy lexical search (existing FTS / LIKE path).

    Semantic and hybrid modes require ``LORE_SEMANTIC_SEARCH=true`` and the
    [semantic] extras. When unavailable, the call degrades to lexical search.

    ``min_score`` (Issue #16): when set, results scoring below the threshold are
    excluded server-side and ``count`` reflects the post-filter total. Hybrid
    mode filters on ``rrf_score``; fts/semantic filter on ``score``. None (the
    default) disables filtering for full backward compatibility.

    ``min_trust_score`` (Issue #14): when set, results whose ``trust_score`` is
    below the threshold are excluded. Applied before ``min_score`` so the two
    filters compose. A row with no ``trust_score`` defaults to 1.0 (fully
    trusted) for backward compatibility. None (the default) disables the filter.
    """
    try:
        from lore import search as _search  # local import: tolerant of degraded envs

        # Telemetry context shared by all success-return paths (Issue #5, Fix 2).
        # _finalize_search_response is invoked just before each return so every
        # one of the 5 result paths is covered and tagged with a query_id.
        _telemetry_ctx = dict(
            query=query,
            topic=topic,
            session_id=session_id,
            parent_query_id=parent_query_id,
            required_requery=required_requery,
            caller_agent=caller_agent,
        )

        # Determine the requested mode.
        if search_mode in {"fts", "semantic", "hybrid"}:
            requested_mode: str = search_mode
        elif semantic:
            requested_mode = "semantic"
        elif hybrid:
            requested_mode = "hybrid"
        else:
            requested_mode = "fts"

        # Bound top_k defensively (schema already constrains 1..200, but the
        # handler is also invoked from internal callers).
        try:
            top_k_int = int(top_k)
        except (TypeError, ValueError):
            top_k_int = 20
        # BUG-8: top_k below 1 is meaningless (top_k=0 previously returned 1
        # result via the legacy LIKE path's max(top_k, 50) floor). Reject it.
        if top_k_int < 1:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "top_k must be at least 1")
        top_k_int = min(200, top_k_int)

        # Decide whether we can actually use the semantic/hybrid path.
        backend = os.getenv("DB_BACKEND", "").strip().lower()
        is_sqlite = backend == "sqlite"
        is_postgres = backend in {"local", "postgres", "postgresql"}
        wants_vectors = requested_mode in {"semantic", "hybrid"}

        # The SQLite hybrid path needs:
        #   - semantic enabled
        #   - vec0 extension loaded on the connection
        #   - fts5 available for hybrid mode
        #   - an embedder we can call
        sqlite_vectors_ok = (
            is_sqlite and _search.semantic_enabled() and getattr(db, "vec_extension_loaded", False)
        )

        # The PostgreSQL hybrid path needs:
        #   - semantic enabled
        #   - pgvector extension installed + kb_embeddings table reachable
        #   - an embedder we can call
        postgres_vectors_ok = (
            is_postgres
            and _search.semantic_enabled()
            and getattr(db, "vec_extension_loaded", False)
        )

        if wants_vectors and sqlite_vectors_ok:
            from lore.embeddings import EmbeddingUnavailableError, encode_text, get_model_name

            def _encode_query(q: str) -> list[float] | None:
                try:
                    return encode_text(q)
                except EmbeddingUnavailableError as exc:
                    logger.warning("Embedding unavailable, falling back: %s", exc)
                    return None

            effective_mode = requested_mode
            if effective_mode == "hybrid" and not getattr(db, "fts5_available", False):
                effective_mode = "semantic"

            results = _search.hybrid_search_sqlite(
                db,
                query,
                topic=topic,
                top_k=top_k_int,
                search_mode=effective_mode,
                encode_query=_encode_query,
            )
            # Issue #14: drop low-confidence rows first so the two filters compose.
            results = _filter_by_min_trust_score(results, min_trust_score)
            # Issue #16: drop results below the relevance threshold (post-filter).
            results = _filter_by_min_score(results, effective_mode, min_score)
            resp_data: dict = {
                "results": results,
                "count": len(results),
                "search_mode": effective_mode,
                "requested_mode": requested_mode,
            }
            if effective_mode in {"semantic", "hybrid"}:
                resp_data["model"] = get_model_name()
            if effective_mode == "hybrid":
                resp_data["rrf_k"] = _search.rrf_k()
            return ResponseEnvelope.success(
                f"Found {len(results)} KB entries (mode={effective_mode})",
                _finalize_search_response(resp_data, **_telemetry_ctx),
            )

        # PostgreSQL semantic/hybrid path (Phase 2 of Issue #6).
        if wants_vectors and postgres_vectors_ok:
            from lore.embeddings import EmbeddingUnavailableError, encode_text, get_model_name

            def _encode_query_pg(q: str) -> list[float] | None:
                try:
                    return encode_text(q)
                except EmbeddingUnavailableError as exc:
                    logger.warning("Embedding unavailable, falling back: %s", exc)
                    return None

            results = _search.hybrid_search_postgres(
                db,
                query,
                topic=topic,
                top_k=top_k_int,
                search_mode=requested_mode,
                encode_query=_encode_query_pg,
            )
            # Issue #14: drop low-confidence rows first so the two filters compose.
            results = _filter_by_min_trust_score(results, min_trust_score)
            # Issue #16: drop results below the relevance threshold (post-filter).
            results = _filter_by_min_score(results, requested_mode, min_score)
            resp_data = {
                "results": results,
                "count": len(results),
                "search_mode": requested_mode,
                "requested_mode": requested_mode,
                "backend": "postgres",
            }
            if requested_mode in {"semantic", "hybrid"}:
                resp_data["model"] = get_model_name()
            if requested_mode == "hybrid":
                resp_data["rrf_k"] = _search.rrf_k()
            return ResponseEnvelope.success(
                f"Found {len(results)} KB entries (mode={requested_mode})",
                _finalize_search_response(resp_data, **_telemetry_ctx),
            )

        # SQLite FTS5-only fast path (no embeddings required).
        if is_sqlite and requested_mode == "fts" and getattr(db, "fts5_available", False):
            results = _search.fts5_search_sqlite(db, query, topic, top_k_int)
            # Strip content from response (consistent with hybrid path).
            results = [{k: v for k, v in r.items() if k != "content"} for r in results]
            # Issue #14: drop low-confidence rows first so the two filters compose.
            results = _filter_by_min_trust_score(results, min_trust_score)
            # Issue #16: drop results below the relevance threshold (post-filter).
            results = _filter_by_min_score(results, "fts", min_score)
            resp_data = {
                "results": results,
                "count": len(results),
                "search_mode": "fts",
                "requested_mode": requested_mode,
            }
            return ResponseEnvelope.success(
                f"Found {len(results)} KB entries (mode=fts)",
                _finalize_search_response(resp_data, **_telemetry_ctx),
            )

        # PostgreSQL FTS-only path (uses the existing GIN index, no embeddings required).
        if is_postgres and requested_mode == "fts":
            pg_rows = _search.fts_search_postgres(db, query, topic, top_k_int)
            pg_rows = [{k: v for k, v in r.items() if k != "content"} for r in pg_rows]
            # Issue #14: drop low-confidence rows first so the two filters compose.
            pg_rows = _filter_by_min_trust_score(pg_rows, min_trust_score)
            # Issue #16: drop results below the relevance threshold (post-filter).
            pg_rows = _filter_by_min_score(pg_rows, "fts", min_score)
            resp_data = {
                "results": pg_rows,
                "count": len(pg_rows),
                "search_mode": "fts",
                "requested_mode": requested_mode,
                "backend": "postgres",
            }
            return ResponseEnvelope.success(
                f"Found {len(pg_rows)} KB entries (mode=fts)",
                _finalize_search_response(resp_data, **_telemetry_ctx),
            )

        # Legacy lexical search path. Preserves today's behavior on SQLite (LIKE
        # via SqliteTableQuery.or_) and PostgreSQL (websearch_to_tsquery).
        safe_query = _sanitize_search_query(query)
        tsquery_safe = _sanitize_search_query(query).replace(" ", " & ")

        query_builder = db.table("knowledge.kb_entries").select(
            "kb_id, topic, title, tags, author, source_type, verified, trust_score"
        )

        if topic:
            query_builder = query_builder.eq("topic", topic)

        try:
            query_builder = query_builder.or_(f"title.wfts.{safe_query},content.wfts.{safe_query}")
        except Exception as fts_error:
            logger.warning(f"Websearch FTS failed, using plain FTS: {fts_error}")
            query_builder = query_builder.or_(
                f"title.plfts.{tsquery_safe},content.plfts.{tsquery_safe}"
            )

        result = query_builder.limit(max(top_k_int, 50)).execute()

        # Issue #14: drop low-confidence rows first so the two filters compose.
        lexical_rows = _filter_by_min_trust_score(list(result.data), min_trust_score)
        # Issue #16: drop results below the relevance threshold (post-filter).
        # The legacy lexical path carries no per-row score, so any positive
        # min_score excludes everything (score defaults to 0.0); min_score=0.0
        # and min_score=None both pass all rows through.
        lexical_results = _filter_by_min_score(lexical_rows, "fts", min_score)

        envelope_data = {
            "results": lexical_results,
            "count": len(lexical_results),
            "search_mode": "fts",
            "requested_mode": requested_mode,
        }
        if wants_vectors and not (sqlite_vectors_ok or postgres_vectors_ok):
            envelope_data["degraded"] = True
            envelope_data["degraded_reason"] = (
                "semantic search unavailable (LORE_SEMANTIC_SEARCH=false, "
                "vector extension not loaded, or embedder missing); "
                "served lexical results"
            )

        return ResponseEnvelope.success(
            f"Found {len(lexical_results)} KB entries",
            _finalize_search_response(envelope_data, **_telemetry_ctx),
        )
    except Exception as e:
        logger.error(f"Error searching KB: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Retrieval Telemetry Analysis Handlers (Issue #5, Phase 2)
#
# All three are hard no-ops when mining is disabled (ok=True, data.skipped=true)
# so callers on SQLite/Supabase or with the flag off get a clean, non-error
# response. Reads open a fresh connection per call inside telemetry.py.
# =============================================================================


def handle_log_retrieval_feedback(
    query_id: str,
    user_feedback_score: int = None,
    notes: str = None,
    required_requery: bool = None,
) -> dict:
    """Score / annotate a prior kb_search result by its ``query_id``.

    Partial update: supply ``user_feedback_score``, ``required_requery``,
    ``notes``, or any combination; an omitted field is left unchanged (cannot be
    reset to NULL). When all are ``None`` this is a no-op (no DB round-trip).
    Returns NOT_FOUND when the ``query_id`` does not exist.

    ``required_requery`` (BUG-3) flags that the user had to refine/repeat the
    query because the results were insufficient. MCP clients may send it as a
    string ("true"/"false"/"1"); it is coerced to a bool here.
    """
    try:
        # BUG-7: reject out-of-range scores before any DB work so a bad value
        # can never corrupt avg_feedback_score in the stats aggregate.
        if user_feedback_score is not None:
            try:
                score_int = int(user_feedback_score)
            except (TypeError, ValueError):
                return ResponseEnvelope.error(
                    ErrorCodes.INVALID_INPUT,
                    "user_feedback_score must be between 1 and 5",
                )
            if not 1 <= score_int <= 5:
                return ResponseEnvelope.error(
                    ErrorCodes.INVALID_INPUT,
                    "user_feedback_score must be between 1 and 5",
                )
            user_feedback_score = score_int

        if not telemetry.mining_enabled():
            return ResponseEnvelope.success(
                "Hard negative mining disabled; feedback not recorded",
                {"skipped": True, "query_id": query_id},
            )

        # MCP clients may send booleans as strings; coerce to a real bool/None.
        if isinstance(required_requery, str):
            required_requery = required_requery.strip().lower() in ("true", "1", "yes")

        # Early no-op: nothing to update, so skip the vacuous COALESCE round-trip.
        if user_feedback_score is None and notes is None and required_requery is None:
            return ResponseEnvelope.success(
                "No feedback fields supplied; nothing to update",
                {"noop": True, "query_id": query_id},
            )

        rows_affected = telemetry.update_retrieval_feedback(
            query_id=query_id,
            user_feedback_score=user_feedback_score,
            required_requery=required_requery,
            notes=notes,
            db=db,
        )
        if rows_affected is None:
            return ResponseEnvelope.error(
                ErrorCodes.UNEXPECTED_EXCEPTION, "Telemetry backend unavailable"
            )
        if rows_affected == 0:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND, f"No telemetry row for query_id: {query_id}"
            )
        return ResponseEnvelope.success(
            f"Recorded feedback for {query_id}",
            {"query_id": query_id, "updated": rows_affected},
        )
    except Exception as e:
        logger.error(f"Error logging retrieval feedback: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_get_retrieval_telemetry(
    query_id: str = None,
    session_id: str = None,
    topic: str = None,
    limit: int = 50,
) -> dict:
    """Read telemetry rows by query_id, session_id, topic, or recent.

    Selector precedence: query_id > session_id > topic > recent. Returns up to
    ``limit`` rows (clamped to ``MAX_READ_LIMIT``) newest-first; an empty match
    is a success with ``count=0`` (not an error).
    """
    try:
        if not telemetry.mining_enabled():
            return ResponseEnvelope.success(
                "Hard negative mining disabled; no telemetry available",
                {"skipped": True},
            )

        rows = telemetry.fetch_retrieval_telemetry(
            query_id=query_id,
            session_id=session_id,
            topic=topic,
            limit=limit,
            db=db,
        )
        rows = rows or []
        if query_id is not None and len(rows) == 0:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND, f"No telemetry row for query_id: {query_id}"
            )
        return ResponseEnvelope.success(
            f"Found {len(rows)} telemetry rows",
            {"rows": rows, "count": len(rows)},
        )
    except Exception as e:
        logger.error(f"Error fetching retrieval telemetry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_get_telemetry_stats(session_id: str = None, topic: str = None) -> dict:
    """Aggregate telemetry stats, optionally scoped by session_id and/or topic."""
    try:
        if not telemetry.mining_enabled():
            return ResponseEnvelope.success(
                "Hard negative mining disabled; no telemetry available",
                {"skipped": True},
            )

        stats = telemetry.fetch_telemetry_stats(session_id=session_id, topic=topic, db=db)
        if stats is None:
            return ResponseEnvelope.error(
                ErrorCodes.UNEXPECTED_EXCEPTION, "Telemetry backend unavailable"
            )
        return ResponseEnvelope.success("Telemetry stats", {"stats": stats})
    except Exception as e:
        logger.error(f"Error fetching telemetry stats: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Hard Negative Mining Handlers (Issue #5, Phase 3)
#
# refresh gates on mining_enabled() (write path); get does NOT (it reads
# historical pairs even when mining is currently off). Both delegate to
# telemetry.py, which opens a fresh connection per call and returns None for a
# non-PostgreSQL backend.
# =============================================================================


def handle_refresh_hard_negatives(since=None, dry_run=False):
    try:
        if not telemetry.mining_enabled():
            return ResponseEnvelope.success(
                "Hard negative mining disabled; no refresh performed",
                {"skipped": True, "reason": "mining_disabled"},
            )
        result = telemetry.refresh_hard_negative_pairs(since=since, dry_run=bool(dry_run), db=db)
        if result is None:
            return ResponseEnvelope.error(
                ErrorCodes.UNEXPECTED_EXCEPTION, "Telemetry backend unavailable"
            )
        msg = f"{'[dry-run] ' if dry_run else ''}Processed {result['processed_telemetry_rows']} telemetry rows"
        return ResponseEnvelope.success(msg, result)
    except Exception as e:
        logger.error(f"Error refreshing hard negatives: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_get_hard_negatives(signal_type=None, limit=None, doc_id=None, query_text_like=None):
    try:
        # Note: get_hard_negatives does NOT gate on mining_enabled() —
        # it reads historical pairs even when mining is currently off
        # BUG-9: use explicit None check so limit=0 is clamped to 1 rather than
        # falling back to DEFAULT_HN_LIMIT (0 is falsy; `0 or default` gave 100).
        if limit is None:
            limit = telemetry.DEFAULT_HN_LIMIT
        limit = max(1, min(telemetry.MAX_HN_LIMIT, int(limit)))
        if signal_type and signal_type not in ("explicit", "behavioral", "all"):
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT, "signal_type must be 'explicit', 'behavioral', or 'all'"
            )
        effective_signal = None if signal_type == "all" else signal_type
        pairs = telemetry.fetch_hard_negatives(
            signal_type=effective_signal,
            limit=limit,
            doc_id=doc_id,
            query_text_like=query_text_like,
            db=db,
        )
        if pairs is None:
            return ResponseEnvelope.error(
                ErrorCodes.UNEXPECTED_EXCEPTION, "Telemetry backend unavailable"
            )
        return ResponseEnvelope.success(
            f"Found {len(pairs)} hard negative pair(s)", {"pairs": pairs, "count": len(pairs)}
        )
    except Exception as e:
        logger.error(f"Error fetching hard negatives: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_backfill_query_embeddings(params: dict) -> dict:
    """Backfill the Phase 4a query_embedding column for older telemetry rows.

    Gated on mining_enabled() (write path). Bounds are clamped defensively:
    batch_size to [1, 200], limit to [1, 10000]. With build_index=true the HNSW
    index is created CONCURRENTLY after a successful (non-dry-run) backfill.
    """
    batch_size = int(params.get("batch_size", 32))
    limit = int(params.get("limit", 1000))
    dry_run = bool(params.get("dry_run", False))
    build_index = bool(params.get("build_index", False))

    if not telemetry.mining_enabled():
        return ResponseEnvelope.error(
            ErrorCodes.NOT_CONFIGURED,
            "Hard negative mining is not enabled (LORE_HARD_NEGATIVE_MINING not set)",
        )

    from .embeddings import encode_text

    result = telemetry.backfill_query_embeddings(
        db,
        encode_fn=encode_text,
        batch_size=max(1, min(batch_size, 200)),
        limit=max(1, min(limit, 10000)),
        dry_run=dry_run,
        build_index=build_index,
    )
    return ResponseEnvelope.success("Backfill complete", result)


def handle_kb_get(kb_id: str) -> dict:
    """Get KB entry details."""
    try:
        result = (
            db.table("knowledge.kb_entries").select("*").eq("kb_id", kb_id).maybe_single().execute()
        )

        if not result or not result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {kb_id}")

        return ResponseEnvelope.success(f"KB entry: {result.data['title']}", result.data)
    except Exception as e:
        logger.error(f"Error getting KB entry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_list(topic: str = None, limit: int = 100, offset: int = 0, **kwargs) -> dict:
    """List KB entries with pagination.

    ``limit`` is clamped to [1, 500] (default 100) and ``offset`` to [0, ∞)
    (default 0). ``total_count`` reflects all matching rows (ignoring
    pagination) so callers can detect further pages via ``has_more``.

    Unknown keyword arguments (e.g. a tool-caller hallucinating a filter such
    as ``created_at__gte``) are caught here and returned as a clean
    invalid_input envelope. Using **kwargs rather than additionalProperties:
    False in the schema avoids FastMCP rejecting the request at the transport
    layer (which produces a raw -32603 error). Returning a graceful envelope —
    instead of letting ``handle_kb_list(**arguments)`` raise a TypeError that
    leaks as a tool execution error — prevents the caller's circuit breaker
    from marking the whole MCP server unreachable (mirrors the kb_update BUG-1
    fix).
    """
    if kwargs:
        unsupported = ", ".join(sorted(kwargs))
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            f"kb_list does not accept: {unsupported}. Supported params: topic, limit, offset.",
        )
    try:
        limit = max(1, min(500, int(limit)))
        offset = max(0, int(offset))

        query = db.table("knowledge.kb_entries").select(
            "kb_id, topic, title, tags, author, source_type, verified, trust_score, created_at",
            count="exact",
        )

        if topic:
            query = query.eq("topic", topic)

        query = query.order("created_at", desc=True)

        result = query.limit(limit).offset(offset).execute()

        results = result.data or []
        if result.count is None and len(results) == limit:
            logger.warning(
                "kb_list: count unavailable and full page returned — has_more may be incorrect"
            )
        total_count = result.count if result.count is not None else len(results)
        has_more = (offset + len(results)) < total_count

        return ResponseEnvelope.success(
            f"Found {len(results)} KB entries",
            {
                "entries": results,
                "count": len(results),
                "limit": limit,
                "offset": offset,
                "total_count": total_count,
                "has_more": has_more,
            },
        )
    except Exception as e:
        logger.error(f"Error listing KB entries: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


_VERIFIED_SENTINEL = object()


def handle_kb_update(
    kb_id: str = None,
    content: str = None,
    title: str = None,
    tags: list = None,
    topic: str = None,
    verified: Any = _VERIFIED_SENTINEL,
    trust_score: float = None,
    entry_id: str = None,
    **kwargs,
) -> dict:
    """Update existing KB entry with partial updates support.

    Updates only the provided fields, preserving existing fields not specified.
    Re-embeds content if title/content changes. Updates updated_at timestamp.

    The entry is identified by ``kb_id`` (preferred, matching the rest of the
    KB API); ``entry_id`` is accepted as a deprecated fallback (BUG-3).

    `verified` accepts True (human-verified), False (disputed), or None (reset to
    unreviewed). Omit the argument entirely to leave the verified state unchanged.

    ``trust_score`` (Issue #14) accepts a number in [0.0, 1.0]. Omit it (leave
    None) to leave the existing confidence unchanged; out-of-range values are
    rejected with an invalid_input envelope.
    """
    import psycopg2

    # Issue #14: validate the confidence bound when provided (None = unchanged).
    err = _validate_trust_score(trust_score)
    if err is not None:
        return err

    # BUG-1: catch unsupported fields (e.g. the removed `metadata`) at the
    # handler level and return a clean invalid_input envelope. Using **kwargs
    # rather than additionalProperties:False in the schema avoids FastMCP
    # rejecting the request at the transport layer (which produces -32603).
    if kwargs:
        unsupported = ", ".join(sorted(kwargs))
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            f"Unsupported field(s): {unsupported}. Use title, content, topic, tags, "
            "verified, or trust_score.",
        )

    # BUG-3: accept kb_id as the primary param name; fall back to entry_id.
    entry_id = kb_id or entry_id
    if not entry_id:
        return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "kb_id is required")
    try:
        # First, verify the entry exists
        existing_result = (
            db.table("knowledge.kb_entries")
            .select("*")
            .eq("kb_id", entry_id)
            .maybe_single()
            .execute()
        )

        if not existing_result or not existing_result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {entry_id}")

        existing_entry = existing_result.data

        # Build update object with only provided fields
        update_data = {}

        if content is not None:
            update_data["content"] = content
            # Note: In a full implementation, you'd regenerate embeddings here
            # when content changes. This is simplified for the basic CRUD operation.

        if title is not None:
            update_data["title"] = title

        if tags is not None:
            # kb_entries.tags is a text[] column — pass the Python list through
            # raw (see handle_kb_add). json.dumps() would raise "malformed array
            # literal" on Postgres.
            update_data["tags"] = tags

        if topic is not None:
            update_data["topic"] = topic

        if verified is not _VERIFIED_SENTINEL:
            # Allow True, False, or explicit None (reset to unreviewed)
            update_data["verified"] = verified

        # Issue #14: only write trust_score when explicitly provided so an
        # omitted value never resets an entry's existing confidence.
        if trust_score is not None:
            update_data["trust_score"] = float(trust_score)

        # Always update the updated_at timestamp
        update_data["updated_at"] = datetime.utcnow().isoformat()

        # Only proceed with update if there are fields to update beyond timestamp
        if set(update_data.keys()) == {"updated_at"}:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "No fields provided for update. Specify content, title, tags, topic, "
                "verified, and/or trust_score.",
            )

        # Perform the update. BUG-1: an unknown/unsupported column reaching the
        # SQL UPDATE raises psycopg2.ProgrammingError (e.g. UndefinedColumn);
        # translate it into a clean invalid_input response instead of leaking
        # the raw SQL error as an unexpected_exception.
        try:
            db.table("knowledge.kb_entries").update(update_data).eq("kb_id", entry_id).execute()
        except psycopg2.ProgrammingError:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "Field not supported")

        # Fetch and return the updated entry
        updated_result = (
            db.table("knowledge.kb_entries")
            .select("*")
            .eq("kb_id", entry_id)
            .maybe_single()
            .execute()
        )

        # Re-embed only if title or content changed. We compare against the
        # stored content_hash to avoid wasted encode calls.
        re_embedded = False
        if _semantic_write_enabled() and updated_result and updated_result.data:
            updated_entry = updated_result.data
            new_title = updated_entry.get("title") or existing_entry.get("title") or ""
            new_content = updated_entry.get("content") or existing_entry.get("content") or ""
            meta = _get_embedding_meta(entry_id)
            existing_hash = meta["content_hash"] if meta else None
            re_embedded, _ = _embed_kb_entry(
                entry_id,
                new_title,
                new_content,
                expected_hash=existing_hash,
            )

        updated_fields = list(update_data.keys())
        return ResponseEnvelope.success(
            f"Updated KB entry '{existing_entry['title']}' (fields: {', '.join(updated_fields)})",
            {
                "kb_id": entry_id,
                "updated_fields": updated_fields,
                "entry": updated_result.data,
                "re_embedded": re_embedded,
            },
        )

    except Exception as e:
        logger.error(f"Error updating KB entry {entry_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_delete(kb_id: str = None, confirm: bool = False, entry_id: str = None) -> dict:
    """Delete existing KB entry from database with safety confirmation.

    The entry is identified by ``kb_id`` (preferred, matching the rest of the
    KB API); ``entry_id`` is accepted as a deprecated fallback (BUG-3).

    Requires explicit confirmation for safety. Deletes entry and associated embeddings.
    Returns deleted entry details for audit trail.
    """
    # BUG-3: accept kb_id as the primary param name; fall back to entry_id.
    entry_id = kb_id or entry_id
    try:
        if not entry_id:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "kb_id is required")

        # Safety check: require explicit confirmation
        if not confirm:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "Deletion requires explicit confirmation. Set confirm=True to proceed.",
            )

        # First, verify the entry exists and get its details for audit trail
        existing_result = (
            db.table("knowledge.kb_entries")
            .select("*")
            .eq("kb_id", entry_id)
            .maybe_single()
            .execute()
        )

        if not existing_result or not existing_result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {entry_id}")

        deleted_entry = existing_result.data
        entry_title = deleted_entry.get("title", "Untitled")

        # Delete the entry FIRST. SQLite vec0 tables don't support FK cascade,
        # but the embedding_meta row will cascade via the regular ON DELETE CASCADE
        # since it references knowledge_kb_entries.
        db.table("knowledge.kb_entries").delete().eq("kb_id", entry_id).execute()

        # Then clean up the vec0 row (and meta row defensively, in case PRAGMA
        # foreign_keys is off on this connection).
        _delete_kb_embedding(entry_id)

        return ResponseEnvelope.success(
            f"Deleted KB entry '{entry_title}' ({entry_id})",
            {"kb_id": entry_id, "deleted_entry": deleted_entry},
        )

    except Exception as e:
        logger.error(f"Error deleting KB entry {entry_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Investigation Handlers
# (DB tables `research_notes` and `research_experiments` are unchanged; only the
# MCP tool/handler surface has been renamed to "investigation".)
# =============================================================================


def handle_investigation_add(topic: str, title: str, content: str, tags: list = None) -> dict:
    """Add an investigation entry."""
    try:
        note_id = f"note_{uuid.uuid4().hex[:12]}"

        note = {
            "note_id": note_id,
            "topic": topic,
            "title": title,
            "content": content,
            "tags": tags or [],
        }

        db.table("knowledge.research_notes").insert(note).execute()

        return ResponseEnvelope.success(f"Added investigation entry: {title}", {"note_id": note_id})
    except Exception as e:
        logger.error(f"Error adding investigation entry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_investigation_list(topic: str = None) -> dict:
    """List investigations."""
    try:
        query = (
            db.table("knowledge.research_notes")
            .select("note_id, topic, title, tags, created_at")
            .order("created_at", desc=True)
        )

        if topic:
            query = query.eq("topic", topic)

        result = query.limit(100).execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} investigations",
            {"investigations": result.data, "count": len(result.data)},
        )
    except Exception as e:
        logger.error(f"Error listing investigations: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_investigation_get(note_id: str) -> dict:
    """Get a single investigation entry."""
    try:
        result = (
            db.table("knowledge.research_notes")
            .select("*")
            .eq("note_id", note_id)
            .maybe_single()
            .execute()
        )

        if not result or not result.data:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND, f"Investigation not found: {note_id}"
            )

        return ResponseEnvelope.success(f"Investigation: {result.data['title']}", result.data)
    except Exception as e:
        logger.error(f"Error getting investigation: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_investigation_log_experiment(
    title: str,
    hypothesis: str = None,
    methodology: str = None,
    results: dict = None,
    conclusion: str = None,
) -> dict:
    """Log an experiment within an investigation."""
    try:
        experiment_id = f"exp_{uuid.uuid4().hex[:12]}"

        experiment = {
            "experiment_id": experiment_id,
            "title": title,
            "hypothesis": hypothesis,
            "methodology": methodology,
            "results": results or {},
            "conclusion": conclusion,
        }

        db.table("knowledge.research_experiments").insert(experiment).execute()

        return ResponseEnvelope.success(
            f"Logged investigation experiment: {title}", {"experiment_id": experiment_id}
        )
    except Exception as e:
        logger.error(f"Error logging investigation experiment: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_investigation_list_experiments() -> dict:
    """List investigation experiments."""
    try:
        result = (
            db.table("knowledge.research_experiments")
            .select("experiment_id, title, created_at")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        return ResponseEnvelope.success(
            f"Found {len(result.data)} investigation experiments",
            {"experiments": result.data, "count": len(result.data)},
        )
    except Exception as e:
        logger.error(f"Error listing investigation experiments: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_investigation_delete_note(
    note_id: str = None,
    confirm: bool = False,
    confirm_production: bool = False,
) -> dict:
    """Hard-delete an investigation note by ID (Issue #21).

    Safety contract — mirrors ``kb_delete``:
      * ``confirm=True`` is mandatory; missing confirmation returns a clean
        ``invalid_input`` envelope so agents can recover without a transport error.
      * In ``LORE_ENV=production`` the production guard additionally requires
        ``confirm_production=True``; otherwise a ``production_guard`` envelope
        is returned. This prevents accidental destructive writes.
      * Missing rows return a ``not_found`` envelope, never raise.
    """
    try:
        if not note_id:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "note_id is required")

        if not confirm:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "Deletion requires explicit confirmation. Set confirm=True to proceed.",
            )

        guard = _production_guard("investigation_delete_note", confirm_production)
        if guard is not None:
            return guard

        existing = (
            db.table("knowledge.research_notes")
            .select("*")
            .eq("note_id", note_id)
            .maybe_single()
            .execute()
        )
        if not existing or not existing.data:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND, f"Investigation note not found: {note_id}"
            )

        db.table("knowledge.research_notes").delete().eq("note_id", note_id).execute()

        return ResponseEnvelope.success(
            f"Deleted investigation note {note_id}",
            {"note_id": note_id, "deleted": True},
        )
    except Exception as e:
        logger.error(f"Error deleting investigation note {note_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_investigation_delete_experiment(
    experiment_id: str = None,
    confirm: bool = False,
    confirm_production: bool = False,
) -> dict:
    """Hard-delete an investigation experiment by ID (Issue #21).

    Same confirmation + production-guard + not-found contract as
    ``investigation_delete_note``. See that handler's docstring for the full
    safety semantics.
    """
    try:
        if not experiment_id:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "experiment_id is required")

        if not confirm:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "Deletion requires explicit confirmation. Set confirm=True to proceed.",
            )

        guard = _production_guard("investigation_delete_experiment", confirm_production)
        if guard is not None:
            return guard

        existing = (
            db.table("knowledge.research_experiments")
            .select("*")
            .eq("experiment_id", experiment_id)
            .maybe_single()
            .execute()
        )
        if not existing or not existing.data:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND,
                f"Investigation experiment not found: {experiment_id}",
            )

        db.table("knowledge.research_experiments").delete().eq(
            "experiment_id", experiment_id
        ).execute()

        return ResponseEnvelope.success(
            f"Deleted investigation experiment {experiment_id}",
            {"experiment_id": experiment_id, "deleted": True},
        )
    except Exception as e:
        logger.error(f"Error deleting investigation experiment {experiment_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Journal Handlers
# =============================================================================


def handle_journal_append(entry_type: str, content: str, tags: list = None) -> dict:
    """Append journal entry."""
    try:
        entry_id = f"jrnl_{uuid.uuid4().hex[:12]}"

        entry = {
            "entry_id": entry_id,
            "date": date.today().isoformat(),
            "entry_type": entry_type,
            "content": content,
            "tags": tags or [],
        }

        db.table("knowledge.journal_entries").insert(entry).execute()

        return ResponseEnvelope.success(
            f"Added journal entry ({entry_type})", {"entry_id": entry_id, "date": entry["date"]}
        )
    except Exception as e:
        logger.error(f"Error appending journal: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_journal_list(limit: int = 20) -> dict:
    """List journal entries."""
    try:
        result = (
            db.table("knowledge.journal_entries")
            .select("entry_id, date, entry_type, tags")
            .order("date", desc=True)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )

        return ResponseEnvelope.success(
            f"Found {len(result.data)} journal entries",
            {"entries": result.data, "count": len(result.data)},
        )
    except Exception as e:
        logger.error(f"Error listing journal: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_journal_get(entry_id: str) -> dict:
    """Get journal entry."""
    try:
        result = (
            db.table("knowledge.journal_entries")
            .select("*")
            .eq("entry_id", entry_id)
            .maybe_single()
            .execute()
        )

        if not result or not result.data:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND, f"Journal entry not found: {entry_id}"
            )

        return ResponseEnvelope.success(f"Journal entry from {result.data['date']}", result.data)
    except Exception as e:
        logger.error(f"Error getting journal entry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def _journal_like_score(content: str, query: str) -> float:
    """Heuristic relevance score for the LIKE/ILIKE journal-search fallback.

    The KB FTS5/pgvector paths carry a real per-row score; journal entries have
    no FTS index, so we derive a lightweight score from term-occurrence counts.
    Returns the total number of (case-insensitive) query-term hits in ``content``,
    normalised to a float so the field type matches the FTS paths. A whole-query
    phrase match adds a bonus so phrase hits rank above scattered term hits.
    """
    if not content:
        return 0.0
    haystack = content.lower()
    q = query.lower().strip()
    if not q:
        return 0.0
    score = float(haystack.count(q))  # phrase-match bonus (whole query)
    for term in {t for t in q.split() if t}:
        score += float(haystack.count(term))
    return score


def handle_journal_search(
    query: str,
    limit: int = 20,
    entry_type: str = None,
    date_from: str = None,
    date_to: str = None,
) -> dict:
    """Full-text search across journal entry content (Issue #15).

    Journal entries were previously only date-browsable (``journal_list`` /
    ``journal_get``); this enables content-based recall. Optional ``entry_type``,
    ``date_from`` and ``date_to`` (ISO ``YYYY-MM-DD``) filters narrow results.

    Backend behaviour:
      * **PostgreSQL** — ranked FTS via ``websearch_to_tsquery`` / ``ts_rank_cd``
        over ``content``; degrades to ``ILIKE`` if the FTS query errors.
      * **SQLite / other** — there is no journal FTS5 index, so the query-layer
        ``ILIKE`` (case-insensitive ``LIKE '%query%'``) fallback is used, mirroring
        the kb_search degradation path. A heuristic ``score`` is attached so the
        response shape matches the FTS path.

    Every result includes ``entry_id``, ``date``, ``entry_type``, ``tags``,
    ``content`` and ``score``. ``count`` reflects the number of returned rows.
    """
    try:
        # Validate ISO date filters before applying any filtering. Reject
        # malformed dates up front so callers get an actionable error rather
        # than silently-ignored or backend-specific failures.
        from datetime import date as _date

        if date_from is not None:
            try:
                _date.fromisoformat(date_from)
            except ValueError:
                return ResponseEnvelope.error(
                    ErrorCodes.INVALID_INPUT,
                    f"date_from must be ISO format (YYYY-MM-DD), got: {date_from!r}",
                )

        if date_to is not None:
            try:
                _date.fromisoformat(date_to)
            except ValueError:
                return ResponseEnvelope.error(
                    ErrorCodes.INVALID_INPUT,
                    f"date_to must be ISO format (YYYY-MM-DD), got: {date_to!r}",
                )

        # Bound limit defensively (schema constrains 1..200, but internal callers
        # may bypass the schema). Default 20; reject sub-1 limits.
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            limit_int = 20
        if limit_int < 1:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "limit must be at least 1")
        limit_int = min(200, limit_int)

        backend = os.getenv("DB_BACKEND", "").strip().lower()
        is_postgres = backend in {"local", "postgres", "postgresql"}

        # PostgreSQL ranked FTS path (uses to_tsvector/websearch_to_tsquery over
        # content; degrades to ILIKE on FTS error). Raw SQL because the journal
        # table has no FTS helper in search.py and we want a real rank score.
        if is_postgres:
            rows = _journal_fts_postgres(query, limit_int, entry_type, date_from, date_to)
            return ResponseEnvelope.success(
                f"Found {len(rows)} journal entries",
                {
                    "entries": rows,
                    "count": len(rows),
                    "search_mode": "fts",
                    "backend": "postgres",
                },
            )

        # SQLite / default lexical (ILIKE) path. No journal FTS5 index exists, so
        # this is the documented degradation path (same shape as kb_search LIKE).
        safe_query = _sanitize_search_query(query)

        query_builder = db.table("knowledge.journal_entries").select(
            "entry_id, date, entry_type, tags, content"
        )
        if entry_type:
            query_builder = query_builder.eq("entry_type", entry_type)
        if date_from:
            query_builder = query_builder.gte("date", date_from)
        if date_to:
            query_builder = query_builder.lte("date", date_to)

        # Case-insensitive substring match on content. A blank sanitized query
        # (e.g. query was only operator tokens) matches everything via "%%".
        query_builder = query_builder.ilike("content", f"%{safe_query}%")
        query_builder = query_builder.order("date", desc=True).order("created_at", desc=True)

        result = query_builder.limit(limit_int).execute()
        rows = list(result.data or [])

        # Attach a heuristic score and re-rank by it (descending), preserving the
        # date-ordered fetch as a stable tiebreak. Limit again post-scoring.
        for row in rows:
            row["score"] = _journal_like_score(row.get("content", ""), query)
        rows.sort(key=lambda r: r.get("score", 0.0), reverse=True)
        rows = rows[:limit_int]

        return ResponseEnvelope.success(
            f"Found {len(rows)} journal entries",
            {
                "entries": rows,
                "count": len(rows),
                "search_mode": "ilike",
                "backend": "sqlite",
            },
        )
    except Exception as e:
        logger.error(f"Error searching journal: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def _journal_fts_postgres(
    query: str,
    limit: int,
    entry_type: str = None,
    date_from: str = None,
    date_to: str = None,
) -> list[dict]:
    """PostgreSQL ranked FTS over journal content; degrades to ILIKE on error.

    Returns rows with entry_id, date, entry_type, tags, content and a numeric
    ``score`` (``ts_rank_cd`` for the FTS path, term-count heuristic for the
    ILIKE fallback). Raises nothing on a failed FTS query — it falls back so a
    misconfigured text-search config never breaks search entirely.
    """
    conn = db._get_connection()

    where: list[str] = []
    params: list[Any] = []

    def _append_filters() -> None:
        if entry_type:
            where.append("entry_type = %s")
            params.append(entry_type)
        if date_from:
            where.append("date >= %s")
            params.append(date_from)
        if date_to:
            where.append("date <= %s")
            params.append(date_to)

    # --- FTS attempt -------------------------------------------------------
    try:
        params = [query]
        where = [
            "to_tsvector('english', coalesce(content,'')) @@ websearch_to_tsquery('english', %s)"
        ]
        _append_filters()
        sql = (
            "SELECT entry_id, date, entry_type, tags, content, "
            "       ts_rank_cd(to_tsvector('english', coalesce(content,'')), "
            "                  websearch_to_tsquery('english', %s)) AS score "
            "FROM knowledge.journal_entries "
            "WHERE " + " AND ".join(where) + " ORDER BY score DESC NULLS LAST LIMIT %s"
        )
        # The rank %s is the first placeholder; prepend the query for it.
        fts_params = [query] + params + [int(limit)]
        cursor = conn.cursor()
        try:
            cursor.execute(sql, fts_params)
            col_names = [d[0] for d in cursor.description]
            return [dict(zip(col_names, raw)) for raw in cursor.fetchall()]
        finally:
            cursor.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Journal PG FTS failed for %r, falling back to ILIKE: %s", query, exc)

    # --- ILIKE fallback ----------------------------------------------------
    params = [f"%{query}%"]
    where = ["content ILIKE %s"]
    _append_filters()
    sql = (
        "SELECT entry_id, date, entry_type, tags, content "
        "FROM knowledge.journal_entries "
        "WHERE " + " AND ".join(where) + " ORDER BY date DESC, created_at DESC LIMIT %s"
    )
    params.append(int(limit))
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        col_names = [d[0] for d in cursor.description]
        rows = [dict(zip(col_names, raw)) for raw in cursor.fetchall()]
    finally:
        cursor.close()
    for row in rows:
        row["score"] = _journal_like_score(row.get("content", ""), query)
    rows.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return rows


def handle_journal_delete(
    entry_id: str = None,
    confirm: bool = False,
    confirm_production: bool = False,
) -> dict:
    """Hard-delete a journal entry by ID (Issue #21).

    Safety contract mirrors ``kb_delete``:
      * ``confirm=True`` is mandatory; missing confirmation returns a clean
        ``invalid_input`` envelope (never a transport error).
      * In ``LORE_ENV=production`` the production guard additionally requires
        ``confirm_production=True`` and returns a ``production_guard`` envelope
        when the flag is missing. This prevents accidental destructive writes.
      * Missing rows return a ``not_found`` envelope, never raise.
    """
    try:
        if not entry_id:
            return ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, "entry_id is required")

        if not confirm:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "Deletion requires explicit confirmation. Set confirm=True to proceed.",
            )

        guard = _production_guard("journal_delete", confirm_production)
        if guard is not None:
            return guard

        existing = (
            db.table("knowledge.journal_entries")
            .select("*")
            .eq("entry_id", entry_id)
            .maybe_single()
            .execute()
        )
        if not existing or not existing.data:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_FOUND, f"Journal entry not found: {entry_id}"
            )

        db.table("knowledge.journal_entries").delete().eq("entry_id", entry_id).execute()

        return ResponseEnvelope.success(
            f"Deleted journal entry {entry_id}",
            {"entry_id": entry_id, "deleted": True},
        )
    except Exception as e:
        logger.error(f"Error deleting journal entry {entry_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_snapshot_config(config_name: str, config_data: dict) -> dict:
    """Snapshot config as journal entry."""
    try:
        entry_id = f"jrnl_{uuid.uuid4().hex[:12]}"

        content = f"Config snapshot: {config_name}\n\n```yaml\n{yaml.dump(config_data)}\n```"

        entry = {
            "entry_id": entry_id,
            "date": date.today().isoformat(),
            "entry_type": "milestone",
            "content": content,
            "tags": ["config-snapshot", config_name],
        }

        db.table("knowledge.journal_entries").insert(entry).execute()

        return ResponseEnvelope.success(
            f"Snapshotted config: {config_name}", {"entry_id": entry_id}
        )
    except Exception as e:
        logger.error(f"Error snapshotting config: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Document Ingestion Handlers (v1.3)
# =============================================================================


def handle_kb_ingest_doc(
    doc_path: str,
    strategy: str = "chunked",
    chunk_size: int = 2000,
    tags: list[str] = None,
    overwrite: bool = False,
    author: str = None,
    source_type: str = "system",
) -> dict:
    """Ingest single markdown document into KB."""
    try:
        doc_path = Path(doc_path).resolve()
        if not doc_path.exists():
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Document not found: {doc_path}")

        # Read document and extract frontmatter
        processor = DocumentProcessor(chunk_size=chunk_size)
        content, metadata = processor.read_document(str(doc_path))

        # Compute hash for change detection
        doc_hash = processor.compute_hash(content)

        # Check if document already synced
        existing_sync = (
            db.table("knowledge.kb_doc_sync")
            .select("*")
            .eq("doc_path", str(doc_path))
            .maybe_single()
            .execute()
        )

        if (
            existing_sync
            and existing_sync.data
            and existing_sync.data.get("doc_hash") == doc_hash
            and not overwrite
        ):
            return ResponseEnvelope.success(
                f"Document unchanged: {doc_path.name}",
                {
                    "doc_path": str(doc_path),
                    "status": "unchanged",
                    "doc_hash": doc_hash,
                    "kb_ids": existing_sync.data.get("kb_ids", []),
                },
            )

        # Delete old KB entries if overwriting
        if overwrite and existing_sync and existing_sync.data:
            old_kb_ids = existing_sync.data.get("kb_ids", [])
            if old_kb_ids:
                db.table("knowledge.kb_entries").delete().in_("kb_id", old_kb_ids).execute()
                logger.info(f"Deleted {len(old_kb_ids)} old KB entries for {doc_path.name}")

        # Extract topic and title
        topic = metadata.get("topic") or processor.extract_topic_from_path(str(doc_path))
        base_title = processor.generate_title(content, str(doc_path))
        doc_tags = tags or []
        if "tags" in metadata:
            doc_tags.extend(metadata["tags"])

        # Ingest based on strategy
        kb_ids = []

        if strategy == "full":
            # Single KB entry for entire document
            kb_id = f"kb_{uuid.uuid4().hex[:12]}"
            entry = {
                "kb_id": kb_id,
                "topic": topic,
                "title": base_title,
                "content": content,
                "tags": doc_tags,
                "source_doc": str(doc_path),
                "source_section": None,
                "line_range": [1, len(content.split("\n"))],
                "author": author,
                "source_type": source_type,
            }
            db.table("knowledge.kb_entries").insert(entry).execute()
            kb_ids.append(kb_id)

        elif strategy == "chunked":
            # Split by sections
            chunks = processor.chunk_by_sections(content, chunk_size)
            for i, chunk in enumerate(chunks):
                kb_id = f"kb_{uuid.uuid4().hex[:12]}"
                title = (
                    f"{base_title} - {chunk.section}"
                    if chunk.section
                    else f"{base_title} (part {i + 1})"
                )
                entry = {
                    "kb_id": kb_id,
                    "topic": topic,
                    "title": title,
                    "content": chunk.content,
                    "tags": doc_tags,
                    "source_doc": str(doc_path),
                    "source_section": chunk.section,
                    "line_range": [chunk.line_start, chunk.line_end],
                    "author": author,
                    "source_type": source_type,
                }
                db.table("knowledge.kb_entries").insert(entry).execute()
                kb_ids.append(kb_id)

        elif strategy == "summary":
            # Lore is intentionally LLM-free: summary generation requires an LLM
            # and belongs to the caller/scheduler, not the knowledge layer. The
            # caller should produce the summary and store it via kb_add or the
            # 'full'/'chunked' strategies. See issue #19 for the documented
            # caller/scheduler-owned workflow.
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_ARGUMENT,
                "Summary strategy is not provided by Lore (it is LLM-free). "
                "Generate the summary in the caller and store it via 'full' or "
                "'chunked'. See issue #19.",
            )

        # Update sync tracking.
        # kb_doc_sync.kb_ids is a jsonb column — serialize the Python list to a
        # JSON string so psycopg2 sends '[...]' rather than a PostgreSQL ARRAY
        # literal '{...}', which raises a jsonb type-mismatch on insert/update.
        sync_data = {
            "doc_path": str(doc_path),
            "doc_hash": doc_hash,
            "kb_ids": json.dumps(kb_ids),
            "last_synced_at": datetime.utcnow().isoformat(),
            "last_modified_at": datetime.fromtimestamp(doc_path.stat().st_mtime).isoformat(),
            "strategy": strategy,
            "metadata": metadata,
        }

        if existing_sync and existing_sync.data:
            db.table("knowledge.kb_doc_sync").update(sync_data).eq(
                "doc_path", str(doc_path)
            ).execute()
            status = "updated"
        else:
            db.table("knowledge.kb_doc_sync").insert(sync_data).execute()
            status = "created"

        return ResponseEnvelope.success(
            f"Ingested {doc_path.name}: {len(kb_ids)} KB entries {status}",
            {
                "doc_path": str(doc_path),
                "kb_entries_created": len(kb_ids),
                "kb_ids": kb_ids,
                "doc_hash": doc_hash,
                "status": status,
            },
        )

    except Exception as e:
        logger.error(f"Error ingesting document {doc_path}: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


async def handle_kb_ingest_dir(
    dir_path: str,
    pattern: str = "*.md",
    strategy: str = "chunked",
    recursive: bool = True,
    exclude_patterns: list[str] = None,
    author: str = None,
    source_type: str = "system",
    confirm_production: bool = False,
) -> dict:
    """Batch ingest directory (5x faster with async/await).

    When ``LORE_ENV=production`` (or unset) and the match set exceeds
    ``_INGEST_DIR_GUARD_THRESHOLD`` files, ``confirm_production=True`` is
    required to guard against accidental bulk writes against the wrong
    environment.
    """
    import asyncio

    USE_ASYNC_INGESTION = os.getenv("ENABLE_ASYNC_INGESTION", "true").lower() == "true"

    try:
        dir_path = Path(dir_path).resolve()
        if not dir_path.exists():
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Directory not found: {dir_path}")

        # Find all matching files
        if recursive:
            files = list(dir_path.rglob(pattern))
        else:
            files = list(dir_path.glob(pattern))

        # Apply exclude patterns
        if exclude_patterns:
            import fnmatch

            files = [
                f
                for f in files
                if not any(fnmatch.fnmatch(str(f), pat) for pat in exclude_patterns)
            ]

        if not files:
            return ResponseEnvelope.success(
                f"No files found matching pattern: {pattern}",
                {"processed": 0, "created": 0, "updated": 0, "unchanged": 0, "errors": []},
            )

        # Production guard: a large bulk ingest is a high-cost write. Only the
        # large-set case needs confirmation; small ingests stay friction-free.
        if len(files) >= _INGEST_DIR_GUARD_THRESHOLD:
            guard = _production_guard("kb_ingest_dir", confirm_production)
            if guard is not None:
                guard["data"] = {"matched_files": len(files)}
                return guard

        created = 0
        updated = 0
        unchanged = 0
        errors = []

        if USE_ASYNC_INGESTION:
            # NEW: True async with asyncio.gather (5x faster, non-blocking)
            import aiofiles

            async def ingest_file_async(filepath: Path) -> dict:
                """Async file ingestion with aiofiles."""
                try:
                    # Async file I/O
                    async with aiofiles.open(filepath, encoding="utf-8") as f:
                        content = await f.read()

                    # Extract frontmatter and hash
                    processor = DocumentProcessor(chunk_size=2000)
                    _, metadata = processor.read_document(str(filepath))
                    doc_hash = processor.compute_hash(content)

                    # Check if document already synced
                    existing_sync = (
                        db.table("knowledge.kb_doc_sync")
                        .select("*")
                        .eq("doc_path", str(filepath))
                        .maybe_single()
                        .execute()
                    )

                    if (
                        existing_sync
                        and existing_sync.data
                        and existing_sync.data.get("doc_hash") == doc_hash
                    ):
                        return {"status": "unchanged", "doc_path": str(filepath)}

                    # Ingest document (synchronous DB calls - Supabase client isn't async)
                    result = handle_kb_ingest_doc(
                        doc_path=str(filepath),
                        strategy=strategy,
                        chunk_size=2000,
                        tags=None,
                        overwrite=False,
                        author=author,
                        source_type=source_type,
                    )

                    return {
                        "status": result.get("data", {}).get("status", "unknown"),
                        "doc_path": str(filepath),
                        "result": result,
                    }

                except Exception as e:
                    return {"status": "error", "doc_path": str(filepath), "error": str(e)}

            # Process files concurrently with asyncio.gather
            import time

            start = time.perf_counter()

            results = await asyncio.gather(*[ingest_file_async(f) for f in files])

            duration_s = time.perf_counter() - start
            logger.info(f"Async ingestion completed in {duration_s:.2f}s")

            # Aggregate results
            for res in results:
                status = res.get("status")
                if status == "created":
                    created += 1
                elif status == "updated":
                    updated += 1
                elif status == "unchanged":
                    unchanged += 1
                elif status == "error":
                    errors.append(
                        {
                            "doc_path": res.get("doc_path"),
                            "error": "ingestion_error",
                            "message": res.get("error"),
                        }
                    )
        else:
            # OLD: ThreadPoolExecutor (fallback for testing)
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_file = {
                    executor.submit(
                        handle_kb_ingest_doc,
                        str(f),
                        strategy,
                        2000,
                        None,
                        False,
                        author,
                        source_type,
                    ): f
                    for f in files
                }

                for future in as_completed(future_to_file):
                    file_path = future_to_file[future]
                    try:
                        result = future.result()
                        if result.get("ok"):
                            status = result.get("data", {}).get("status")
                            if status == "created":
                                created += 1
                            elif status == "updated":
                                updated += 1
                            elif status == "unchanged":
                                unchanged += 1
                        else:
                            errors.append(
                                {
                                    "doc_path": str(file_path),
                                    "error": result.get("error"),
                                    "message": result.get("message"),
                                }
                            )
                    except Exception as e:
                        errors.append(
                            {
                                "doc_path": str(file_path),
                                "error": "unexpected_exception",
                                "message": str(e),
                            }
                        )

        return ResponseEnvelope.success(
            f"Processed {len(files)} files: {created} created, {updated} updated, {unchanged} unchanged",
            {
                "processed": len(files),
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "errors": errors,
            },
        )

    except Exception as e:
        logger.error(f"Error ingesting directory {dir_path}: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_sync_status(dir_path: str = None) -> dict:
    """Check sync state between source docs and KB.

    ``dir_path`` is optional: when omitted it falls back to the ``LORE_SYNC_DIR``
    (or legacy ``LORE_KB_DIR``) environment variable. If neither is provided,
    a clean ``not_configured`` error is returned rather than a validation crash.
    """
    try:
        if not (dir_path and dir_path.strip()):
            dir_path = os.environ.get("LORE_SYNC_DIR") or os.environ.get("LORE_KB_DIR")
        if not dir_path:
            return ResponseEnvelope.error(
                ErrorCodes.NOT_CONFIGURED,
                "dir_path is required when LORE_SYNC_DIR is not configured",
            )

        dir_path = Path(dir_path).resolve()
        if not dir_path.exists():
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Directory not found: {dir_path}")

        # Get all markdown files in directory
        md_files = list(dir_path.rglob("*.md"))
        md_paths = {str(f.resolve()): f for f in md_files}

        # Get all sync records
        sync_records = db.table("knowledge.kb_doc_sync").select("*").execute()

        synced_paths = {r["doc_path"]: r for r in sync_records.data}

        # Classify files
        synced = 0
        modified = 0
        new = 0
        details = []

        for path_str, path_obj in md_paths.items():
            if path_str in synced_paths:
                sync_rec = synced_paths[path_str]
                file_mtime = datetime.fromtimestamp(path_obj.stat().st_mtime)
                last_synced = datetime.fromisoformat(
                    sync_rec["last_modified_at"].replace("Z", "+00:00")
                )

                if file_mtime > last_synced:
                    modified += 1
                    status = "modified"
                else:
                    synced += 1
                    status = "synced"

                details.append(
                    {
                        "doc_path": path_str,
                        "status": status,
                        "last_synced": sync_rec["last_synced_at"],
                        "doc_modified": file_mtime.isoformat(),
                        "kb_ids": sync_rec["kb_ids"],
                    }
                )
            else:
                new += 1
                details.append(
                    {
                        "doc_path": path_str,
                        "status": "new",
                        "last_synced": None,
                        "doc_modified": datetime.fromtimestamp(
                            path_obj.stat().st_mtime
                        ).isoformat(),
                        "kb_ids": [],
                    }
                )

        # Find orphaned KB entries (source doc deleted)
        orphaned_kb_ids = []
        for sync_path, sync_rec in synced_paths.items():
            if sync_path not in md_paths:
                orphaned_kb_ids.extend(sync_rec["kb_ids"])

        return ResponseEnvelope.success(
            f"Sync status: {synced} synced, {modified} modified, {new} new, {len(orphaned_kb_ids)} orphaned",
            {
                "total_docs": len(md_files),
                "synced": synced,
                "modified": modified,
                "new": new,
                "orphaned_kb_entries": len(orphaned_kb_ids),
                "details": details,
            },
        )

    except Exception as e:
        logger.error(f"Error checking sync status: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Semantic Search Maintenance Handlers (v0.6)
# =============================================================================


# Module-level lock that prevents concurrent backfill runs from competing for
# the embedder (and from double-embedding the same rows). Threading.Lock is
# sufficient here: handlers are sync and the process is single-tenant.
_BACKFILL_LOCK = threading.Lock()


def handle_kb_backfill_embeddings(
    batch_size: int = 32,
    limit: int | None = None,
    dry_run: bool = False,
    confirm_production: bool = False,
) -> dict:
    """Embed any KB entries missing or stale embeddings.

    A row is considered stale when:
      - It has no row in the embeddings table, OR
      - Its computed content_hash differs from the stored hash, OR
      - The stored model_name differs from the current LORE_EMBEDDING_MODEL.

    On SQLite the backfill is guarded by the process-local ``_BACKFILL_LOCK``;
    on PostgreSQL it additionally acquires ``pg_try_advisory_lock`` so that
    concurrent processes cannot race each other to embed the same rows.
    Per-row hash guards inside the persist helpers keep the loop safe even
    when the lock is missed.

    When ``LORE_ENV=production`` (or unset) a real (non-dry-run) backfill
    requires ``confirm_production=True`` to guard against accidental
    large-scale writes against the wrong environment.
    """
    guard = _production_guard("kb_backfill_embeddings", confirm_production, dry_run)
    if guard is not None:
        return guard

    backend = _backend_kind()
    if backend == "":
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            "kb_backfill_embeddings: unsupported DB_BACKEND",
        )
    if not _semantic_write_enabled():
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            "Semantic search not enabled. Set LORE_SEMANTIC_SEARCH=true and install "
            "the [semantic] extra.",
        )

    try:
        from lore.embeddings import EMBEDDING_DIM, compute_content_hash, encode_batch
        from lore.embeddings import _model_name as _embedder_model_name
    except ImportError as exc:
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(exc))

    current_model = _embedder_model_name()

    if not _BACKFILL_LOCK.acquire(blocking=False):
        return ResponseEnvelope.error(
            ErrorCodes.INVALID_INPUT,
            "Another backfill is already running; try again shortly.",
        )

    # PG advisory lock — same hashtext() string as future workers will use so
    # cross-process callers cooperate. ``pg_try_advisory_lock`` is non-blocking
    # and returns false when another backend already holds the lock.
    pg_advisory_held = False
    if backend == "postgres":
        try:
            conn = db._get_connection()
            cur = conn.cursor()
            try:
                cur.execute("SELECT pg_try_advisory_lock(hashtext('lore.backfill'))")
                pg_advisory_held = bool(cur.fetchone()[0])
            finally:
                cur.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not acquire PG advisory lock: %s", exc)
            pg_advisory_held = False
        if not pg_advisory_held:
            _BACKFILL_LOCK.release()
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "Another backfill holds the PostgreSQL advisory lock; try again shortly.",
            )

    try:
        if backend == "sqlite":
            conn = db._get_connection()
            # Pull (kb_id, title, content) joined with current meta hash + model.
            rows = conn.execute(
                """
                SELECT e.kb_id, e.title, e.content,
                       m.content_hash AS meta_hash,
                       m.model_name   AS meta_model
                FROM knowledge_kb_entries e
                LEFT JOIN knowledge_kb_embedding_meta m ON m.kb_id = e.kb_id
                """
            ).fetchall()
        else:
            # PostgreSQL.
            conn = db._get_connection()
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT e.kb_id, e.title, e.content,
                           em.content_hash AS meta_hash,
                           em.model_name   AS meta_model
                    FROM knowledge.kb_entries e
                    LEFT JOIN knowledge.kb_embeddings em ON em.kb_id = e.kb_id
                    """
                )
                rows = cur.fetchall()
            finally:
                cur.close()

        to_embed: list[tuple[str, str, str]] = []
        skipped_current = 0
        for r in rows:
            kb_id, title, content, meta_hash, meta_model = (
                r[0],
                r[1] or "",
                r[2] or "",
                r[3],
                r[4],
            )
            expected_hash = compute_content_hash(title, content)
            if meta_hash == expected_hash and meta_model == current_model:
                skipped_current += 1
                continue
            to_embed.append((kb_id, title, content))

        if limit is not None:
            to_embed = to_embed[: int(limit)]

        if dry_run:
            return ResponseEnvelope.success(
                f"Backfill dry run: {len(to_embed)} entries would be embedded",
                {
                    "backend": backend,
                    "total_entries": len(rows),
                    "needs_embedding": len(to_embed),
                    "already_current": skipped_current,
                    "model": current_model,
                    "dry_run": True,
                },
            )

        # Encode in batches for throughput; persist one row at a time so a
        # mid-batch failure still produces partial progress.
        embedded = 0
        failed = 0
        bs = max(1, int(batch_size))
        for start in range(0, len(to_embed), bs):
            batch = to_embed[start : start + bs]
            if backend == "postgres":
                # PG path: batch-encode then upsert each row. Cheaper than
                # encoding one-at-a-time via _embed_kb_entry.
                texts = [f"{t}\n\n{c}" for _, t, c in batch]
                try:
                    vectors = encode_batch(texts)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Batch encode failed (%d rows): %s", len(batch), exc)
                    failed += len(batch)
                    continue
                for (kb_id, title, content), vec in zip(batch, vectors, strict=True):
                    if len(vec) != EMBEDDING_DIM:
                        logger.error("PG backfill dim mismatch for %s", kb_id)
                        failed += 1
                        continue
                    content_hash = compute_content_hash(title, content)
                    ok, _ = _persist_embedding_postgres(
                        kb_id,
                        vec,
                        content_hash,
                        current_model,
                        EMBEDDING_DIM,
                    )
                    if ok:
                        embedded += 1
                    else:
                        failed += 1
            else:
                for kb_id, title, content in batch:
                    ok, _ = _embed_kb_entry(kb_id, title, content)
                    if ok:
                        embedded += 1
                    else:
                        failed += 1

        return ResponseEnvelope.success(
            f"Backfill complete: embedded={embedded}, failed={failed}",
            {
                "backend": backend,
                "total_entries": len(rows),
                "embedded": embedded,
                "failed": failed,
                "already_current": skipped_current,
                "model": current_model,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("kb_backfill_embeddings failed: %s", exc, exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(exc))
    finally:
        if pg_advisory_held:
            try:
                conn = db._get_connection()
                cur = conn.cursor()
                try:
                    cur.execute("SELECT pg_advisory_unlock(hashtext('lore.backfill'))")
                    cur.fetchone()
                finally:
                    cur.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to release PG advisory lock: %s", exc)
        _BACKFILL_LOCK.release()


def handle_kb_embedding_status() -> dict:
    """Report embedding coverage and configuration."""
    backend = _backend_kind()
    if backend == "":
        return ResponseEnvelope.success(
            "kb_embedding_status: unsupported backend",
            {
                "backend": os.getenv("DB_BACKEND", "unknown"),
                "semantic_enabled": False,
            },
        )

    try:
        from lore.embeddings import EMBEDDING_DIM
        from lore.embeddings import _model_name as _embedder_model_name
    except ImportError:
        return ResponseEnvelope.success(
            "Embeddings module not installed",
            {
                "backend": backend,
                "semantic_enabled": False,
                "embeddings_module": False,
            },
        )

    try:
        if backend == "sqlite":
            conn = db._get_connection()
            total = conn.execute("SELECT COUNT(*) FROM knowledge_kb_entries").fetchone()[0]
            if getattr(db, "vec_extension_loaded", False):
                embedded = conn.execute(
                    "SELECT COUNT(*) FROM knowledge_kb_embedding_meta"
                ).fetchone()[0]
                per_model_rows = conn.execute(
                    "SELECT model_name, COUNT(*) FROM knowledge_kb_embedding_meta "
                    "GROUP BY model_name ORDER BY COUNT(*) DESC"
                ).fetchall()
                per_model = {row[0]: row[1] for row in per_model_rows}
            else:
                embedded = 0
                per_model = {}
            missing = max(0, total - embedded)
            current_model = _embedder_model_name()
            model_mismatch = sum(
                count for name, count in per_model.items() if name != current_model
            )

            return ResponseEnvelope.success(
                f"Embedding coverage: {embedded}/{total} entries",
                {
                    "backend": "sqlite",
                    "semantic_enabled": _semantic_write_enabled(),
                    "vec_extension_loaded": bool(getattr(db, "vec_extension_loaded", False)),
                    "fts5_available": bool(getattr(db, "fts5_available", False)),
                    "current_model": current_model,
                    "embedding_dim": EMBEDDING_DIM,
                    "total_entries": total,
                    "embedded": embedded,
                    "missing": missing,
                    "model_mismatch": model_mismatch,
                    "coverage_pct": round(100.0 * embedded / total, 2) if total else 0.0,
                    "per_model": per_model,
                },
            )

        # PostgreSQL.
        conn = db._get_connection()
        cur = conn.cursor()
        try:
            cur.execute("SELECT COUNT(*) FROM knowledge.kb_entries")
            total = cur.fetchone()[0]

            current_model = _embedder_model_name()
            if getattr(db, "vec_extension_loaded", False):
                cur.execute("SELECT COUNT(*) FROM knowledge.kb_embeddings")
                embedded = cur.fetchone()[0]
                cur.execute(
                    "SELECT model_name, COUNT(*) FROM knowledge.kb_embeddings "
                    "GROUP BY model_name ORDER BY COUNT(*) DESC"
                )
                per_model = {row[0]: row[1] for row in cur.fetchall()}
                cur.execute(
                    "SELECT COUNT(*) FROM knowledge.kb_embeddings WHERE model_name != %s",
                    (current_model,),
                )
                model_mismatch = cur.fetchone()[0]
            else:
                embedded = 0
                per_model = {}
                model_mismatch = 0
        finally:
            cur.close()

        missing = max(0, total - embedded)
        pg_version = getattr(db, "pgvector_version", None)
        vector_type = getattr(db, "vector_type", None)
        vec_ext_label = (
            f"pgvector {pg_version} ({vector_type}(384))" if pg_version else "pgvector unavailable"
        )

        return ResponseEnvelope.success(
            f"Embedding coverage: {embedded}/{total} entries",
            {
                "backend": "postgres",
                "semantic_enabled": _semantic_write_enabled(),
                "vec_extension_loaded": bool(getattr(db, "vec_extension_loaded", False)),
                "vector_extension": vec_ext_label,
                "pgvector_version": pg_version,
                "vector_type": vector_type,
                "current_model": current_model,
                "embedding_dim": EMBEDDING_DIM,
                "total_entries": total,
                "embedded": embedded,
                "missing": missing,
                "model_mismatch": model_mismatch,
                "coverage_pct": round(100.0 * embedded / total, 2) if total else 0.0,
                "per_model": per_model,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("kb_embedding_status failed: %s", exc, exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(exc))


# =============================================================================
# MCP Index Handlers
# =============================================================================


def handle_mcp_index_scan(triggered_by: str = "manual", config_filter: bool = True) -> dict:
    """Scan all MCP servers and index their tools."""
    try:
        scanner = MCPIndexScanner(db)
        result = scanner.scan_all_servers(triggered_by=triggered_by, config_filter=config_filter)

        if result.get("error") == ErrorCodes.NOT_CONFIGURED:
            return ResponseEnvelope.error(ErrorCodes.NOT_CONFIGURED, result["message"])

        return ResponseEnvelope.success(
            f"Scanned {result['servers_scanned']} servers, indexed {result['tools_indexed']} tools",
            result,
        )

    except Exception as e:
        logger.error(f"Error scanning MCP index: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_mcp_index_search(query: str, category: str = None, limit: int = 20) -> dict:
    """Search for MCP tools by description/capability."""
    try:
        scanner = MCPIndexScanner(db)
        results = scanner.search_tools(query, category, limit)

        # Auto re-index watchdog: if results are empty, check if index is stale
        re_index_triggered = False
        if len(results) == 0:
            # Query last scan time from mcp_index_versions
            try:
                last_scan_result = (
                    db.table("mcp_index_versions")
                    .select("scan_time")
                    .order("scan_time", desc=True)
                    .limit(1)
                    .execute()
                )

                if last_scan_result.data and len(last_scan_result.data) > 0:
                    last_scan_time = datetime.fromisoformat(
                        last_scan_result.data[0]["scan_time"].replace("Z", "+00:00")
                    )
                    time_since_scan = (
                        datetime.now(last_scan_time.tzinfo) - last_scan_time
                    ).total_seconds()

                    # If stale (>1 hour = 3600 seconds), trigger background re-index
                    if time_since_scan > 3600:
                        logger.info(
                            f"MCP Index stale ({time_since_scan / 3600:.1f}h old), triggering background re-index"
                        )

                        # Launch background re-index using threading
                        def background_reindex():
                            try:
                                scanner_bg = MCPIndexScanner(db)
                                result = scanner_bg.scan_all_servers(triggered_by="auto_watchdog")
                                if result.get("error") == ErrorCodes.NOT_CONFIGURED:
                                    logger.debug(
                                        "mcp_index auto-watchdog skipped: LORE_MCP_SERVERS_PATH not configured"
                                    )
                                    return
                                logger.info(
                                    f"Auto re-index complete: {result['servers_scanned']} servers, {result['tools_indexed']} tools"
                                )
                            except Exception as e:
                                logger.error(f"Background re-index failed: {e}", exc_info=True)

                        thread = threading.Thread(target=background_reindex, daemon=True)
                        thread.start()
                        re_index_triggered = True
                else:
                    # No scan history found, trigger initial scan
                    logger.info(
                        "No MCP Index scan history found, triggering initial background scan"
                    )

                    def background_reindex():
                        try:
                            scanner_bg = MCPIndexScanner(db)
                            result = scanner_bg.scan_all_servers(
                                triggered_by="auto_watchdog_initial"
                            )
                            if result.get("error") == ErrorCodes.NOT_CONFIGURED:
                                logger.debug(
                                    "mcp_index auto-watchdog skipped: LORE_MCP_SERVERS_PATH not configured"
                                )
                                return
                            logger.info(
                                f"Initial auto scan complete: {result['servers_scanned']} servers, {result['tools_indexed']} tools"
                            )
                        except Exception as e:
                            logger.error(f"Background initial scan failed: {e}", exc_info=True)

                    thread = threading.Thread(target=background_reindex, daemon=True)
                    thread.start()
                    re_index_triggered = True

            except Exception as e:
                logger.warning(f"Failed to check MCP Index staleness: {e}")

        # Build response with re-index metadata
        response_data = {
            "results": results,
            "query": query,
            "category": category,
            "re_index_triggered": re_index_triggered,
        }

        message = f"Found {len(results)} tools matching '{query}'"
        if re_index_triggered:
            message += " (re-index triggered in background, retry in 30 seconds)"

        return ResponseEnvelope.success(message, response_data)

    except Exception as e:
        logger.error(f"Error searching MCP index: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_mcp_index_get_server(server_id: str) -> dict:
    """Get all tools for a specific MCP server."""
    try:
        scanner = MCPIndexScanner(db)
        result = scanner.get_server_tools(server_id)

        if not result:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Server not found: {server_id}")

        server = result["server"]
        tools = result["tools"]

        return ResponseEnvelope.success(
            f"Server {server_id} has {len(tools)} tools", {"server": server, "tools": tools}
        )

    except Exception as e:
        logger.error(f"Error getting server tools: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_mcp_index_get_tool(tool_name: str) -> dict:
    """Get detailed information about a specific tool."""
    try:
        scanner = MCPIndexScanner(db)
        tool = scanner.get_tool_details(tool_name)

        if not tool:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Tool not found: {tool_name}")

        return ResponseEnvelope.success(f"Found tool: {tool['full_name']}", {"tool": tool})

    except Exception as e:
        logger.error(f"Error getting tool details: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_mcp_index_rebuild() -> dict:
    """Force rebuild of entire MCP index."""
    try:
        scanner = MCPIndexScanner(db)
        result = scanner.scan_all_servers(triggered_by="rebuild")

        if result.get("error") == ErrorCodes.NOT_CONFIGURED:
            return ResponseEnvelope.error(ErrorCodes.NOT_CONFIGURED, result["message"])

        return ResponseEnvelope.success(
            f"Rebuilt index: {result['servers_scanned']} servers, {result['tools_indexed']} tools",
            result,
        )

    except Exception as e:
        logger.error(f"Error rebuilding MCP index: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Search Handlers (consolidated from search-mcp)
# =============================================================================


def search_file_content(file_path: Path, query: str) -> dict | None:
    """Search a single file for query string."""
    try:
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
            if query.lower() in content.lower():
                # Find context around match
                pos = content.lower().find(query.lower())
                start = max(0, pos - 100)
                end = min(len(content), pos + len(query) + 100)
                snippet = content[start:end]

                return {
                    "file": str(file_path),
                    "match_count": content.lower().count(query.lower()),
                    "snippet": snippet,
                    "file_size": file_path.stat().st_size,
                }
    except Exception as e:
        logger.warning(f"Error searching {file_path}: {e}")
    return None


def handle_search_local(query: str, paths: list[str] = None, file_types: list[str] = None) -> dict:
    """Search local files by content."""
    try:
        # Default paths. The Latvian roots are optional and may be unset (None);
        # skip those so we never scan a literal "None" path. KNOWLEDGE_DATA_DIR
        # always has a portable default and is always included.
        if not paths:
            default_roots = [LATVIAN_LEARNING_ROOT, LATVIAN_XTTS_ROOT, KNOWLEDGE_DATA_DIR]
            paths = [str(root) for root in default_roots if root is not None]

        # Default file types
        if not file_types:
            file_types = ["txt", "json", "md", "py", "yaml", "yml"]

        results = []
        file_count = 0

        for search_path in paths:
            path_obj = Path(search_path)
            if not path_obj.exists():
                continue

            for file_type in file_types:
                for file_path in path_obj.rglob(f"*.{file_type}"):
                    file_count += 1
                    result = search_file_content(file_path, query)
                    if result:
                        results.append(result)

                    # Limit results
                    if len(results) >= 100:
                        break

        results.sort(key=lambda x: x["match_count"], reverse=True)

        return ResponseEnvelope.success(
            f"Found {len(results)} matches in {file_count} files",
            {
                "results": results[:50],  # Return top 50
                "total_matches": len(results),
                "files_searched": file_count,
            },
        )
    except Exception as e:
        logger.error(f"Error in handle_search_local: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_search_corpora(query: str, corpus_ids: list[str] = None) -> dict:
    """Search across corpus manifests."""
    try:
        # INGEST_ROOT is an optional, deployment-specific corpora location. When
        # it is unset (the portable default) there is no data source to search,
        # so return a clean empty result instead of crashing on ``None / "..."``.
        if INGEST_ROOT is None:
            return ResponseEnvelope.success(
                "Corpora search not configured (set INGEST_ROOT to enable)",
                {"results": [], "total_matches": 0},
            )

        results = []
        corpora_dir = INGEST_ROOT / "corpora"

        if not corpora_dir.exists():
            return ResponseEnvelope.success(
                "Corpora directory not found (expected until data ingested)",
                {"results": [], "count": 0},
            )

        # Search corpus manifest files
        for manifest_file in corpora_dir.glob("*.jsonl"):
            # Filter by corpus_ids if specified
            if corpus_ids and manifest_file.stem not in corpus_ids:
                continue

            with open(manifest_file) as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        entry = json.loads(line)
                        # Search in transcript and metadata
                        if (
                            query.lower() in entry.get("text", "").lower()
                            or query.lower() in json.dumps(entry.get("metadata", {})).lower()
                        ):
                            results.append(
                                {
                                    "corpus": manifest_file.stem,
                                    "line": line_num,
                                    "segment_id": entry.get("segment_id", "unknown"),
                                    "text": entry.get("text", "")[:200],
                                    "metadata": entry.get("metadata", {}),
                                }
                            )

                            if len(results) >= 100:
                                break
                    except json.JSONDecodeError:
                        continue

            if len(results) >= 100:
                break

        return ResponseEnvelope.success(
            f"Found {len(results)} matches in corpora",
            {"results": results[:50], "total_matches": len(results)},
        )
    except Exception as e:
        logger.error(f"Error in handle_search_corpora: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_search_transcripts(query: str, speaker: str = None) -> dict:
    """Search transcript segments."""
    try:
        # LATVIAN_XTTS_ROOT is an optional, deployment-specific transcript
        # location. When unset (the portable default) there is nothing to
        # search, so return a clean empty result instead of crashing on
        # ``None / "whisper_extracted"``.
        if LATVIAN_XTTS_ROOT is None:
            return ResponseEnvelope.success(
                "Transcript search not configured (set LATVIAN_XTTS_ROOT to enable)",
                {"results": [], "total_matches": 0},
            )

        results = []

        # Search in whisper extracted directories
        search_dirs = [
            LATVIAN_XTTS_ROOT / "whisper_extracted",
            LATVIAN_XTTS_ROOT / "whisper_extracted_enhanced",
            LATVIAN_XTTS_ROOT / "whisper_extracted_normalized",
        ]

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue

            for json_file in search_dir.rglob("*.json"):
                try:
                    with open(json_file) as f:
                        data = json.load(f)

                        # Filter by speaker if specified
                        if speaker and data.get("speaker") != speaker:
                            continue

                        # Search in text
                        text = data.get("text", "")
                        if query.lower() in text.lower():
                            results.append(
                                {
                                    "file": str(json_file.relative_to(LATVIAN_XTTS_ROOT)),
                                    "speaker": data.get("speaker", "unknown"),
                                    "text": text[:200],
                                    "duration": data.get("duration_seconds"),
                                    "timestamp": data.get("start_time"),
                                }
                            )

                            if len(results) >= 100:
                                break
                except (OSError, json.JSONDecodeError):
                    continue

            if len(results) >= 100:
                break

        return ResponseEnvelope.success(
            f"Found {len(results)} transcript matches",
            {"results": results[:50], "total_matches": len(results)},
        )
    except Exception as e:
        logger.error(f"Error in handle_search_transcripts: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_multi_search(query: str) -> dict:
    """Combined search across all sources.

    Issue #20 — the KB-search subpath MUST return the same results that a
    direct ``kb_search`` call would for the same query. Concretely, this means:
      * route through ``handle_kb_search`` (not a private helper)
      * pass the query verbatim — no extra terms, no sanitisation, no rewrite
      * leave ``search_mode``/``semantic``/``hybrid`` at their defaults so
        the same routing rules apply (Issue #20 regression test guards this)
      * surface ``data.results`` under ``knowledge.kb_entries`` verbatim — no
        post-filter, no slicing (kb_search already honours its own top_k=20)
    The ``caller_agent="multi_search"`` tag only flows through to telemetry;
    it has no effect on result filtering.
    """
    try:
        results = {"local": [], "corpora": [], "transcripts": [], "knowledge": {}}

        # Local search (limited)
        local_result = handle_search_local(
            query, paths=[str(KNOWLEDGE_DATA_DIR)], file_types=["json", "md"]
        )
        if local_result.get("ok"):
            results["local"] = local_result["data"]["results"][:10]

        # Knowledge search using kb_search (Issue #20).
        # Pass the query through unchanged so multi_search and a direct
        # kb_search call return identical KB results for the same input.
        knowledge_result = handle_kb_search(query, caller_agent="multi_search")
        if knowledge_result.get("ok"):
            results["knowledge"] = {"kb_entries": knowledge_result["data"]["results"]}

        # Corpora search
        corpora_result = handle_search_corpora(query)
        if corpora_result.get("ok"):
            results["corpora"] = corpora_result["data"]["results"][:10]

        # Transcript search
        transcript_result = handle_search_transcripts(query)
        if transcript_result.get("ok"):
            results["transcripts"] = transcript_result["data"]["results"][:10]

        total_matches = (
            len(results["local"])
            + len(results["knowledge"].get("kb_entries", []))
            + len(results["corpora"])
            + len(results["transcripts"])
        )

        return ResponseEnvelope.success(
            f"Multi-search found {total_matches} matches across all sources",
            {"results": results, "total_matches": total_matches},
        )
    except Exception as e:
        logger.error(f"Error in handle_multi_search: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_deduplicate_results(results: list[dict], threshold: float = 0.9) -> dict:
    """Remove duplicate search results.

    De-dup key selection (BUG-2): items carrying text/content/snippet are
    de-duplicated on that normalized text. Items WITHOUT any such field (e.g.
    kb_search results, which only have kb_id/title/topic/score) fall back to
    their ``kb_id`` as the identity key — so two results with different kb_ids
    are never collapsed into one, while a repeated kb_id is still removed.
    Items with neither a text field nor a kb_id are always kept.
    """
    try:
        # Simple deduplication based on exact text matches
        seen = set()
        deduped = []

        for result in results:
            # Prefer a text/content/snippet key; fall back to kb_id when the
            # item has no content field (or it is empty), so content-less
            # search results are de-duped by identity, not collapsed together.
            text = result.get("text", "") or result.get("content", "") or result.get("snippet", "")
            text_normalized = text.lower().strip() if isinstance(text, str) else ""

            if text_normalized:
                key = ("text", text_normalized)
            elif result.get("kb_id"):
                key = ("kb_id", result["kb_id"])
            else:
                # Nothing to key on — keep the item rather than dropping it.
                deduped.append(result)
                continue

            if key not in seen:
                seen.add(key)
                deduped.append(result)

        removed = len(results) - len(deduped)

        return ResponseEnvelope.success(
            f"Removed {removed} duplicates, {len(deduped)} unique results remaining",
            {"results": deduped, "removed_count": removed, "unique_count": len(deduped)},
        )
    except Exception as e:
        logger.error(f"Error in handle_deduplicate_results: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_cluster_results(results: list[dict]) -> dict:
    """Group results by source_type (Issue #22).

    Bucketed by a per-result key — file extension when ``file`` is present,
    ``corpus`` / ``transcript`` for the corresponding source types, otherwise
    ``other``. The cluster count is determined entirely by the input data;
    there is no parameter to override it.

    Issue #22: the previously-accepted ``num_clusters`` / ``n_clusters`` knobs
    were misleading (they were ignored by the implementation) and have been
    removed from the schema and signature. Behaviour is unchanged — only the
    surface API has been trimmed to match reality.
    """
    try:
        clusters = {}

        for result in results:
            # Determine cluster key
            if "file" in result:
                file_path = Path(result["file"])
                cluster_key = file_path.suffix or "other"
            elif "corpus" in result:
                cluster_key = "corpus"
            elif "speaker" in result:
                cluster_key = "transcript"
            else:
                cluster_key = "other"

            if cluster_key not in clusters:
                clusters[cluster_key] = []
            clusters[cluster_key].append(result)

        cluster_summary = {cluster: len(items) for cluster, items in clusters.items()}

        return ResponseEnvelope.success(
            f"Clustered {len(results)} results into {len(clusters)} groups",
            {
                "clusters": clusters,
                "cluster_summary": cluster_summary,
                "total_results": len(results),
            },
        )
    except Exception as e:
        logger.error(f"Error in handle_cluster_results: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def main() -> None:
    """Deprecated entry point. Use lore.server_fastmcp:main instead.

    Retained for backwards compatibility for users invoking ``python -m lore.server``.
    Emits a DeprecationWarning then delegates to ``lore.server_fastmcp:main``.
    """
    import warnings

    warnings.warn(
        "lore.server:main is deprecated and will be removed in a future release. "
        "The lore-mcp console script now invokes lore.server_fastmcp:main directly.",
        DeprecationWarning,
        stacklevel=2,
    )
    from lore.server_fastmcp import main as _main

    _main()


if __name__ == "__main__":
    main()
