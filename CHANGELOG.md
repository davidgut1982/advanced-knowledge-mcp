# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed
- **multi_search KB results path** (#20): pin down the contract that
  `multi_search` surfaces the same KB entries as a direct `kb_search` call for
  the same query. `handle_multi_search` continues to delegate to
  `handle_kb_search` verbatim (no query rewriting, no extra filters); a new
  regression test guards the field shape (`knowledge.kb_entries`) and result
  parity end-to-end. Adds explanatory comments documenting the contract so
  future refactors don't regress the path silently.

### Added
- **`journal_delete`** (#21): hard-delete a journal entry by `entry_id`.
  Requires `confirm=True`; in `LORE_ENV=production` additionally requires
  `confirm_production=True` (production guard). Missing rows return a clean
  `not_found` envelope; missing confirmation returns `invalid_input`.
- **`investigation_delete_note`** (#21): hard-delete an investigation note by
  `note_id` with the same confirmation + production-guard contract as
  `journal_delete`.
- **`investigation_delete_experiment`** (#21): hard-delete an investigation
  experiment by `experiment_id` with the same safety contract. Tool count rises
  from 38 to 41.

### Changed
- **`cluster_results` schema trimmed** (#22): the misleading
  `num_clusters` / `n_clusters` parameters have been removed from the input
  schema and handler signature. They were always silently ignored — the
  implementation buckets by `source_type` and the cluster count is determined
  entirely by the input. Behaviour is unchanged; the schema now reflects
  reality. Tool description updated accordingly.

## [0.8.5] - 2026-05-28

### Changed
- **Entry point consolidation**: `lore-mcp` console script now invokes
  `lore.server_fastmcp:main` (FastMCP) directly. Stdio + HTTP modes are
  unchanged; CLI flags are preserved. Production deployments already used
  FastMCP (`python -m lore.server_fastmcp`), so there is no operational change.
- `lore.server:main` is now a deprecation shim that delegates to
  `lore.server_fastmcp:main`. `python -m lore.server` emits a
  `DeprecationWarning` but still works.
- `--version` output normalised to `lore-mcp <version>` across both entry
  points (previously `lore.server_fastmcp` printed `lore-mcp (fastmcp) ...`).
- `docker/knowledge-mcp.service` `ExecStart` updated to the canonical
  `lore-mcp --host 0.0.0.0 --port 5555` invocation.

### Removed
- `lore.mcp_http_wrapper_sse` (the legacy SSE/JSON-RPC HTTP wrapper). FastMCP's
  `/mcp`, `/jsonrpc`, `/health`, and `/stream` endpoints provide the full HTTP
  surface. The retired wrapper returned SSE-framed responses
  (`event: message\ndata: <json>`); FastMCP returns bare JSON (which all known
  clients — Hermes, mcpo, OWUI — already use).
- Unused `asyncpg>=0.28.0` core dependency. Lore's PostgreSQL access is
  synchronous (`psycopg2`); nothing imports `asyncpg`. Dropping it trims
  ~15 MB from the install footprint with no behavioural change.

### Added
- **Tool schema snapshot tests** (syrupy) — 38 MCP tools each get a snapshot
  of name + description + inputSchema + outputSchema. Any accidental interface
  change fails CI with a precise per-tool diff, guarding the MCP surface against
  unintended regressions.
- **Postgres integration CI job** — 45 Postgres tests (KB CRUD, FTS, semantic
  and hybrid search via pgvector, telemetry) run on every push and pull request.
- `develop` branch added to CI push triggers so the integration job validates
  before any release merge.

### Fixed
- **Lazy DB initialisation**: `lore.server` no longer attempts a DB connection
  at import time. The module global is `db = None`; it is initialised in
  `main()` (stdio/HTTP) and `lore_lifespan` (FastMCP) before any handler runs.
  Importing the module no longer triggers any connection attempt or error log.
- Resolved stale TODOs: doc-summary strategy clarified (LLM-free; delegated to
  caller/scheduler, tracked in [#19](https://github.com/davidgut1982/lore-mcp/issues/19));
  `mcp_index_scan` `modified` change-detection implemented (compares `tool_count`
  against prior stored value).

### Testing
- Coverage floor raised from 30% to 66% (measured 68.76% in CI).
- 924 unit tests + 45 Postgres integration tests.
- `make lint` now mirrors CI exactly (`ruff check` + `ruff format --check` on
  `src/` and `tests/`).

## [0.8.4] - 2026-05-27

### Added
- **Opt-in bearer-token auth for HTTP transports (P1-8).** Set `LORE_API_KEY`
  to require `Authorization: Bearer <key>` on all HTTP/SSE requests; missing or
  wrong tokens get a `401 {"error":"unauthorized"}` (constant-time compare).
  Auth is **opt-in**: when `LORE_API_KEY` is unset the HTTP surfaces behave
  exactly as before (open), so existing no-auth deployments keep working. The
  check is applied consistently across all three HTTP surfaces — `lore.server`
  (`--host/--port`), `lore.server_fastmcp` (FastMCP HTTP), and the SSE wrapper
  `lore.mcp_http_wrapper_sse` — via a shared `lore.http_auth` middleware. Health
  endpoints (`/health`, `/healthz`, `/ready`, `/readyz`, `/`) are exempt so
  liveness probes work even with a key set. stdio mode is unaffected.
- A prominent startup **WARNING** is logged when binding HTTP to a non-localhost
  host (`0.0.0.0` / a LAN IP) without `LORE_API_KEY` set, since the server is
  then reachable on the network with no authentication.
- `LORE_CORS_ORIGINS` (comma-separated, default `*`) makes the CORS allow-list
  configurable.

### Fixed
- **Invalid CORS configuration.** The HTTP transports previously sent
  `allow_origins=["*"]` together with `allow_credentials=True`, which violates
  the CORS spec and is rejected by browsers. Credentials are now disabled
  automatically whenever origins are wildcard (`*`); set explicit origins via
  `LORE_CORS_ORIGINS` to re-enable credentialed CORS.
- **Import-time DB double-init (P1-5).** `lore.server` connected to the database
  at *import* time (`db = get_db_client()` at module scope). This ran before
  `main()` / the FastMCP lifespan could apply the `DB_BACKEND=sqlite` default,
  so it attempted a connection against whatever `DB_BACKEND` was in the
  environment (defaulting to Supabase), logged a spurious failure, and opened a
  stray client that was immediately discarded when startup re-initialised it.
  The module global is now `db = None`; it is initialised exactly once — in
  `lore.server.main()` (stdio/HTTP) and in `lore.server_fastmcp.lore_lifespan`
  (FastMCP) — before any handler runs. Importing the module no longer triggers
  any connection or error log. Handler call sites are unchanged.
- **`mcp_index_scan` never reported modified servers (P1-7).** The scanner's
  `changes["modified"]` list was hardcoded to `[]` behind a `# TODO`, so an
  advertised capability silently under-reported. It now compares each scanned
  server's `tool_count` against the prior stored value: a server present in both
  the previous and current scan whose tool count changed (tools added/removed)
  is reported as modified. A prior row with no recorded `tool_count` is left out
  rather than reported as a false positive.

### Changed
- **Removed the unused `asyncpg>=0.28.0` core dependency (P1-4).** Lore's
  PostgreSQL access is synchronous (`psycopg2`); nothing imports `asyncpg`.
  Dropping it trims the install footprint with no behavioural change.
- **Clarified the `kb_ingest_doc` `summary` strategy (P1-6).** Lore is
  intentionally LLM-free, so it does not generate summaries — that belongs to
  the LLM-capable caller/scheduler. Replaced the misleading
  `# TODO: Implement GPT summary strategy` with a clear comment and error
  message pointing callers to `full`/`chunked`, and updated the tool-schema
  description accordingly. Tracked for documentation in
  [#19](https://github.com/davidgut1982/lore-mcp/issues/19).

### Tests
- New `tests/test_http_auth.py` — covers the shared auth helpers and verifies
  the middleware on all three transports: key-unset back-compat, 401 on
  missing/wrong/correct bearer, health-endpoint exemption, CORS credentials fix,
  and the non-localhost insecure-bind warning.
- New `tests/test_mcp_index_scanner.py` (P1-7) — covers `scan_all_servers`
  change detection: added (new server), modified (tool_count changed), unchanged
  (no diff), and the no-false-positive case when the prior `tool_count` is `None`.
- New `tests/test_basic.py::test_import_does_not_connect_to_db` (P1-5) —
  re-imports `lore.server` under a `get_db_client` tripwire and asserts it is
  never called at import and `server.db is None` until startup initialises it.
- **Postgres integration CI job (P1-2).** A dedicated `integration` job in
  `.github/workflows/ci.yml` spins up a real PostgreSQL service container and
  runs 45 tests covering KB CRUD, FTS, semantic search, and pgvector hybrid
  paths on every push and pull request. `develop` branch added to CI push
  triggers alongside `main`.
- Fixed misleading `create_local_schema.sql` — primary key column changed from
  `INTEGER` to `TEXT` to match the application schema (`kb_id` is a UUID
  string). Prevents confusion when using the file to bootstrap a local Postgres
  database manually.

## [0.8.3] - 2026-05-27

Public-readiness cleanup — remove leftover homelab-specific defaults and fix the
documented PostgreSQL setup so a fresh public install works out of the box.

### Fixed
- **`DB_BACKEND=postgres` now works.** The `DatabaseBackend` enum gained
  `postgres`/`postgresql` aliases that route to the local PostgreSQL client (the
  same path as `DB_BACKEND=local`). Previously `postgres` fell through to the
  Supabase branch and crashed demanding `SUPABASE_URL`, contradicting the README.
- `search_transcripts` / `search_corpora` no longer crash with a `NoneType / str`
  `TypeError` when their data-source roots are unset; they return a clean,
  empty "not configured" result instead.

### Changed
- Search roots are now env-var driven with **portable defaults**. `KNOWLEDGE_DATA_DIR`
  defaults to `./knowledge-data` (matching the SQLite fallback); `LATVIAN_LEARNING_ROOT`,
  `LATVIAN_XTTS_ROOT`, and `INGEST_ROOT` default to unset instead of hardcoded
  `/srv/*` homelab paths.
- Generic PostgreSQL connection defaults: `DB_NAME=lore`, `DB_USER=lore_user`
  (was `mpm_system` / `latvian_user`).
- Sentry release string defaults to `lore-knowledge-mcp@<version>` instead of
  the hardcoded `latvian-lab@1.0.0`.
- stdio MCP server identity renamed from `knowledge-mcp` to `lore` to match the project.
- README version badge is now the dynamic PyPI badge; added a configuration
  reference table documenting `KNOWLEDGE_DATA_DIR` and the optional search roots.

### Removed
- Deleted committed working backups of `server.py` (`*.backup`, `*.backup-verbose`,
  `*.bak-*`, `*.json_backup`) and added matching patterns to `.gitignore`.

### Tests
- New `tests/test_db_backend_postgres.py` — `DB_BACKEND=postgres`/`postgresql`/`local`
  select `LocalPostgresClient`, never Supabase; generic `lore`/`lore_user` defaults.
- New `tests/test_search_config.py` — unset search roots return clean empty
  "not configured" results (no crash, no `/srv` path).

## [0.8.0] - 2026-05-26

### Added
- `min_score` parameter on `kb_search` — filter results below a relevance threshold (FTS bm25 scores are negative; positive values return nothing on FTS path, by design)
- `journal_search` tool — full-text and ILIKE search across journal entries with optional date range filtering
- `trust_score` field on KB entries — float 0.0–1.0 provenance/quality signal; filterable via `min_trust_score` on `kb_search` and `kb_list`
- Migration `009_trust_score.sql` for PostgreSQL; SQLite auto-migrated via PRAGMA-guarded ALTER TABLE

### Fixed
- SQL operator-precedence bug in `fts_search_postgres` — `AND topic = %s` was bypassed by the English FTS branch due to unparenthesized OR expression; topic filtering now works correctly for all FTS queries

### Tests
- 277 unit tests (up from 205 at v0.7.0)
- 81 e2e tests against staging (up from 64); all passing
- Regression corpus tests now topic-scoped for reliable isolation against large corpora

## [0.6.0] - 2026-05-24

> **Note:** This release was yanked from PyPI on 2026-05-24. Existing installs continue to work; new installs will skip this version. A successor release will follow once the staging and end-to-end testing pipeline is established. The feature set remains intact and accurate.

### Added
- **Semantic & Hybrid Search** (Issue #6) — Lore now finds entries by meaning, not just keywords
  - Local sentence-transformers embeddings via ONNX (no API key, no external calls)
  - FTS5 (BM25) lexical search replaces SQLite LIKE fallback
  - sqlite-vec cosine similarity for vector search
  - Reciprocal Rank Fusion (RRF) hybrid mode combining lexical + semantic
  - New optional `[semantic]` extra: `pip install lore-knowledge-mcp[semantic]`
  - Opt-in via `LORE_SEMANTIC_SEARCH=true` (default: off, zero impact on existing users)
  - Configurable via `LORE_EMBEDDING_MODEL`, `LORE_RRF_K`, `LORE_DEBUG_SEARCH`
  - Multilingual support via `paraphrase-multilingual-MiniLM-L12-v2` (same 384d)
- New MCP tool: `kb_backfill_embeddings` — generate embeddings for existing KB entries (idempotent)
- New MCP tool: `kb_embedding_status` — report embedding coverage and model info
- New module `src/lore/embeddings.py` — singleton model loader with content_hash for stale detection
- New module `src/lore/search.py` — RRF, candidate pool sizing, hybrid orchestration
- FTS5 virtual table + triggers for SQLite (replaces unranked LIKE search)

### Changed
- SQLite schema now applied via `executescript()` instead of split-on-semicolon (required for trigger blocks)
- `handle_kb_search` accepts new optional params: `semantic`, `hybrid`, `search_mode`, `top_k`
- Search response includes new fields: `search_mode`, `model`, `rrf_k`, `score` (when applicable)
- `kb_add` embeds at write time (best-effort — KB entry still succeeds if embed fails)
- `kb_update` re-embeds only when content changes (content_hash comparison)
- `kb_delete` correctly orders deletes: `knowledge_kb_entries` first, then vec0 row

### Fixed
- RRF tie-breaking is now deterministic (secondary sort by `kb_id`)
- FTS5 fast path no longer requires semantic flag to activate
- `[semantic]` extra now includes `optimum[onnxruntime]` for clean fresh installs

### Deferred to Phase 2 (Issue #6 follow-up)
- PostgreSQL semantic path (pgvector HNSW with `halfvec(384)`) — schema migration file present, Python integration pending
- `kb_reindex_embeddings` for full re-embedding on model change
- Cross-encoder reranker
- `include_content` param on `kb_search`

## [0.5.0] — 2026-05-23

### Changed
- Renamed Python package from `knowledge_mcp` to `lore`
- Renamed systemd service from `knowledge-mcp` to `lore`
- Renamed CLI entry point from `knowledge-mcp` to `lore-mcp`
- Updated project metadata URLs to `lore-mcp` identity
- `lore-mcp` now accepts `--host`/`--port` to run as HTTP/SSE server (stdio remains default)
- Default backend changed to `sqlite` so a clean install boots without any external services

### Fixed
- DB password removed from `main()` (was hardcoded for dev convenience)
- DB password removed from systemd unit (was duplicated from `.env`)
- Old `knowledge-mcp.service` masked to prevent accidental double-start
- `__version__` bumped from 0.1.0 to 0.5.0 to match `pyproject.toml`

## [0.4.0] — 2026-05-23

### Added
- Attribution model: `author`, `source_type`, `verified` fields on all KB entries
- `kb_ingest_doc`/`kb_ingest_dir` now accept `author` and `source_type` parameters
- Query sanitization for `kb_search` to prevent PostgREST filter injection
- Ruff linter + formatter (`make lint`, `make format`, `make fix`)

### Changed
- Rebranded from "Advanced Knowledge MCP" to **Lore**
- Renamed `research_*` tools to `investigation_*` (better reflects ops use case)
- Tool surface: 38 → 29 tools (streamlined)

### Removed
- Knowledge Graph tools (`kg_*`) — unused in practice
- Source tracking tools (`research_add_source`, etc.) — unused in practice
- `kb_link_to_source` tool

## [0.3.0] — 2026-04-07

### Added
- SQLite backend support (no database server required)
- MCP Index: scan and search across all configured MCP servers
- Multi-search: query KB, investigations, journal, and transcripts simultaneously

## [0.2.0] — 2025-12-08

### Added
- Research workflows (notes, experiments, source linking)
- Document ingestion with change detection (SHA-256 hashing)
- Supabase backend support

## [0.1.0] — 2025-12-01

### Added
- Initial release: Knowledge Base with semantic search
- Journal system for decision logging
- PostgreSQL backend
