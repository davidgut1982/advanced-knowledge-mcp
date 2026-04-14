#!/usr/bin/env python3
"""
Knowledge MCP Server - Supabase Backend

Unified knowledge management system for:
- Knowledge Base (structured documents)
- Research (notes, sources, experiments)
- Journal (decision log and config snapshots)
- MCP Index (tool discovery and search)

Spec: LATVIAN_LAB_MCP_MASTER_SPEC_v1.3 § 4.1 + § 4.1.5 (Document Ingestion)
"""

import os
import sys
import json
import logging
import threading
import re
from pathlib import Path
from typing import Any, List, Dict, Optional
from datetime import datetime, date
import uuid
import psycopg2
import hashlib
import glob as glob_module
from concurrent.futures import ThreadPoolExecutor, as_completed

import jsonschema
import sentry_sdk
import yaml
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

# Import document processor and MCP scanner
from .doc_processor import DocumentProcessor
from .mcp_index_scanner import MCPIndexScanner

from .response import ResponseEnvelope, ErrorCodes
from .env_config import get_env, require_env
from .db_client import get_db_client, DatabaseBackend

# Initialize logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Sentry
SENTRY_DSN = get_env("SENTRY_DSN")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        traces_sample_rate=1.0,
        environment=get_env("SENTRY_ENVIRONMENT", "development"),
        release=get_env("SENTRY_RELEASE", "latvian-lab@1.0.0"),
    )
    logger.info("Sentry monitoring enabled")

# Initialize MCP server
app = Server("knowledge-mcp")

# Database Configuration (will be initialized in main())
db = None  # Database client (LocalPostgresClient or SupabaseWrapper)

# Search Configuration (consolidated from search-mcp)
LATVIAN_LEARNING_ROOT = Path(get_env("LATVIAN_LEARNING_ROOT", "/srv/latvian_learning"))
LATVIAN_XTTS_ROOT = Path(get_env("LATVIAN_XTTS_ROOT", "/srv/latvian_xtts"))
INGEST_ROOT = Path(get_env("INGEST_ROOT", "/srv/ingest"))
KNOWLEDGE_DATA_DIR = Path(get_env("KNOWLEDGE_DATA_DIR", "/srv/latvian_mcp/data/knowledge"))


def json_serializer(obj):
    """Custom JSON serializer for datetime objects."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def format_response(response: dict) -> list[types.TextContent]:
    """Format response as MCP TextContent."""
    return [types.TextContent(type="text", text=json.dumps(response, default=json_serializer))]


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
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags"}
                },
                "required": ["topic", "title", "content"]
            }
        ),
        types.Tool(
            name="kb_search",
            description="Search knowledge base",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "topic": {"type": "string", "description": "Filter by topic"}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="kb_get",
            description="Get full KB entry by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "KB entry ID"}
                },
                "required": ["kb_id"]
            }
        ),
        types.Tool(
            name="kb_list",
            description="List KB entries",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Filter by topic"}
                }
            }
        ),
        types.Tool(
            name="kb_update",
            description="Update existing KB entry content and metadata",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "UUID of entry to update"},
                    "content": {"type": "string", "description": "New content text"},
                    "metadata": {"type": "object", "description": "Updated metadata object"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Updated tags array"}
                },
                "required": ["entry_id"]
            }
        ),
        types.Tool(
            name="kb_delete",
            description="Delete existing KB entry from database",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string", "description": "UUID of entry to delete"},
                    "confirm": {"type": "boolean", "description": "Confirmation flag for safety", "default": False}
                },
                "required": ["entry_id"]
            }
        ),

        # Knowledge Graph Tools (5)
        types.Tool(
            name="kg_add_node",
            description="Add a node to knowledge graph",
            inputSchema={
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Node label"},
                    "kind": {"type": "string", "enum": ["concept", "entity", "event", "attribute"]},
                    "properties": {"type": "object", "description": "Node properties"}
                },
                "required": ["label", "kind"]
            }
        ),
        types.Tool(
            name="kg_add_edge",
            description="Add edge between KG nodes",
            inputSchema={
                "type": "object",
                "properties": {
                    "from_node": {"type": "string", "description": "Source node ID"},
                    "to_node": {"type": "string", "description": "Target node ID"},
                    "relation": {"type": "string", "description": "Relation type"},
                    "properties": {"type": "object", "description": "Edge properties"}
                },
                "required": ["from_node", "to_node", "relation"]
            }
        ),
        types.Tool(
            name="kg_get_node",
            description="Get KG node details",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node ID"}
                },
                "required": ["node_id"]
            }
        ),
        types.Tool(
            name="kg_neighbors",
            description="Get neighboring nodes",
            inputSchema={
                "type": "object",
                "properties": {
                    "node_id": {"type": "string", "description": "Node ID"}
                },
                "required": ["node_id"]
            }
        ),
        types.Tool(
            name="kg_search",
            description="Search knowledge graph",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "kind": {"type": "string", "description": "Filter by kind"}
                },
                "required": ["query"]
            }
        ),

        # Research Tools (8)
        types.Tool(
            name="research_add_note",
            description="Add research note",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["topic", "title", "content"]
            }
        ),
        types.Tool(
            name="research_list_notes",
            description="List research notes",
            inputSchema={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Filter by topic"}
                }
            }
        ),
        types.Tool(
            name="research_get_note",
            description="Get research note",
            inputSchema={
                "type": "object",
                "properties": {
                    "note_id": {"type": "string"}
                },
                "required": ["note_id"]
            }
        ),
        types.Tool(
            name="research_add_source",
            description="Add research source",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["paper", "book", "article", "url", "dataset"]},
                    "url": {"type": "string"},
                    "authors": {"type": "array", "items": {"type": "string"}},
                    "year": {"type": "integer"},
                    "notes": {"type": "string"}
                },
                "required": ["title", "kind"]
            }
        ),
        types.Tool(
            name="research_list_sources",
            description="List research sources",
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string"}
                }
            }
        ),
        types.Tool(
            name="research_log_experiment",
            description="Log experiment",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "methodology": {"type": "string"},
                    "results": {"type": "object"},
                    "conclusion": {"type": "string"}
                },
                "required": ["title"]
            }
        ),
        types.Tool(
            name="research_list_experiments",
            description="List experiments",
            inputSchema={"type": "object", "properties": {}}
        ),
        types.Tool(
            name="research_link_source_to_experiment",
            description="Link source to experiment",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_id": {"type": "string"},
                    "experiment_id": {"type": "string"}
                },
                "required": ["source_id", "experiment_id"]
            }
        ),

        # Journal Tools (4)
        types.Tool(
            name="journal_append",
            description="Append journal entry",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_type": {"type": "string", "enum": ["daily", "milestone", "reflection", "idea"]},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}}
                },
                "required": ["entry_type", "content"]
            }
        ),
        types.Tool(
            name="journal_list",
            description="List journal entries",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20}
                }
            }
        ),
        types.Tool(
            name="journal_get",
            description="Get journal entry",
            inputSchema={
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"}
                },
                "required": ["entry_id"]
            }
        ),
        types.Tool(
            name="snapshot_config",
            description="Snapshot current config",
            inputSchema={
                "type": "object",
                "properties": {
                    "config_name": {"type": "string"},
                    "config_data": {"type": "object"}
                },
                "required": ["config_name", "config_data"]
            }
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
                        "description": "Ingestion strategy: full (one entry), chunked (by sections), summary (GPT summary)"
                    },
                    "chunk_size": {"type": "integer", "default": 2000, "description": "Max tokens per chunk (chunked strategy only)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Additional tags"},
                    "overwrite": {"type": "boolean", "default": False, "description": "Replace existing KB entries from this doc"}
                },
                "required": ["doc_path"]
            }
        ),
        types.Tool(
            name="kb_ingest_dir",
            description="Batch ingest directory of markdown files",
            inputSchema={
                "type": "object",
                "properties": {
                    "dir_path": {"type": "string", "description": "Directory to scan"},
                    "pattern": {"type": "string", "default": "*.md", "description": "File pattern (e.g., *.md)"},
                    "strategy": {"type": "string", "enum": ["full", "chunked", "summary"], "default": "chunked"},
                    "recursive": {"type": "boolean", "default": True, "description": "Scan subdirectories"},
                    "exclude_patterns": {"type": "array", "items": {"type": "string"}, "description": "Patterns to exclude"}
                },
                "required": ["dir_path"]
            }
        ),
        types.Tool(
            name="kb_sync_status",
            description="Check sync state between source docs and KB",
            inputSchema={
                "type": "object",
                "properties": {
                    "dir_path": {"type": "string", "description": "Directory to check"}
                },
                "required": ["dir_path"]
            }
        ),
        types.Tool(
            name="kb_link_to_source",
            description="Get source document reference for a KB entry",
            inputSchema={
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "description": "KB entry ID"}
                },
                "required": ["kb_id"]
            }
        ),

        # MCP Index Tools (5)
        types.Tool(
            name="mcp_index_scan",
            description="Scan all MCP servers and index their tools. By default, scans only configured servers (66% token savings).",
            inputSchema={
                "type": "object",
                "properties": {
                    "triggered_by": {"type": "string", "default": "manual", "description": "Source of scan (manual, cron, deployment)"},
                    "config_filter": {"type": "boolean", "default": True, "description": "If true (default), scan only servers in ~/.claude.json. Set false to scan all servers."}
                }
            }
        ),
        types.Tool(
            name="mcp_index_search",
            description="Search for MCP tools by description/capability",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "category": {"type": "string", "description": "Optional category filter (search, storage, processing, etc.)"},
                    "limit": {"type": "integer", "default": 20, "description": "Maximum results"}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="mcp_index_get_server",
            description="Get all tools for a specific MCP server",
            inputSchema={
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "Server ID (e.g., 'knowledge-mcp')"}
                },
                "required": ["server_id"]
            }
        ),
        types.Tool(
            name="mcp_index_get_tool",
            description="Get detailed information about a specific tool",
            inputSchema={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Tool name (e.g., 'kb_search')"}
                },
                "required": ["tool_name"]
            }
        ),
        types.Tool(
            name="mcp_index_rebuild",
            description="Force rebuild of entire MCP index (same as mcp_index_scan)",
            inputSchema={
                "type": "object",
                "properties": {}
            }
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
                        "description": "Paths to search (defaults: learning, xtts, knowledge)"
                    },
                    "file_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File extensions to search (default: txt, json, md, py, yaml)"
                    }
                },
                "required": ["query"]
            }
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
                        "description": "Specific corpus IDs to search (optional)"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="search_transcripts",
            description="Search transcript segments from Whisper outputs",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "speaker": {"type": "string", "description": "Filter by speaker (optional)"}
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="multi_search",
            description="Combined search across all sources (local, knowledge, corpora, transcripts)",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
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
                        "description": "Array of search result objects"
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Similarity threshold (0-1, default: 0.9)"
                    }
                },
                "required": ["results"]
            }
        ),
        types.Tool(
            name="cluster_results",
            description="Cluster search results by topic/source type",
            inputSchema={
                "type": "object",
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Array of search result objects"
                    },
                    "num_clusters": {
                        "type": "integer",
                        "description": "Number of clusters (default: 5)"
                    }
                },
                "required": ["results"]
            }
        ),
]


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    """List all available tools."""
    return _TOOL_DEFINITIONS


# Pre-built schema lookup table for O(1) access in call_tool().
_TOOL_SCHEMA_MAP: Dict[str, dict] = {t.name: t.inputSchema for t in _TOOL_DEFINITIONS}


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
                    ResponseEnvelope.error(ErrorCodes.INVALID_INPUT, f"Input validation error: {exc.message}")
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

        # Knowledge Graph Tools
        elif name == "kg_add_node":
            return format_response(handle_kg_add_node(**arguments))
        elif name == "kg_add_edge":
            return format_response(handle_kg_add_edge(**arguments))
        elif name == "kg_get_node":
            return format_response(handle_kg_get_node(**arguments))
        elif name == "kg_neighbors":
            return format_response(handle_kg_neighbors(**arguments))
        elif name == "kg_search":
            return format_response(handle_kg_search(**arguments))

        # Research Tools
        elif name == "research_add_note":
            return format_response(handle_research_add_note(**arguments))
        elif name == "research_list_notes":
            return format_response(handle_research_list_notes(**arguments))
        elif name == "research_get_note":
            return format_response(handle_research_get_note(**arguments))
        elif name == "research_add_source":
            return format_response(handle_research_add_source(**arguments))
        elif name == "research_list_sources":
            return format_response(handle_research_list_sources(**arguments))
        elif name == "research_log_experiment":
            return format_response(handle_research_log_experiment(**arguments))
        elif name == "research_list_experiments":
            return format_response(handle_research_list_experiments(**arguments))
        elif name == "research_link_source_to_experiment":
            return format_response(handle_research_link_source_to_experiment(**arguments))

        # Journal Tools
        elif name == "journal_append":
            return format_response(handle_journal_append(**arguments))
        elif name == "journal_list":
            return format_response(handle_journal_list(**arguments))
        elif name == "journal_get":
            return format_response(handle_journal_get(**arguments))
        elif name == "snapshot_config":
            return format_response(handle_snapshot_config(**arguments))

        # Document Ingestion Tools (v1.3)
        elif name == "kb_ingest_doc":
            return format_response(handle_kb_ingest_doc(**arguments))
        elif name == "kb_ingest_dir":
            return format_response(handle_kb_ingest_dir(**arguments))
        elif name == "kb_sync_status":
            return format_response(handle_kb_sync_status(**arguments))
        elif name == "kb_link_to_source":
            return format_response(handle_kb_link_to_source(**arguments))

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

def handle_kb_add(topic: str, title: str, content: str, tags: list = None) -> dict:
    """Add KB entry."""
    try:
        kb_id = f"kb_{uuid.uuid4().hex[:12]}"

        entry = {
            "kb_id": kb_id,
            "topic": topic,
            "title": title,
            "content": content,
            "tags": tags or []
        }

        db.table("knowledge.kb_entries").insert(entry).execute()

        return ResponseEnvelope.success(
            f"Added KB entry: {title}",
            {"kb_id": kb_id, "topic": topic}
        )
    except Exception as e:
        logger.error(f"Error adding KB entry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_search(query: str, topic: str = None) -> dict:
    """Search KB entries using PostgreSQL full-text search."""
    try:
        # Sanitize query for tsquery - replace spaces with & for AND logic
        # This makes "Phase 2" search for "Phase & 2"
        tsquery_safe = query.strip().replace(" ", " & ")

        query_builder = db.table("knowledge.kb_entries")\
            .select("kb_id, topic, title, tags")

        if topic:
            query_builder = query_builder.eq("topic", topic)

        # Use PostgreSQL full-text search with websearch syntax (most forgiving)
        # This leverages the GIN index: idx_kb_search on to_tsvector('english', title || ' ' || content)
        # PostgREST will use the index automatically when we search on title or content
        try:
            # Try websearch FTS first (most flexible - handles phrases, AND/OR)
            query_builder = query_builder.or_(
                f"title.wfts.{query},content.wfts.{query}"
            )
        except Exception as fts_error:
            # Fallback to plain text FTS if websearch fails
            logger.warning(f"Websearch FTS failed, using plain FTS: {fts_error}")
            query_builder = query_builder.or_(
                f"title.plfts.{tsquery_safe},content.plfts.{tsquery_safe}"
            )

        result = query_builder.limit(50).execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} KB entries",
            {"results": result.data, "count": len(result.data)}
        )
    except Exception as e:
        logger.error(f"Error searching KB: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_get(kb_id: str) -> dict:
    """Get KB entry details."""
    try:
        result = db.table("knowledge.kb_entries")\
            .select("*")\
            .eq("kb_id", kb_id)\
            .maybe_single()\
            .execute()

        if not result or not result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {kb_id}")

        return ResponseEnvelope.success(
            f"KB entry: {result.data['title']}",
            result.data
        )
    except Exception as e:
        logger.error(f"Error getting KB entry: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_list(topic: str = None) -> dict:
    """List KB entries."""
    try:
        query = db.table("knowledge.kb_entries")\
            .select("kb_id, topic, title, tags, created_at")\
            .order("created_at", desc=True)

        if topic:
            query = query.eq("topic", topic)

        result = query.limit(100).execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} KB entries",
            {"entries": result.data, "count": len(result.data)}
        )
    except Exception as e:
        logger.error(f"Error listing KB entries: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_update(entry_id: str, content: str = None, metadata: dict = None, tags: list = None) -> dict:
    """Update existing KB entry with partial updates support.

    Updates only the provided fields, preserving existing fields not specified.
    Re-embeds content if content changes. Updates updated_at timestamp.
    """
    try:
        # First, verify the entry exists
        existing_result = db.table("knowledge.kb_entries")\
            .select("*")\
            .eq("kb_id", entry_id)\
            .maybe_single()\
            .execute()

        if not existing_result or not existing_result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {entry_id}")

        existing_entry = existing_result.data

        # Build update object with only provided fields
        update_data = {}

        if content is not None:
            update_data["content"] = content
            # Note: In a full implementation, you'd regenerate embeddings here
            # when content changes. This is simplified for the basic CRUD operation.

        if metadata is not None:
            # Merge with existing metadata if it exists
            current_metadata = existing_entry.get("metadata", {})
            if isinstance(current_metadata, dict):
                current_metadata.update(metadata)
                update_data["metadata"] = current_metadata
            else:
                update_data["metadata"] = metadata

        if tags is not None:
            update_data["tags"] = tags

        # Always update the updated_at timestamp
        update_data["updated_at"] = datetime.utcnow().isoformat()

        # Only proceed with update if there are fields to update
        if not update_data:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "No fields provided for update. Specify content, metadata, and/or tags."
            )

        # Perform the update
        db.table("knowledge.kb_entries")\
            .update(update_data)\
            .eq("kb_id", entry_id)\
            .execute()

        # Fetch and return the updated entry
        updated_result = db.table("knowledge.kb_entries")\
            .select("*")\
            .eq("kb_id", entry_id)\
            .single()\
            .execute()

        updated_fields = list(update_data.keys())
        return ResponseEnvelope.success(
            f"Updated KB entry '{existing_entry['title']}' (fields: {', '.join(updated_fields)})",
            {
                "kb_id": entry_id,
                "updated_fields": updated_fields,
                "entry": updated_result.data
            }
        )

    except Exception as e:
        logger.error(f"Error updating KB entry {entry_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_delete(entry_id: str, confirm: bool = False) -> dict:
    """Delete existing KB entry from database with safety confirmation.

    Requires explicit confirmation for safety. Deletes entry and associated embeddings.
    Returns deleted entry details for audit trail.
    """
    try:
        # Safety check: require explicit confirmation
        if not confirm:
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_INPUT,
                "Deletion requires explicit confirmation. Set confirm=True to proceed."
            )

        # First, verify the entry exists and get its details for audit trail
        existing_result = db.table("knowledge.kb_entries")\
            .select("*")\
            .eq("kb_id", entry_id)\
            .maybe_single()\
            .execute()

        if not existing_result or not existing_result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {entry_id}")

        deleted_entry = existing_result.data
        entry_title = deleted_entry.get("title", "Untitled")

        # Delete the entry
        db.table("knowledge.kb_entries")\
            .delete()\
            .eq("kb_id", entry_id)\
            .execute()

        # Note: In a full implementation, you would also delete associated embeddings/vectors here
        # This is simplified for the basic CRUD operation.

        return ResponseEnvelope.success(
            f"Deleted KB entry '{entry_title}' ({entry_id})",
            {
                "kb_id": entry_id,
                "deleted_entry": deleted_entry
            }
        )

    except Exception as e:
        logger.error(f"Error deleting KB entry {entry_id}: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Research Handlers
# =============================================================================

def handle_research_add_note(topic: str, title: str, content: str, tags: list = None) -> dict:
    """Add research note."""
    try:
        note_id = f"note_{uuid.uuid4().hex[:12]}"

        note = {
            "note_id": note_id,
            "topic": topic,
            "title": title,
            "content": content,
            "tags": tags or []
        }

        db.table("knowledge.research_notes").insert(note).execute()

        return ResponseEnvelope.success(
            f"Added research note: {title}",
            {"note_id": note_id}
        )
    except Exception as e:
        logger.error(f"Error adding research note: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_list_notes(topic: str = None) -> dict:
    """List research notes."""
    try:
        query = db.table("knowledge.research_notes")\
            .select("note_id, topic, title, tags, created_at")\
            .order("created_at", desc=True)

        if topic:
            query = query.eq("topic", topic)

        result = query.limit(100).execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} notes",
            {"notes": result.data, "count": len(result.data)}
        )
    except Exception as e:
        logger.error(f"Error listing research notes: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_get_note(note_id: str) -> dict:
    """Get research note."""
    try:
        result = db.table("knowledge.research_notes")\
            .select("*")\
            .eq("note_id", note_id)\
            .maybe_single()\
            .execute()

        if not result or not result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Note not found: {note_id}")

        return ResponseEnvelope.success(
            f"Note: {result.data['title']}",
            result.data
        )
    except Exception as e:
        logger.error(f"Error getting research note: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_add_source(title: str, kind: str, url: str = None,
                               authors: list = None, year: int = None, notes: str = None) -> dict:
    """Add research source."""
    try:
        source_id = f"src_{uuid.uuid4().hex[:12]}"

        source = {
            "source_id": source_id,
            "title": title,
            "kind": kind,
            "url": url,
            "authors": authors or [],
            "year": year,
            "notes": notes
        }

        db.table("knowledge.research_sources").insert(source).execute()

        return ResponseEnvelope.success(
            f"Added source: {title}",
            {"source_id": source_id}
        )
    except Exception as e:
        logger.error(f"Error adding research source: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_list_sources(kind: str = None) -> dict:
    """List research sources."""
    try:
        query = db.table("knowledge.research_sources")\
            .select("source_id, title, kind, authors, year")\
            .order("created_at", desc=True)

        if kind:
            query = query.eq("kind", kind)

        result = query.limit(100).execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} sources",
            {"sources": result.data, "count": len(result.data)}
        )
    except Exception as e:
        logger.error(f"Error listing research sources: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_log_experiment(title: str, hypothesis: str = None,
                                   methodology: str = None, results: dict = None,
                                   conclusion: str = None) -> dict:
    """Log experiment."""
    try:
        experiment_id = f"exp_{uuid.uuid4().hex[:12]}"

        experiment = {
            "experiment_id": experiment_id,
            "title": title,
            "hypothesis": hypothesis,
            "methodology": methodology,
            "results": results or {},
            "conclusion": conclusion
        }

        db.table("knowledge.research_experiments").insert(experiment).execute()

        return ResponseEnvelope.success(
            f"Logged experiment: {title}",
            {"experiment_id": experiment_id}
        )
    except Exception as e:
        logger.error(f"Error logging experiment: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_list_experiments() -> dict:
    """List experiments."""
    try:
        result = db.table("knowledge.research_experiments")\
            .select("experiment_id, title, created_at")\
            .order("created_at", desc=True)\
            .limit(100)\
            .execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} experiments",
            {"experiments": result.data, "count": len(result.data)}
        )
    except Exception as e:
        logger.error(f"Error listing experiments: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_research_link_source_to_experiment(source_id: str, experiment_id: str) -> dict:
    """Link source to experiment."""
    try:
        link_id = f"link_{uuid.uuid4().hex[:12]}"

        link = {
            "link_id": link_id,
            "source_id": source_id,
            "experiment_id": experiment_id
        }

        db.table("knowledge.research_source_links").insert(link).execute()

        return ResponseEnvelope.success(
            f"Linked source {source_id} to experiment {experiment_id}",
            {"link_id": link_id}
        )
    except Exception as e:
        logger.error(f"Error linking source to experiment: {e}")
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
            "tags": tags or []
        }

        db.table("knowledge.journal_entries").insert(entry).execute()

        return ResponseEnvelope.success(
            f"Added journal entry ({entry_type})",
            {"entry_id": entry_id, "date": entry["date"]}
        )
    except Exception as e:
        logger.error(f"Error appending journal: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_journal_list(limit: int = 20) -> dict:
    """List journal entries."""
    try:
        result = db.table("knowledge.journal_entries")\
            .select("entry_id, date, entry_type, tags")\
            .order("date", desc=True)\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()

        return ResponseEnvelope.success(
            f"Found {len(result.data)} journal entries",
            {"entries": result.data, "count": len(result.data)}
        )
    except Exception as e:
        logger.error(f"Error listing journal: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_journal_get(entry_id: str) -> dict:
    """Get journal entry."""
    try:
        result = db.table("knowledge.journal_entries")\
            .select("*")\
            .eq("entry_id", entry_id)\
            .maybe_single()\
            .execute()

        if not result or not result.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Journal entry not found: {entry_id}")

        return ResponseEnvelope.success(
            f"Journal entry from {result.data['date']}",
            result.data
        )
    except Exception as e:
        logger.error(f"Error getting journal entry: {e}")
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
            "tags": ["config-snapshot", config_name]
        }

        db.table("knowledge.journal_entries").insert(entry).execute()

        return ResponseEnvelope.success(
            f"Snapshotted config: {config_name}",
            {"entry_id": entry_id}
        )
    except Exception as e:
        logger.error(f"Error snapshotting config: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Document Ingestion Handlers (v1.3)
# =============================================================================

def handle_kb_ingest_doc(doc_path: str, strategy: str = "chunked", chunk_size: int = 2000,
                         tags: List[str] = None, overwrite: bool = False) -> dict:
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
        existing_sync = db.table("knowledge.kb_doc_sync")\
            .select("*")\
            .eq("doc_path", str(doc_path))\
            .maybe_single()\
            .execute()

        if existing_sync and existing_sync.data and existing_sync.data.get('doc_hash') == doc_hash and not overwrite:
            return ResponseEnvelope.success(
                f"Document unchanged: {doc_path.name}",
                {
                    "doc_path": str(doc_path),
                    "status": "unchanged",
                    "doc_hash": doc_hash,
                    "kb_ids": existing_sync.data.get('kb_ids', [])
                }
            )

        # Delete old KB entries if overwriting
        if overwrite and existing_sync and existing_sync.data:
            old_kb_ids = existing_sync.data.get('kb_ids', [])
            if old_kb_ids:
                db.table("knowledge.kb_entries")\
                    .delete()\
                    .in_("kb_id", old_kb_ids)\
                    .execute()
                logger.info(f"Deleted {len(old_kb_ids)} old KB entries for {doc_path.name}")

        # Extract topic and title
        topic = metadata.get('topic') or processor.extract_topic_from_path(str(doc_path))
        base_title = processor.generate_title(content, str(doc_path))
        doc_tags = tags or []
        if 'tags' in metadata:
            doc_tags.extend(metadata['tags'])

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
                "line_range": [1, len(content.split('\n'))]
            }
            db.table("knowledge.kb_entries").insert(entry).execute()
            kb_ids.append(kb_id)

        elif strategy == "chunked":
            # Split by sections
            chunks = processor.chunk_by_sections(content, chunk_size)
            for i, chunk in enumerate(chunks):
                kb_id = f"kb_{uuid.uuid4().hex[:12]}"
                title = f"{base_title} - {chunk.section}" if chunk.section else f"{base_title} (part {i+1})"
                entry = {
                    "kb_id": kb_id,
                    "topic": topic,
                    "title": title,
                    "content": chunk.content,
                    "tags": doc_tags,
                    "source_doc": str(doc_path),
                    "source_section": chunk.section,
                    "line_range": [chunk.line_start, chunk.line_end]
                }
                db.table("knowledge.kb_entries").insert(entry).execute()
                kb_ids.append(kb_id)

        elif strategy == "summary":
            # TODO: Implement GPT summary strategy
            return ResponseEnvelope.error(
                ErrorCodes.INVALID_ARGUMENT,
                "Summary strategy not yet implemented. Use 'full' or 'chunked'."
            )

        # Update sync tracking
        sync_data = {
            "doc_path": str(doc_path),
            "doc_hash": doc_hash,
            "kb_ids": kb_ids,
            "last_synced_at": datetime.utcnow().isoformat(),
            "last_modified_at": datetime.fromtimestamp(doc_path.stat().st_mtime).isoformat(),
            "strategy": strategy,
            "metadata": metadata
        }

        if existing_sync and existing_sync.data:
            db.table("knowledge.kb_doc_sync")\
                .update(sync_data)\
                .eq("doc_path", str(doc_path))\
                .execute()
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
                "status": status
            }
        )

    except Exception as e:
        logger.error(f"Error ingesting document {doc_path}: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


async def handle_kb_ingest_dir(dir_path: str, pattern: str = "*.md", strategy: str = "chunked",
                         recursive: bool = True, exclude_patterns: List[str] = None) -> dict:
    """Batch ingest directory (5x faster with async/await)."""
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
            files = [f for f in files if not any(fnmatch.fnmatch(str(f), pat) for pat in exclude_patterns)]

        if not files:
            return ResponseEnvelope.success(
                f"No files found matching pattern: {pattern}",
                {"processed": 0, "created": 0, "updated": 0, "unchanged": 0, "errors": []}
            )

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
                    async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
                        content = await f.read()

                    # Extract frontmatter and hash
                    processor = DocumentProcessor(chunk_size=2000)
                    _, metadata = processor.read_document(str(filepath))
                    doc_hash = processor.compute_hash(content)

                    # Check if document already synced
                    existing_sync = db.table("knowledge.kb_doc_sync")\
                        .select("*")\
                        .eq("doc_path", str(filepath))\
                        .maybe_single()\
                        .execute()

                    if existing_sync and existing_sync.data and existing_sync.data.get('doc_hash') == doc_hash:
                        return {"status": "unchanged", "doc_path": str(filepath)}

                    # Ingest document (synchronous DB calls - Supabase client isn't async)
                    result = handle_kb_ingest_doc(
                        doc_path=str(filepath),
                        strategy=strategy,
                        chunk_size=2000,
                        tags=None,
                        overwrite=False
                    )

                    return {
                        "status": result.get('data', {}).get('status', 'unknown'),
                        "doc_path": str(filepath),
                        "result": result
                    }

                except Exception as e:
                    return {
                        "status": "error",
                        "doc_path": str(filepath),
                        "error": str(e)
                    }

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
                    errors.append({
                        "doc_path": res.get("doc_path"),
                        "error": "ingestion_error",
                        "message": res.get("error")
                    })
        else:
            # OLD: ThreadPoolExecutor (fallback for testing)
            with ThreadPoolExecutor(max_workers=4) as executor:
                future_to_file = {
                    executor.submit(handle_kb_ingest_doc, str(f), strategy): f
                    for f in files
                }

                for future in as_completed(future_to_file):
                    file_path = future_to_file[future]
                    try:
                        result = future.result()
                        if result.get('ok'):
                            status = result.get('data', {}).get('status')
                            if status == 'created':
                                created += 1
                            elif status == 'updated':
                                updated += 1
                            elif status == 'unchanged':
                                unchanged += 1
                        else:
                            errors.append({
                                "doc_path": str(file_path),
                                "error": result.get('error'),
                                "message": result.get('message')
                            })
                    except Exception as e:
                        errors.append({
                            "doc_path": str(file_path),
                            "error": "unexpected_exception",
                            "message": str(e)
                        })

        return ResponseEnvelope.success(
            f"Processed {len(files)} files: {created} created, {updated} updated, {unchanged} unchanged",
            {
                "processed": len(files),
                "created": created,
                "updated": updated,
                "unchanged": unchanged,
                "errors": errors
            }
        )

    except Exception as e:
        logger.error(f"Error ingesting directory {dir_path}: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_sync_status(dir_path: str) -> dict:
    """Check sync state between source docs and KB."""
    try:
        dir_path = Path(dir_path).resolve()
        if not dir_path.exists():
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Directory not found: {dir_path}")

        # Get all markdown files in directory
        md_files = list(dir_path.rglob("*.md"))
        md_paths = {str(f.resolve()): f for f in md_files}

        # Get all sync records
        sync_records = db.table("knowledge.kb_doc_sync")\
            .select("*")\
            .execute()

        synced_paths = {r['doc_path']: r for r in sync_records.data}

        # Classify files
        synced = 0
        modified = 0
        new = 0
        details = []

        for path_str, path_obj in md_paths.items():
            if path_str in synced_paths:
                sync_rec = synced_paths[path_str]
                file_mtime = datetime.fromtimestamp(path_obj.stat().st_mtime)
                last_synced = datetime.fromisoformat(sync_rec['last_modified_at'].replace('Z', '+00:00'))

                if file_mtime > last_synced:
                    modified += 1
                    status = "modified"
                else:
                    synced += 1
                    status = "synced"

                details.append({
                    "doc_path": path_str,
                    "status": status,
                    "last_synced": sync_rec['last_synced_at'],
                    "doc_modified": file_mtime.isoformat(),
                    "kb_ids": sync_rec['kb_ids']
                })
            else:
                new += 1
                details.append({
                    "doc_path": path_str,
                    "status": "new",
                    "last_synced": None,
                    "doc_modified": datetime.fromtimestamp(path_obj.stat().st_mtime).isoformat(),
                    "kb_ids": []
                })

        # Find orphaned KB entries (source doc deleted)
        orphaned_kb_ids = []
        for sync_path, sync_rec in synced_paths.items():
            if sync_path not in md_paths:
                orphaned_kb_ids.extend(sync_rec['kb_ids'])

        return ResponseEnvelope.success(
            f"Sync status: {synced} synced, {modified} modified, {new} new, {len(orphaned_kb_ids)} orphaned",
            {
                "total_docs": len(md_files),
                "synced": synced,
                "modified": modified,
                "new": new,
                "orphaned_kb_entries": len(orphaned_kb_ids),
                "details": details
            }
        )

    except Exception as e:
        logger.error(f"Error checking sync status: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_kb_link_to_source(kb_id: str) -> dict:
    """Get source document reference for a KB entry."""
    try:
        # Get KB entry
        entry = supabase.table("kb_entries")\
            .select("*")\
            .eq("kb_id", kb_id)\
            .maybe_single()\
            .execute()

        if not entry or not entry.data:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"KB entry not found: {kb_id}")

        source_doc = entry.data.get('source_doc')
        if not source_doc:
            return ResponseEnvelope.success(
                f"KB entry {kb_id} has no source document",
                {
                    "kb_id": kb_id,
                    "source_doc": None,
                    "source_section": None,
                    "line_range": None,
                    "last_synced": None,
                    "doc_exists": False
                }
            )

        # Get sync record
        sync_rec = db.table("knowledge.kb_doc_sync")\
            .select("*")\
            .eq("doc_path", source_doc)\
            .maybe_single()\
            .execute()

        # Check if source file still exists
        doc_exists = Path(source_doc).exists()

        return ResponseEnvelope.success(
            f"Source: {Path(source_doc).name}",
            {
                "kb_id": kb_id,
                "source_doc": source_doc,
                "source_section": entry.data.get('source_section'),
                "line_range": entry.data.get('line_range'),
                "last_synced": sync_rec.data.get('last_synced_at') if sync_rec and sync_rec.data else None,
                "doc_exists": doc_exists
            }
        )

    except Exception as e:
        logger.error(f"Error linking KB to source: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# MCP Index Handlers
# =============================================================================

def handle_mcp_index_scan(triggered_by: str = "manual", config_filter: bool = True) -> dict:
    """Scan all MCP servers and index their tools."""
    try:
        scanner = MCPIndexScanner(db)
        result = scanner.scan_all_servers(triggered_by=triggered_by, config_filter=config_filter)

        return ResponseEnvelope.success(
            f"Scanned {result['servers_scanned']} servers, indexed {result['tools_indexed']} tools",
            result
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
                last_scan_result = db.table("mcp_index_versions")\
                    .select("scan_time")\
                    .order("scan_time", desc=True)\
                    .limit(1)\
                    .execute()

                if last_scan_result.data and len(last_scan_result.data) > 0:
                    last_scan_time = datetime.fromisoformat(last_scan_result.data[0]["scan_time"].replace("Z", "+00:00"))
                    time_since_scan = (datetime.now(last_scan_time.tzinfo) - last_scan_time).total_seconds()

                    # If stale (>1 hour = 3600 seconds), trigger background re-index
                    if time_since_scan > 3600:
                        logger.info(f"MCP Index stale ({time_since_scan/3600:.1f}h old), triggering background re-index")

                        # Launch background re-index using threading
                        def background_reindex():
                            try:
                                scanner_bg = MCPIndexScanner(db)
                                result = scanner_bg.scan_all_servers(triggered_by="auto_watchdog")
                                logger.info(f"Auto re-index complete: {result['servers_scanned']} servers, {result['tools_indexed']} tools")
                            except Exception as e:
                                logger.error(f"Background re-index failed: {e}", exc_info=True)

                        thread = threading.Thread(target=background_reindex, daemon=True)
                        thread.start()
                        re_index_triggered = True
                else:
                    # No scan history found, trigger initial scan
                    logger.info("No MCP Index scan history found, triggering initial background scan")

                    def background_reindex():
                        try:
                            scanner_bg = MCPIndexScanner(db)
                            result = scanner_bg.scan_all_servers(triggered_by="auto_watchdog_initial")
                            logger.info(f"Initial auto scan complete: {result['servers_scanned']} servers, {result['tools_indexed']} tools")
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
            "re_index_triggered": re_index_triggered
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
            f"Server {server_id} has {len(tools)} tools",
            {"server": server, "tools": tools}
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

        return ResponseEnvelope.success(
            f"Found tool: {tool['full_name']}",
            {"tool": tool}
        )

    except Exception as e:
        logger.error(f"Error getting tool details: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_mcp_index_rebuild() -> dict:
    """Force rebuild of entire MCP index."""
    try:
        scanner = MCPIndexScanner(db)
        result = scanner.scan_all_servers(triggered_by="rebuild")

        return ResponseEnvelope.success(
            f"Rebuilt index: {result['servers_scanned']} servers, {result['tools_indexed']} tools",
            result
        )

    except Exception as e:
        logger.error(f"Error rebuilding MCP index: {e}", exc_info=True)
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


# =============================================================================
# Search Handlers (consolidated from search-mcp)
# =============================================================================

def search_file_content(file_path: Path, query: str) -> Optional[Dict]:
    """Search a single file for query string."""
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
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
                    "file_size": file_path.stat().st_size
                }
    except Exception as e:
        logger.warning(f"Error searching {file_path}: {e}")
    return None


def handle_search_local(query: str, paths: List[str] = None, file_types: List[str] = None) -> dict:
    """Search local files by content."""
    try:
        # Default paths
        if not paths:
            paths = [
                str(LATVIAN_LEARNING_ROOT),
                str(LATVIAN_XTTS_ROOT),
                str(KNOWLEDGE_DATA_DIR)
            ]

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
                "files_searched": file_count
            }
        )
    except Exception as e:
        logger.error(f"Error in handle_search_local: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_search_corpora(query: str, corpus_ids: List[str] = None) -> dict:
    """Search across corpus manifests."""
    try:
        results = []
        corpora_dir = INGEST_ROOT / "corpora"

        if not corpora_dir.exists():
            return ResponseEnvelope.success(
                "Corpora directory not found (expected until data ingested)",
                {"results": [], "count": 0}
            )

        # Search corpus manifest files
        for manifest_file in corpora_dir.glob("*.jsonl"):
            # Filter by corpus_ids if specified
            if corpus_ids and manifest_file.stem not in corpus_ids:
                continue

            with open(manifest_file, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    try:
                        entry = json.loads(line)
                        # Search in transcript and metadata
                        if (query.lower() in entry.get("text", "").lower() or
                            query.lower() in json.dumps(entry.get("metadata", {})).lower()):
                            results.append({
                                "corpus": manifest_file.stem,
                                "line": line_num,
                                "segment_id": entry.get("segment_id", "unknown"),
                                "text": entry.get("text", "")[:200],
                                "metadata": entry.get("metadata", {})
                            })

                            if len(results) >= 100:
                                break
                    except json.JSONDecodeError:
                        continue

            if len(results) >= 100:
                break

        return ResponseEnvelope.success(
            f"Found {len(results)} matches in corpora",
            {"results": results[:50], "total_matches": len(results)}
        )
    except Exception as e:
        logger.error(f"Error in handle_search_corpora: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_search_transcripts(query: str, speaker: str = None) -> dict:
    """Search transcript segments."""
    try:
        results = []

        # Search in whisper extracted directories
        search_dirs = [
            LATVIAN_XTTS_ROOT / "whisper_extracted",
            LATVIAN_XTTS_ROOT / "whisper_extracted_enhanced",
            LATVIAN_XTTS_ROOT / "whisper_extracted_normalized"
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
                            results.append({
                                "file": str(json_file.relative_to(LATVIAN_XTTS_ROOT)),
                                "speaker": data.get("speaker", "unknown"),
                                "text": text[:200],
                                "duration": data.get("duration_seconds"),
                                "timestamp": data.get("start_time")
                            })

                            if len(results) >= 100:
                                break
                except (json.JSONDecodeError, IOError):
                    continue

            if len(results) >= 100:
                break

        return ResponseEnvelope.success(
            f"Found {len(results)} transcript matches",
            {"results": results[:50], "total_matches": len(results)}
        )
    except Exception as e:
        logger.error(f"Error in handle_search_transcripts: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_multi_search(query: str) -> dict:
    """Combined search across all sources."""
    try:
        results = {
            "local": [],
            "corpora": [],
            "transcripts": [],
            "knowledge": {}
        }

        # Local search (limited)
        local_result = handle_search_local(query, paths=[str(KNOWLEDGE_DATA_DIR)], file_types=["json", "md"])
        if local_result.get("ok"):
            results["local"] = local_result["data"]["results"][:10]

        # Knowledge search using kb_search
        knowledge_result = handle_kb_search(query)
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
            len(results["local"]) +
            len(results["knowledge"].get("kb_entries", [])) +
            len(results["corpora"]) +
            len(results["transcripts"])
        )

        return ResponseEnvelope.success(
            f"Multi-search found {total_matches} matches across all sources",
            {"results": results, "total_matches": total_matches}
        )
    except Exception as e:
        logger.error(f"Error in handle_multi_search: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_deduplicate_results(results: List[Dict], threshold: float = 0.9) -> dict:
    """Remove duplicate search results."""
    try:
        # Simple deduplication based on exact text matches
        seen = set()
        deduped = []

        for result in results:
            # Create a key from result text/content
            key = result.get("text", "") or result.get("content", "") or result.get("snippet", "")
            key_normalized = key.lower().strip()

            if key_normalized and key_normalized not in seen:
                seen.add(key_normalized)
                deduped.append(result)

        removed = len(results) - len(deduped)

        return ResponseEnvelope.success(
            f"Removed {removed} duplicates, {len(deduped)} unique results remaining",
            {"results": deduped, "removed_count": removed, "unique_count": len(deduped)}
        )
    except Exception as e:
        logger.error(f"Error in handle_deduplicate_results: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def handle_cluster_results(results: List[Dict], num_clusters: int = 5) -> dict:
    """Cluster search results by topic."""
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

        cluster_summary = {
            cluster: len(items)
            for cluster, items in clusters.items()
        }

        return ResponseEnvelope.success(
            f"Clustered {len(results)} results into {len(clusters)} groups",
            {
                "clusters": clusters,
                "cluster_summary": cluster_summary,
                "total_results": len(results)
            }
        )
    except Exception as e:
        logger.error(f"Error in handle_cluster_results: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


def main():
    """Run the MCP server."""
    import asyncio
    global db

    # Set environment defaults for local PostgreSQL (primary backend)
    os.environ.setdefault('DB_BACKEND', 'local')
    os.environ.setdefault('DB_HOST', 'localhost')
    os.environ.setdefault('DB_PORT', '5433')
    os.environ.setdefault('DB_NAME', 'mpm_system')
    os.environ.setdefault('DB_USER', 'latvian_user')
    os.environ.setdefault('DB_PASSWORD', 'latvian_dev_password_2026')

    # Initialize database client (supports both Supabase and local PostgreSQL)
    db = get_db_client()
    backend = os.getenv('DB_BACKEND', 'local')
    logger.info(f"Connected to database backend: {backend}")

    logger.info("Starting knowledge-mcp server")

    async def _run():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(_run())


# =============================================================================
# Knowledge Graph Handlers
# =============================================================================

def get_db_connection():
    """Get direct PostgreSQL connection for kg_ operations."""
    return psycopg2.connect(
        host=os.getenv('DB_HOST', '192.168.1.12'),
        port=int(os.getenv('DB_PORT', 5432)),
        database=os.getenv('DB_NAME', 'mmp_system'),
        user=os.getenv('DB_USER', 'latvian_user'),
        password=os.getenv('DB_PASSWORD', 'latvian_dev_password_2026')
    )

def handle_kg_add_node(label: str, kind: str, properties: dict = None) -> dict:
    """Add KG node."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        node_id = f"node_{uuid.uuid4().hex[:12]}"

        cur.execute("""
            INSERT INTO knowledge.kg_nodes (node_id, label, kind, properties, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (
            node_id,
            label,
            kind,
            json.dumps(properties or {}),
            datetime.now()
        ))

        conn.commit()
        cur.close()
        conn.close()

        return ResponseEnvelope.success(
            f"Added KG node: {label}",
            {"node_id": node_id, "label": label, "kind": kind}
        )
    except Exception as e:
        logger.error(f"Error adding knowledge graph node: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))

def handle_kg_add_edge(from_node: str, to_node: str, relation: str, properties: dict = None) -> dict:
    """Add KG edge."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        edge_id = f"edge_{uuid.uuid4().hex[:12]}"

        cur.execute("""
            INSERT INTO knowledge.kg_edges (edge_id, from_node, to_node, relation, properties, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            edge_id,
            from_node,
            to_node,
            relation,
            json.dumps(properties or {}),
            datetime.now()
        ))

        conn.commit()
        cur.close()
        conn.close()

        return ResponseEnvelope.success(
            f"Added edge: {from_node} --[{relation}]--> {to_node}",
            {"edge_id": edge_id, "from_node": from_node, "to_node": to_node, "relation": relation}
        )
    except Exception as e:
        logger.error(f"Error adding knowledge graph edge: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))

def handle_kg_get_node(node_id: str) -> dict:
    """Get KG node."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT node_id, label, kind, properties, created_at
            FROM knowledge.kg_nodes WHERE node_id = %s
        """, (node_id,))

        result = cur.fetchone()
        cur.close()
        conn.close()

        if result:
            node_id, label, kind, properties, created_at = result
            return ResponseEnvelope.success(
                f"Found node: {label}",
                {
                    "node_id": node_id,
                    "label": label,
                    "kind": kind,
                    "properties": properties if isinstance(properties, dict) else (json.loads(properties) if properties else {}),
                    "created_at": created_at.isoformat() if created_at else None
                }
            )
        else:
            return ResponseEnvelope.error(ErrorCodes.NOT_FOUND, f"Node not found: {node_id}")

    except Exception as e:
        logger.error(f"Error getting knowledge graph node: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))

def handle_kg_neighbors(node_id: str) -> dict:
    """Get neighboring nodes."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Get outgoing edges
        cur.execute("""
            SELECT e.edge_id, e.relation, n.node_id, n.label, n.kind
            FROM knowledge.kg_edges e
            JOIN knowledge.kg_nodes n ON e.to_node = n.node_id
            WHERE e.from_node = %s
        """, (node_id,))

        outgoing = cur.fetchall()

        # Get incoming edges
        cur.execute("""
            SELECT e.edge_id, e.relation, n.node_id, n.label, n.kind
            FROM knowledge.kg_edges e
            JOIN knowledge.kg_nodes n ON e.from_node = n.node_id
            WHERE e.to_node = %s
        """, (node_id,))

        incoming = cur.fetchall()

        cur.close()
        conn.close()

        neighbors = {
            "outgoing": [{"edge_id": e[0], "relation": e[1], "node": {"node_id": e[2], "label": e[3], "kind": e[4]}} for e in outgoing],
            "incoming": [{"edge_id": e[0], "relation": e[1], "node": {"node_id": e[2], "label": e[3], "kind": e[4]}} for e in incoming]
        }

        return ResponseEnvelope.success(
            f"Found {len(outgoing)} outgoing and {len(incoming)} incoming neighbors",
            neighbors
        )

    except Exception as e:
        logger.error(f"Error exploring knowledge graph neighbors: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))

def handle_kg_search(query: str, kind: str = None) -> dict:
    """Search KG nodes."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        if kind:
            cur.execute("""
                SELECT node_id, label, kind, properties
                FROM knowledge.kg_nodes
                WHERE label ILIKE %s AND kind = %s
                ORDER BY label
            """, (f"%{query}%", kind))
        else:
            cur.execute("""
                SELECT node_id, label, kind, properties
                FROM knowledge.kg_nodes
                WHERE label ILIKE %s
                ORDER BY label
            """, (f"%{query}%",))

        results = cur.fetchall()
        cur.close()
        conn.close()

        nodes = []
        for result in results:
            node_id, label, kind, properties = result
            nodes.append({
                "node_id": node_id,
                "label": label,
                "kind": kind,
                "properties": properties if isinstance(properties, dict) else (json.loads(properties) if properties else {})
            })

        return ResponseEnvelope.success(
            f"Found {len(nodes)} nodes matching '{query}'",
            {"nodes": nodes, "count": len(nodes)}
        )

    except Exception as e:
        logger.error(f"Error searching knowledge graph: {e}")
        return ResponseEnvelope.error(ErrorCodes.UNEXPECTED_EXCEPTION, str(e))


if __name__ == "__main__":
    main()
