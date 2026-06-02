# Lore

**`lore-knowledge-mcp`** · Operational knowledge layer for engineering teams and their AI agents.

[![PyPI version](https://img.shields.io/pypi/v/lore-knowledge-mcp)](https://pypi.org/project/lore-knowledge-mcp/)
[![CI](https://github.com/davidgut1982/lore-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidgut1982/lore-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11+-green)](https://python.org)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple)](https://modelcontextprotocol.io)
[![Hybrid Search](https://img.shields.io/badge/search-hybrid%20%2B%20semantic-blueviolet)](https://github.com/davidgut1982/lore-mcp#semantic-search)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Lore demo](docs/demo.gif)

---

## The Problem

Your agents start every session knowing nothing about your systems. Every runbook you've written. Every gotcha you've hit. Every incident you've debugged. None of it carries forward.

You re-explain. They re-discover. Context vanishes when the session ends.

**Lore fixes that.**

```
Without Lore                     With Lore
─────────────────────────────    ──────────────────────────────────
Agent starts fresh every time    Agent queries Lore on startup
"How does our infra work?"       Gets: topology, gotchas, runbooks,
You re-explain everything        past incidents, verified decisions
Context lost at session end      Knowledge persists across all sessions
```

---

## How It's Different

| Tool | Built for | What it remembers | Agent-native |
|---|---|---|---|
| OB1 / personal memory | One person | Your thoughts and captures | No |
| Mem0 / Zep | App developers | User preferences, conversations | Partially |
| Confluence / Notion | Human teams | Documentation (human-browsed) | No |
| **Lore** | **Engineering teams + AI agents** | **How your systems actually work — searchable by meaning, not just keywords** | **Yes** |

Lore is not a second brain. It's the operational intelligence your agents need to work in *your* environment — not just any environment.

---

## What Lore Does

### Knowledge Base

Your team's operational knowledge — always queryable by any agent. Capture the things that matter: runbooks, hard-won gotchas, architecture decisions, deployment state. Every entry carries attribution so agents know who wrote it and whether a human has verified it.

### Investigations

When something breaks, open a structured investigation. Document the symptom, test hypotheses, record what you tried and what you found. Six months later when the same issue resurfaces — different engineer, different agent — the trail is there.

### Journal

A permanent record of milestones, architecture decisions, and buying decisions. The kind of thing that lives in someone's head until they leave the team.

---

## Built for Multi-Agent Systems

In a multi-agent environment, provenance matters. Every Lore entry carries `author`, `source_type`, and `verified`.

```
kb_search("proxmox lxc dns")

  [1] "LXC inherits host resolv.conf — Tailscale breaks containers"
      david · human · ✓ verified

  [2] "LXC DNS fix after Tailscale install"
      engineer-agent · agent · unreviewed

  [3] "LXC DNS configuration reference"
      research-agent · agent · ✗ disputed
```

Your agents know: result 1 is production-safe. Result 2, spot-check before acting. Result 3, review first.

---

## Semantic Search

Lore finds entries by meaning, not just keywords. Search "DNS broken in containers" and it returns an entry titled "LXC containers inherit resolv.conf from the host" — no keyword overlap required.

Powered by local sentence-transformers embeddings (no API key, no external calls), combined with lexical full-text search and Reciprocal Rank Fusion. The same model used by mcp-memory-service, fully self-hosted. On SQLite the lexical leg uses FTS5; on PostgreSQL it uses a GIN full-text index plus pgvector for the semantic leg.

### Enable it

```bash
pip install lore-knowledge-mcp[semantic]
LORE_SEMANTIC_SEARCH=true lore-mcp
```

### Search modes

`kb_search` resolves its mode from (in order): an explicit `search_mode`/`semantic`/`hybrid` argument, then `LORE_SEARCH_MODE_DEFAULT`, then the built-in default of `hybrid`. Every search response echoes `requested_mode` (the caller's intent) alongside `search_mode` (the mode actually executed, after any degradation).

| Mode | When to use |
|---|---|
| `fts` | Exact term matches. |
| `semantic` | Meaning-based retrieval, no keyword overlap needed. |
| `hybrid` | Best of both — lexical + vector via RRF (default). |

> The `summary` mode was removed — passing `search_mode="summary"` now returns a validation error. Lore is LLM-free by design; summarisation is the caller's responsibility.

### Backfill existing KB

If you already have entries, generate embeddings for them:

```
kb_backfill_embeddings()    # idempotent, safe to re-run
kb_embedding_status()       # check coverage
```

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `LORE_SEMANTIC_SEARCH` | `false` | Master switch — off = lexical-only behaviour. |
| `LORE_SEARCH_MODE_DEFAULT` | `hybrid` | Default mode for `kb_search` when no mode is passed (`fts`, `semantic`, or `hybrid`). |
| `LORE_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | 384d, ~90MB, English-optimized. |
| `LORE_RRF_K` | `10` | Increase to 30–60 for corpora >10k entries. |

For multilingual content, set `LORE_EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2` (same 384d, no schema change).

---

## Automatic Memory Extraction

Lore can extract durable memories from agent conversations automatically. At the end of a session, conversation turns are sent asynchronously to a fast LLM, which extracts facts, preferences, goals, events, and system facts — then deduplicates them against the existing KB before writing.

- **Opt-in** — disabled by default (`auto_extract.enabled: false`).
- **Two providers** — OpenRouter (default, simple setup) or Cerebras direct API (gpt-oss-120b, 300+ TPS, high prompt-cache hit rate).
- **Graceful degradation** — a missing API key, HTTP error, or bad JSON returns an empty result silently; it never raises and never blocks the session.
- **Auditable** — every auto-extracted entry is tagged `source:auto-extracted`, with an optional review queue (`topic="auto-memory-pending"`) for human approval.

Set `OPENROUTER_API_KEY` (or `CEREBRAS_API_KEY`) and enable it in your plugin config.

→ **[Full setup guide: docs/auto-extraction-setup.md](docs/auto-extraction-setup.md)** — API keys, provider config, tuning thresholds, review mode, and inspecting or removing extracted entries.

---

## Automating Lore in Your Workflow

Add one line to every agent's system prompt and one entry to `~/.mcp.json` — that's the entire integration. Each phase of your engineering workflow reads prior knowledge from Lore and writes its findings back, so nothing is re-discovered from scratch.

→ **[How to wire Lore into a 6-phase multi-agent pipeline](docs/multi-agent-workflow.md)** — full walkthrough with code examples for every phase: research, architecture review, implementation, adversarial code review, QA, and documentation.

---

## Quick Start

**No database setup required.** Lore runs out of the box with SQLite.

### 1. Install

```bash
pip install lore-knowledge-mcp
```

#### Optional: semantic search

```bash
pip install lore-knowledge-mcp[semantic]
```

Then set `LORE_SEMANTIC_SEARCH=true`. See [Semantic Search](#semantic-search) for details.

### 2. Start the server

```bash
# Stdio mode (for local MCP clients like Claude Code)
lore-mcp

# HTTP mode (for remote or multi-agent access)
lore-mcp --host 0.0.0.0 --port 8000

# HTTP mode WITH authentication (recommended for teams / LAN exposure)
LORE_API_KEY="$(openssl rand -hex 32)" lore-mcp --host 0.0.0.0 --port 8000
```

#### Authentication (`LORE_API_KEY`)

HTTP auth is **opt-in** and off by default:

- **`LORE_API_KEY` unset** → the HTTP server is **open** (no auth), exactly as
  before. This keeps existing no-auth deployments working. When you bind to a
  non-localhost host (`0.0.0.0` or a LAN IP) without a key, Lore logs a
  prominent startup **WARNING** that the server is reachable on your network
  with no authentication.
- **`LORE_API_KEY` set** → every HTTP/SSE request must include
  `Authorization: Bearer <key>`. Missing or wrong tokens get
  `401 {"error":"unauthorized"}` (token compared in constant time). Health
  endpoints (`/health`, `/healthz`, `/`) stay open so liveness probes keep
  working. **stdio mode is never affected** — it has no network surface.

The same rule applies to the HTTP entry point (`lore-mcp --host/--port`,
which invokes the FastMCP server).

**CORS:** origins default to `*` with credentials disabled (the spec forbids
`*` + credentials). Set `LORE_CORS_ORIGINS` to a comma-separated allow-list
(e.g. `https://app.example.com,https://admin.example.com`) to restrict origins;
credentialed CORS is enabled automatically when origins are explicit.

### 3. Add to your MCP client

**Claude Code / Claude Desktop** — add to `~/.mcp.json`:

```json
{
  "mcpServers": {
    "lore": {
      "type": "stdio",
      "command": "lore-mcp"
    }
  }
}
```

Or for HTTP mode (recommended for teams). When the server is started with
`LORE_API_KEY` set, include a matching bearer token in the client config:

```json
{
  "mcpServers": {
    "lore": {
      "type": "http",
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <your LORE_API_KEY>"
      }
    }
  }
}
```

If the server is started without `LORE_API_KEY`, omit the `headers` block — the
endpoint is open.

That’s it. Lore is ready.

---

## Tool Reference

### Knowledge Base
| Tool | What it does |
|---|---|
| `kb_add` | Add an entry. Accepts `author`, `source_type` for attribution. |
| `kb_search` | Semantic / hybrid / FTS search with optional topic filter. |
| `kb_get` | Fetch full entry by ID. |
| `kb_get_batch` | Fetch multiple entries by ID in a single call (re-keyed by `kb_id`). |
| `kb_list` | List entries, filter by topic. |
| `kb_update` | Update content, tags, or set `verified` flag. |
| `kb_delete` | Delete entry (requires `confirm=true`). |
| `kb_backfill_embeddings` | Generate embeddings for existing entries (idempotent). |
| `kb_embedding_status` | Report embedding coverage across the KB. |

### Investigations
| Tool | What it does |
|---|---|
| `investigation_add` | Open or add to an investigation. |
| `investigation_list` | List investigations, filter by topic. |
| `investigation_get` | Fetch full investigation by ID. |
| `investigation_log_experiment` | Log a structured hypothesis → result → conclusion. |
| `investigation_list_experiments` | List all logged experiments. |
| `investigation_delete_note` | Hard-delete a note (requires `confirm=true`; production guard). |
| `investigation_delete_experiment` | Hard-delete an experiment (requires `confirm=true`; production guard). |

### Journal
| Tool | What it does |
|---|---|
| `journal_append` | Add a milestone, decision, or reflection. |
| `journal_list` | List recent entries (default 20). |
| `journal_get` | Fetch entry by ID. |
| `journal_delete` | Hard-delete an entry (requires `confirm=true`; production guard). |
| `snapshot_config` | Snapshot a config object to the journal. |

### Document Ingestion
| Tool | What it does |
|---|---|
| `kb_ingest_doc` | Ingest a markdown file into the KB (`strategy`: `full` or `chunked`). |
| `kb_ingest_dir` | Batch-ingest a directory, with change detection. |
| `kb_sync_status` | Check what's changed since last sync. |

### MCP Index
| Tool | What it does |
|---|---|
| `mcp_index_scan` | Scan all configured MCP servers and index their tools. |
| `mcp_index_search` | Search indexed tools by description. |
| `mcp_index_get_server` | Get all tools for a specific MCP server. |
| `mcp_index_rebuild` | Force a full rescan. |

### Search
| Tool | What it does |
|---|---|
| `multi_search` | Search across KB, investigations, journal, and transcripts at once. |
| `search_local` | Search local files by content. |
| `search_transcripts` | Search Whisper transcript segments. |
| `deduplicate_results` | Deduplicate a result set by similarity threshold. |
| `cluster_results` | Cluster results by topic. |

---

## Backends

| | SQLite | PostgreSQL |
|---|---|---|
| Setup required | None | Existing PostgreSQL instance |
| Best for | Solo developers, local use | Teams, shared agents, production |
| Config | `DB_BACKEND=sqlite` (default) | `DB_BACKEND=postgres` + connection vars |
| Data location | `./knowledge-data/` (override with `KNOWLEDGE_DATA_DIR`) | Your database |
| Semantic search | sqlite-vec + FTS5 | pgvector + GIN full-text index |

**SQLite is the default.** No configuration needed — just install and run. The
SQLite database and any local-file search corpus live under
`KNOWLEDGE_DATA_DIR`, which defaults to `./knowledge-data` (a portable, relative
path — set it to an absolute path for a stable on-disk location).

**PostgreSQL** is for teams who want a shared knowledge layer accessible from
multiple machines or agents simultaneously. `DB_BACKEND=postgres` (and the
`postgresql` alias) select the bundled local PostgreSQL client — the same path
as `DB_BACKEND=local`. Connection defaults are generic (`DB_NAME=lore`,
`DB_USER=lore_user`); override them with the connection variables below. On
PostgreSQL, hybrid search uses a combined title+content GIN full-text index for
the lexical leg and pgvector for the semantic leg.

```bash
# PostgreSQL setup
export DB_BACKEND=postgres          # "postgresql" and "local" also work
export DB_HOST=your-db-host
export DB_PORT=5432
export DB_NAME=lore                 # default: lore
export DB_USER=your-user            # default: lore_user
export DB_PASSWORD=your-password
lore-mcp
```

### Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `DB_BACKEND` | `sqlite` | `sqlite`, `postgres`/`postgresql`/`local`, or `supabase`. |
| `KNOWLEDGE_DATA_DIR` | `./knowledge-data` | Root for the SQLite DB and local-file search. Portable by default — no `/srv` paths. |
| `DB_NAME` | `lore` | PostgreSQL database name. |
| `DB_USER` | `lore_user` | PostgreSQL user. |
| `LORE_SEMANTIC_SEARCH` | `false` | Master switch for semantic/hybrid search (requires the `[semantic]` extra). |
| `LORE_SEARCH_MODE_DEFAULT` | `hybrid` | Default `kb_search` mode when none is passed (`fts`, `semantic`, `hybrid`). |
| `OPENROUTER_API_KEY` | _(unset)_ | API key for automatic memory extraction via OpenRouter. See [Automatic Memory Extraction](#automatic-memory-extraction). |
| `CEREBRAS_API_KEY` | _(unset)_ | API key for automatic memory extraction via the Cerebras direct API. |
| `LATVIAN_LEARNING_ROOT` | _(unset)_ | Optional corpus root for `search_local`. Unset → that source is skipped. |
| `LATVIAN_XTTS_ROOT` | _(unset)_ | Optional transcript root for `search_transcripts`. Unset → returns a clean "not configured" result. |
| `INGEST_ROOT` | _(unset)_ | Optional corpora root for `search_corpora`. Unset → returns a clean "not configured" result. |
| `LORE_API_KEY` | _(unset)_ | **Opt-in HTTP auth.** Set → require `Authorization: Bearer <key>` on HTTP/SSE requests (401 otherwise). Unset → HTTP is open (and a warning is logged on non-localhost binds). stdio is never affected. |
| `LORE_CORS_ORIGINS` | `*` | Comma-separated CORS allow-list. `*` (default) disables credentials per the CORS spec; explicit origins enable credentialed CORS. |

The deployment-specific search roots (`LATVIAN_LEARNING_ROOT`,
`LATVIAN_XTTS_ROOT`, `INGEST_ROOT`) are **unset by default**. When a root is not
configured, the dependent search tool returns an empty, clearly-labelled "not
configured" result instead of scanning a nonexistent path — so a fresh install
works out of the box.

---

## Hermes Memory Provider

A Hermes agent memory provider plugin that backs conversation memory with Lore is available as a separate package:

**[hermes-lore-plugin](https://github.com/davidgut1982/hermes-lore-plugin)** — drop-in memory provider for the [Hermes agent](https://github.com/NousResearch/Hermes). Stores KB entries in Lore, prefetches relevant context on session start, and deduplicates before storing. It also drives the [automatic memory extraction](#automatic-memory-extraction) pipeline.

---

## License

MIT — see [LICENSE](LICENSE)
