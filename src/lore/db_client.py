"""
Database abstraction layer for MCP servers.

Supports both Supabase and local PostgreSQL backends.
Environment variable DB_BACKEND controls which is used (default: supabase).

Usage:
    from db_client import get_db_client, DatabaseBackend

    # Automatic backend selection based on DB_BACKEND env var
    db = get_db_client()

    # Force specific backend
    db = get_db_client(backend=DatabaseBackend.LOCAL)

    # Query interface (works with both backends)
    result = db.table("kb_entries").select("*").eq("topic", "test").execute()

    # Insert
    db.table("kb_entries").insert({"topic": "test", "title": "Test"}).execute()

    # Update
    db.table("kb_entries").update({"title": "Updated"}).eq("kb_id", "123").execute()

    # Delete
    db.table("kb_entries").delete().eq("kb_id", "123").execute()
"""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)


class DatabaseBackend(Enum):
    """Supported database backends.

    ``POSTGRES`` / ``POSTGRESQL`` are aliases that route to the same local
    PostgreSQL backend as ``LOCAL`` — the README documents
    ``DB_BACKEND=postgres`` and ``_backend_kind()`` already treats those
    spellings as the PostgreSQL path, so the enum must accept them too.
    """

    SUPABASE = "supabase"
    LOCAL = "local"
    POSTGRES = "postgres"
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"


@dataclass
class QueryResult:
    """Unified query result across backends."""

    data: list[dict[str, Any]]
    count: int | None = None
    error: str | None = None


# Issue #14: the ``trust_score`` column carries a per-entry confidence signal in
# [0.0, 1.0] (default 1.0). Added by a single idempotent DDL statement rather
# than by editing a frozen base-schema constant. Mirrors
# migrations/009_trust_score.sql byte-for-byte (a unit test enforces parity).
# Applied in LocalPostgresClient._init_schema for PostgreSQL; SQLite gets the
# equivalent column from _SQLITE_SCHEMA + an idempotent PRAGMA-guarded ALTER.
KB_ENTRIES_TRUST_SCORE_DDL = "ALTER TABLE knowledge.kb_entries ADD COLUMN IF NOT EXISTS trust_score REAL DEFAULT 1.0;"


# Issue #26: pg_trgm GIN index on knowledge.kb_entries.content. The lexical /
# substring search paths (journal ILIKE fallback and any TableQuery.ilike()
# filter that compiles to ``content ILIKE '%term%'``) cannot use the existing
# to_tsvector FTS indexes, so they sequentially scan a table bloated by long
# hermes-conversations transcripts. A trigram GIN index makes those
# ``ILIKE '%...%'`` substring queries index-accelerated without rewriting them.
#
# These two statements mirror migrations/010_kb_content_trgm_index.sql (a unit
# test enforces parity). They use the NON-CONCURRENT ``CREATE INDEX IF NOT
# EXISTS`` form because bootstrap runs in autocommit and a fresh DB has an empty
# table — the build is instant. The standalone migration ships a CONCURRENTLY
# variant for the EXISTING populated production DB so the build never holds a
# write lock on the live table. Both are idempotent.
KB_CONTENT_TRGM_EXTENSION_DDL = "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
KB_CONTENT_TRGM_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_kb_entries_content_trgm "
    "ON knowledge.kb_entries USING gin (content gin_trgm_ops);"
)

KB_FTS_ENGLISH_COMBINED_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_kb_entries_fts_english_combined"
    " ON knowledge.kb_entries"
    " USING gin("
    "     to_tsvector('english',"
    "         coalesce(title, '') || ' ' || coalesce(content, ''))"
    " );"
)


class LocalPostgresClient:
    """
    PostgreSQL client that mimics Supabase's query interface.

    Provides a compatible API so code can switch backends with minimal changes.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5433,
        database: str = "lore",
        user: str = "lore_user",
        password: str = "",
    ):
        """Initialize local PostgreSQL connection."""
        try:
            import psycopg2
            import psycopg2.extras

            self._psycopg2 = psycopg2
            self._extras = psycopg2.extras
        except ImportError:
            raise ImportError("psycopg2 is required for local PostgreSQL connections")

        self.host = host
        self.port = port
        self.database = database
        self.user = user
        self.password = password
        self._conn = None

        # Semantic search state (Phase 2 / Issue #6). Populated by _init_schema().
        # Mirrors the SqliteClient interface so lore.search can stay backend-agnostic.
        self.vec_extension_loaded: bool = False
        self.pgvector_version: str | None = None
        self.vector_type: str = (
            "vector"  # "halfvec" when pgvector >= 0.7, else "vector"
        )
        self._schema_initialized: bool = False

        logger.info(f"LocalPostgresClient initialized for {database}@{host}:{port}")

    def _get_connection(self):
        """Get or create database connection."""
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password,
            )
            # Force UTF-8 decoding even when server_encoding is SQL_ASCII.
            # Without this, psycopg2 uses ASCII and chokes on multi-byte chars (0xe2 etc).
            self._conn.set_client_encoding("UTF8")
            self._conn.autocommit = True
            logger.debug("Created new PostgreSQL connection")
            # Initialize semantic schema once per process (idempotent CREATE IF NOT EXISTS).
            if not self._schema_initialized:
                self._init_schema()
                self._schema_initialized = True
        return self._conn

    def _init_schema(self) -> None:
        """Create kb_embeddings table for PostgreSQL semantic search.

        Idempotent: safe to call on every startup. Detects pgvector version
        and chooses halfvec(384) (>=0.7) or vector(384) fallback.
        Silently no-ops when the vector extension is not installed —
        lore.search degrades to FTS-only behavior in that case.

        Sets self.vec_extension_loaded, self.pgvector_version, and
        self.vector_type so callers can introspect the live capabilities.
        """
        semantic_enabled = (
            os.getenv("LORE_SEMANTIC_SEARCH", "false").strip().lower() == "true"
        )
        conn = self._conn
        cursor = conn.cursor()
        # Safe default: if anything fails before the pgvector probe below
        # (e.g. the GIN-index DDL), skip pgvector setup rather than referencing
        # an unbound _skip_pgvector later (UnboundLocalError).
        _skip_pgvector = True
        try:
            # Issue #14: trust_score confidence column (idempotent ADD COLUMN IF
            # NOT EXISTS). Applied before the GIN index below so a fresh column
            # is in place for any later schema work. Existing rows pick up the
            # DEFAULT 1.0 automatically — no backfill needed.
            cursor.execute(KB_ENTRIES_TRUST_SCORE_DDL.rstrip(";"))
            logger.debug("knowledge.kb_entries.trust_score ensured")

            # Issue #10: GIN index using simple config + regexp_replace so that
            # dotted/slashed identifiers like asyncio.gather are split into
            # individual tokens. Idempotent (CREATE INDEX IF NOT EXISTS).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_kb_search_simple "
                "    ON knowledge.kb_entries "
                "    USING gin(to_tsvector('simple', regexp_replace("
                "        coalesce(title,'') || ' ' || coalesce(content,''),"
                "        '[.,/\\\\:_-]', ' ', 'g')))"
            )
            logger.debug("idx_kb_search_simple ensured")

            # Issue #26: pg_trgm extension + GIN trigram index on content so the
            # lexical ILIKE '%...%' substring paths stop sequentially scanning a
            # transcript-bloated table. NON-CONCURRENT form is safe here: fresh
            # DBs have an empty/small table so the build is instant, and a
            # pre-existing index short-circuits via IF NOT EXISTS. The live
            # production DB uses the CONCURRENTLY variant in
            # migrations/010_kb_content_trgm_index.sql (run via psql). Mirrors the
            # KB_CONTENT_TRGM_* constants byte-for-byte (a unit test enforces it).
            cursor.execute(KB_CONTENT_TRGM_EXTENSION_DDL.rstrip(";"))
            cursor.execute(KB_CONTENT_TRGM_INDEX_DDL.rstrip(";"))
            logger.debug("idx_kb_entries_content_trgm ensured")

            # FTS English-config combined index: the fts_search_postgres WHERE
            # clause matches on to_tsvector('english', coalesce(title,'') || ' '
            # || coalesce(content,'')), an expression the content-only FTS index
            # cannot serve — Postgres falls back to seqscan. This expression GIN
            # index matches the WHERE clause exactly so the FTS leg of hybrid
            # search uses a Bitmap Index Scan. NON-CONCURRENT form is safe here
            # (fresh DBs build instantly, IF NOT EXISTS short-circuits); the live
            # production DB uses the CONCURRENTLY variant in
            # migrations/011_kb_fts_english_combined_index.sql. Mirrors the
            # KB_FTS_ENGLISH_COMBINED_* constants (a unit test enforces it).
            cursor.execute(KB_FTS_ENGLISH_COMBINED_INDEX_DDL.rstrip(";"))
            logger.debug("idx_kb_entries_fts_english_combined ensured")

            # Detect pgvector extension and version.
            cursor.execute(
                "SELECT extversion FROM pg_extension WHERE extname = %s",
                ("vector",),
            )
            row = cursor.fetchone()
            if not row:
                self.vec_extension_loaded = False
                if semantic_enabled:
                    logger.warning(
                        "LORE_SEMANTIC_SEARCH=true but pgvector extension is not "
                        "installed in database %s. Semantic search will be "
                        "unavailable on PostgreSQL.",
                        self.database,
                    )
                _skip_pgvector = True
            else:
                _skip_pgvector = False

            if not _skip_pgvector:
                self.pgvector_version = row[0]

                # Determine vector type: halfvec requires pgvector >= 0.7.
                def _version_tuple(v: str) -> tuple:
                    parts = []
                    for chunk in v.split("."):
                        digits = "".join(ch for ch in chunk if ch.isdigit())
                        parts.append(int(digits) if digits else 0)
                    return tuple(parts)

                try:
                    supports_halfvec = _version_tuple(self.pgvector_version) >= (
                        0,
                        7,
                        0,
                    )
                except Exception:  # noqa: BLE001
                    supports_halfvec = False

                self.vector_type = "halfvec" if supports_halfvec else "vector"
                vt = self.vector_type
                ops = "halfvec_cosine_ops" if supports_halfvec else "vector_cosine_ops"

                # Check existence first so we don't issue CREATE on every startup.
                cursor.execute(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s)",
                    ("knowledge", "kb_embeddings"),
                )
                exists = cursor.fetchone()[0]

                if not exists:
                    logger.info(
                        "Creating knowledge.kb_embeddings (%s(384), pgvector %s)",
                        vt,
                        self.pgvector_version,
                    )
                    cursor.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS knowledge.kb_embeddings (
                            kb_id        TEXT PRIMARY KEY
                                         REFERENCES knowledge.kb_entries(kb_id) ON DELETE CASCADE,
                            embedding    {vt}(384) NOT NULL,
                            content_hash TEXT NOT NULL,
                            model_name   TEXT NOT NULL,
                            model_dims   INTEGER NOT NULL DEFAULT 384,
                            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            embedded_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        )
                        """
                    )
                    # Add created_at to pre-existing tables (idempotent).
                    cursor.execute(
                        """
                        ALTER TABLE knowledge.kb_embeddings
                            ADD COLUMN IF NOT EXISTS
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        """
                    )
                    cursor.execute(
                        f"""
                        CREATE INDEX IF NOT EXISTS idx_kb_embeddings_hnsw
                            ON knowledge.kb_embeddings
                            USING hnsw (embedding {ops})
                            WITH (m = 16, ef_construction = 64)
                        """
                    )
                    cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_kb_embeddings_model
                            ON knowledge.kb_embeddings (model_name)
                        """
                    )

                # Final verification: can we actually select from the table?
                cursor.execute("SELECT 1 FROM knowledge.kb_embeddings LIMIT 0")
                self.vec_extension_loaded = True
                logger.info(
                    "PostgreSQL semantic ready: pgvector=%s, vector_type=%s(384)",
                    self.pgvector_version,
                    self.vector_type,
                )
        except self._psycopg2.Error as exc:
            # Don't crash on schema init failure — degrade gracefully.
            self.vec_extension_loaded = False
            self._schema_initialized = False  # Allow retry on next reconnect.
            if semantic_enabled:
                logger.warning(
                    "Failed to initialize kb_embeddings schema: %s. "
                    "Semantic search on PostgreSQL will be unavailable.",
                    exc,
                )
            else:
                logger.debug("kb_embeddings schema init skipped: %s", exc)
        finally:
            cursor.close()

        # Retrieval telemetry schema (Issue #5, Phase 1). Isolated in its own
        # try/except so a telemetry failure can NEVER touch vec_extension_loaded
        # or _schema_initialized — those are owned exclusively by the pgvector
        # block above. Local import avoids a circular import at module load.
        from lore import telemetry as telemetry_module

        if telemetry_module.mining_enabled():
            try:
                telemetry_module.ensure_telemetry_schema(self._conn)
                logger.info("Retrieval telemetry schema ready")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "Failed to initialize telemetry schema (non-fatal): %s", e
                )

            # Hard negative pairs schema (Phase 3) — requires mining enabled +
            # PostgreSQL. ensure_hard_negative_schema already swallows + logs any
            # failure internally, so no outer try/except is needed here.
            telemetry_module.ensure_hard_negative_schema(self._conn)
            logger.info("Hard negative pairs schema ready")

    def table(self, name: str) -> "TableQuery":
        """Start a query on a table (Supabase-compatible interface)."""
        return TableQuery(self, name)

    def rpc(self, function_name: str, params: dict[str, Any] = None) -> QueryResult:
        """Call a stored function (RPC)."""
        conn = self._get_connection()
        cursor = conn.cursor(cursor_factory=self._extras.RealDictCursor)

        if params:
            # Build named parameter syntax: function_name(param1 => %s, param2 => %s)
            param_list = ", ".join([f"{k} => %s" for k in params.keys()])
            sql = f"SELECT * FROM {function_name}({param_list})"
            cursor.execute(sql, list(params.values()))
        else:
            sql = f"SELECT * FROM {function_name}()"
            cursor.execute(sql)

        try:
            data = [dict(row) for row in cursor.fetchall()]
        except self._psycopg2.ProgrammingError:
            data = []  # No results (e.g., for functions that return void)

        cursor.close()
        return QueryResult(data=data)

    def close(self):
        """Close database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None
            logger.debug("Closed PostgreSQL connection")


class TableQuery:
    """
    Query builder that mimics Supabase's fluent interface.

    Supports: select, insert, update, delete, upsert
    Filters: eq, neq, gt, gte, lt, lte, like, ilike, in_, is_, order, limit, offset
    """

    def __init__(self, client: LocalPostgresClient, table: str):
        self.client = client
        self.table = table
        self._operation = "select"
        self._columns = "*"
        self._data = None
        self._filters: list[tuple] = []
        self._order_by: list[tuple] = []
        self._limit_val: int | None = None
        self._offset_val: int | None = None
        self._on_conflict: str | None = None
        self._count_mode: str | None = None
        self._single_result: bool = False  # For maybe_single() support
        self._or_filters: list[str] = []  # For OR conditions

    def select(self, columns: str = "*", count: str = None) -> "TableQuery":
        """Select columns from table."""
        self._operation = "select"
        self._columns = columns
        self._count_mode = count  # "exact", "planned", or "estimated"
        return self

    def insert(
        self, data: Union[dict, list[dict]], upsert: bool = False
    ) -> "TableQuery":
        """Insert data into table."""
        self._operation = "upsert" if upsert else "insert"
        self._data = data if isinstance(data, list) else [data]
        return self

    def upsert(
        self, data: Union[dict, list[dict]], on_conflict: str = None
    ) -> "TableQuery":
        """Upsert (insert or update on conflict)."""
        self._operation = "upsert"
        self._data = data if isinstance(data, list) else [data]
        self._on_conflict = on_conflict
        return self

    def update(self, data: dict) -> "TableQuery":
        """Update rows in table."""
        self._operation = "update"
        self._data = data
        return self

    def delete(self) -> "TableQuery":
        """Delete rows from table."""
        self._operation = "delete"
        return self

    # Filter methods
    def eq(self, column: str, value: Any) -> "TableQuery":
        """Equal to."""
        self._filters.append((column, "=", value))
        return self

    def neq(self, column: str, value: Any) -> "TableQuery":
        """Not equal to."""
        self._filters.append((column, "!=", value))
        return self

    def gt(self, column: str, value: Any) -> "TableQuery":
        """Greater than."""
        self._filters.append((column, ">", value))
        return self

    def gte(self, column: str, value: Any) -> "TableQuery":
        """Greater than or equal."""
        self._filters.append((column, ">=", value))
        return self

    def lt(self, column: str, value: Any) -> "TableQuery":
        """Less than."""
        self._filters.append((column, "<", value))
        return self

    def lte(self, column: str, value: Any) -> "TableQuery":
        """Less than or equal."""
        self._filters.append((column, "<=", value))
        return self

    def like(self, column: str, pattern: str) -> "TableQuery":
        """LIKE pattern match."""
        self._filters.append((column, "LIKE", pattern))
        return self

    def ilike(self, column: str, pattern: str) -> "TableQuery":
        """Case-insensitive LIKE."""
        self._filters.append((column, "ILIKE", pattern))
        return self

    def in_(self, column: str, values: list[Any]) -> "TableQuery":
        """IN list of values."""
        self._filters.append((column, "IN", tuple(values)))
        return self

    def is_(self, column: str, value: Any) -> "TableQuery":
        """IS (for NULL checks)."""
        self._filters.append((column, "IS", value))
        return self

    def order(self, column: str, desc: bool = False) -> "TableQuery":
        """Order by column."""
        self._order_by.append((column, "DESC" if desc else "ASC"))
        return self

    def limit(self, count: int) -> "TableQuery":
        """Limit number of results."""
        self._limit_val = count
        return self

    def offset(self, count: int) -> "TableQuery":
        """Offset results."""
        self._offset_val = count
        return self

    def or_(self, conditions: str) -> "TableQuery":
        """
        Add OR conditions (Supabase-compatible).

        Example: .or_("title.wfts.search_term,content.wfts.search_term")

        For local PostgreSQL, we parse this into proper full-text search.
        """
        # Store raw OR condition for processing in execute
        self._or_filters.append(conditions)
        return self

    def maybe_single(self) -> "TableQuery":
        """
        Return single result or None (Supabase-compatible).

        Sets limit to 1 and marks query to return single object instead of array.
        """
        self._limit_val = 1
        self._single_result = True
        return self

    def execute(self) -> QueryResult:
        """Execute the query and return results."""
        conn = self.client._get_connection()
        cursor = conn.cursor(cursor_factory=self.client._extras.RealDictCursor)

        try:
            if self._operation == "select":
                return self._execute_select(cursor)
            elif self._operation == "insert":
                return self._execute_insert(cursor)
            elif self._operation == "upsert":
                return self._execute_upsert(cursor)
            elif self._operation == "update":
                return self._execute_update(cursor)
            elif self._operation == "delete":
                return self._execute_delete(cursor)
            else:
                raise ValueError(f"Unknown operation: {self._operation}")
        finally:
            cursor.close()

    def _build_where_clause(self) -> tuple:
        """Build WHERE clause from filters."""
        if not self._filters and not self._or_filters:
            return "", []

        conditions = []
        values = []

        # Handle regular AND filters
        for column, op, value in self._filters:
            if op == "IS":
                if value is None:
                    conditions.append(f"{column} IS NULL")
                else:
                    conditions.append(f"{column} IS NOT NULL")
            elif op == "IN":
                placeholders = ", ".join(["%s"] * len(value))
                conditions.append(f"{column} IN ({placeholders})")
                values.extend(value)
            else:
                conditions.append(f"{column} {op} %s")
                values.append(value)

        # Handle OR filters (full-text search)
        for or_condition in self._or_filters:
            # Parse Supabase-style: "title.wfts.query,content.wfts.query"
            or_parts = or_condition.split(",")
            or_conditions = []

            for part in or_parts:
                if ".wfts." in part or ".plfts." in part:
                    # Full-text search: "title.wfts.search_term"
                    column, operator, search_term = part.split(".", 2)

                    # Use PostgreSQL's full-text search
                    if operator == "wfts":
                        # Web search syntax (phrase-aware)
                        or_conditions.append(
                            f"to_tsvector('english', {column}) @@ websearch_to_tsquery('english', %s)"
                        )
                    else:  # plfts
                        # Plain text search
                        or_conditions.append(
                            f"to_tsvector('english', {column}) @@ plainto_tsquery('english', %s)"
                        )
                    values.append(search_term)

            if or_conditions:
                # Wrap OR conditions in parentheses
                conditions.append(f"({' OR '.join(or_conditions)})")

        if not conditions:
            return "", []

        return " WHERE " + " AND ".join(conditions), values

    def _build_order_clause(self) -> str:
        """Build ORDER BY clause."""
        if not self._order_by:
            return ""

        parts = [f"{col} {direction}" for col, direction in self._order_by]
        return " ORDER BY " + ", ".join(parts)

    def _execute_select(self, cursor) -> QueryResult:
        """Execute SELECT query."""
        where_clause, where_values = self._build_where_clause()
        order_clause = self._build_order_clause()

        sql = f"SELECT {self._columns} FROM {self.table}{where_clause}{order_clause}"

        if self._limit_val:
            sql += f" LIMIT {self._limit_val}"
        if self._offset_val:
            sql += f" OFFSET {self._offset_val}"

        cursor.execute(sql, where_values)
        data = [dict(row) for row in cursor.fetchall()]

        # Handle single result mode (maybe_single)
        if self._single_result:
            # Return single object or None (not an array)
            data = data[0] if data else None

        # Get count if requested
        count = None
        if self._count_mode == "exact":
            count_sql = f"SELECT COUNT(*) FROM {self.table}{where_clause}"
            cursor.execute(count_sql, where_values)
            count = cursor.fetchone()["count"]

        return QueryResult(data=data, count=count)

    def _serialize_value(self, value):
        """Serialize Python objects to PostgreSQL-compatible types."""
        import json

        if isinstance(value, dict):
            # Dicts are serialized to JSONB
            return json.dumps(value)
        elif isinstance(value, list):
            # Pass lists directly — psycopg2 maps Python list → text[] natively.
            # JSON-encoding would break text[] columns (malformed array literal error).
            return value
        return value

    def _execute_insert(self, cursor) -> QueryResult:
        """Execute INSERT query."""
        if not self._data:
            return QueryResult(data=[], error="No data to insert")

        columns = list(self._data[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))

        sql = f"INSERT INTO {self.table} ({col_names}) VALUES ({placeholders}) RETURNING *"

        results = []
        for row in self._data:
            values = [self._serialize_value(row.get(col)) for col in columns]
            cursor.execute(sql, values)
            results.extend([dict(r) for r in cursor.fetchall()])

        return QueryResult(data=results)

    def _execute_upsert(self, cursor) -> QueryResult:
        """Execute UPSERT (INSERT ... ON CONFLICT DO UPDATE)."""
        if not self._data:
            return QueryResult(data=[], error="No data to upsert")

        columns = list(self._data[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(["%s"] * len(columns))

        # Determine conflict target
        conflict_col = self._on_conflict or columns[0]  # Default to first column

        # Build SET clause for update
        update_sets = ", ".join(
            [f"{col} = EXCLUDED.{col}" for col in columns if col != conflict_col]
        )

        sql = f"""
            INSERT INTO {self.table} ({col_names})
            VALUES ({placeholders})
            ON CONFLICT ({conflict_col}) DO UPDATE SET {update_sets}
            RETURNING *
        """

        results = []
        for row in self._data:
            values = [self._serialize_value(row.get(col)) for col in columns]
            cursor.execute(sql, values)
            results.extend([dict(r) for r in cursor.fetchall()])

        return QueryResult(data=results)

    def _execute_update(self, cursor) -> QueryResult:
        """Execute UPDATE query."""
        if not self._data:
            return QueryResult(data=[], error="No data to update")

        set_parts = []
        set_values = []
        for col, val in self._data.items():
            set_parts.append(f"{col} = %s")
            set_values.append(self._serialize_value(val))

        where_clause, where_values = self._build_where_clause()

        sql = (
            f"UPDATE {self.table} SET {', '.join(set_parts)}{where_clause} RETURNING *"
        )

        cursor.execute(sql, set_values + where_values)
        data = [dict(row) for row in cursor.fetchall()]

        return QueryResult(data=data)

    def _execute_delete(self, cursor) -> QueryResult:
        """Execute DELETE query."""
        where_clause, where_values = self._build_where_clause()

        sql = f"DELETE FROM {self.table}{where_clause} RETURNING *"

        cursor.execute(sql, where_values)
        data = [dict(row) for row in cursor.fetchall()]

        return QueryResult(data=data)


class SupabaseWrapper:
    """
    Wrapper around Supabase client to return QueryResult objects.

    Makes Supabase responses compatible with LocalPostgresClient.
    """

    def __init__(self, url: str, key: str):
        """Initialize Supabase client."""
        try:
            from supabase import create_client

            self._client = create_client(url, key)
        except ImportError:
            raise ImportError("supabase-py is required for Supabase connections")

        logger.info(f"SupabaseWrapper initialized for {url}")

    def table(self, name: str):
        """Return Supabase table query builder."""
        return SupabaseTableWrapper(self._client.table(name))

    def rpc(self, function_name: str, params: dict[str, Any] = None) -> QueryResult:
        """Call a stored function (RPC)."""
        response = self._client.rpc(function_name, params or {}).execute()
        return QueryResult(data=response.data if response.data else [])

    def close(self):
        """Close client (no-op for Supabase)."""
        pass


class SupabaseTableWrapper:
    """Wraps Supabase table queries to return QueryResult objects."""

    def __init__(self, table_query):
        self._query = table_query

    def __getattr__(self, name):
        """Proxy all method calls to underlying query."""
        attr = getattr(self._query, name)
        if callable(attr):

            def wrapper(*args, **kwargs):
                result = attr(*args, **kwargs)
                # If result is another query builder, wrap it
                if hasattr(result, "execute"):
                    return SupabaseTableWrapper(result)
                return result

            return wrapper
        return attr

    def execute(self) -> QueryResult:
        """Execute and return QueryResult."""
        response = self._query.execute()
        return QueryResult(
            data=response.data if response.data else [],
            count=getattr(response, "count", None),
        )


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_kb_entries (
    kb_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    source_doc TEXT,
    source_section TEXT,
    line_range TEXT,
    tags TEXT DEFAULT '[]',
    author TEXT,
    source_type TEXT,
    verified INTEGER,
    trust_score REAL DEFAULT 1.0
);
CREATE TABLE IF NOT EXISTS knowledge_research_notes (
    note_id TEXT PRIMARY KEY,
    topic TEXT,
    title TEXT,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS knowledge_research_sources (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT,
    url TEXT,
    authors TEXT DEFAULT '[]',
    year INTEGER,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS knowledge_research_experiments (
    experiment_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    hypothesis TEXT,
    methodology TEXT,
    results TEXT DEFAULT '{}',
    conclusion TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS knowledge_research_source_links (
    link_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS knowledge_journal_entries (
    entry_id TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS knowledge_kb_doc_sync (
    doc_path TEXT PRIMARY KEY,
    doc_hash TEXT NOT NULL,
    kb_ids TEXT DEFAULT '[]',
    last_synced_at TEXT,
    last_modified_at TEXT,
    strategy TEXT,
    metadata TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS knowledge_kg_nodes (
    node_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT,
    properties TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS knowledge_kg_edges (
    edge_id TEXT PRIMARY KEY,
    from_node TEXT NOT NULL,
    to_node TEXT NOT NULL,
    relation TEXT NOT NULL,
    properties TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS mcp_index_versions (
    server_name TEXT PRIMARY KEY,
    version TEXT,
    last_scanned TEXT,
    tool_count INTEGER DEFAULT 0
);

-- Semantic search support (Issue #6).
-- All tables here are optional; absence is tolerated when LORE_SEMANTIC_SEARCH=false.

-- FTS5 contentless virtual table mirroring (title, content) of knowledge_kb_entries.
-- Triggers below keep it in sync. Falls back to LIKE if FTS5 is unavailable.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_kb_entries_fts USING fts5(
    title,
    content,
    content='knowledge_kb_entries',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

-- Keep FTS index in sync with knowledge_kb_entries.
CREATE TRIGGER IF NOT EXISTS knowledge_kb_entries_ai
AFTER INSERT ON knowledge_kb_entries
BEGIN
    INSERT INTO knowledge_kb_entries_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_kb_entries_ad
AFTER DELETE ON knowledge_kb_entries
BEGIN
    INSERT INTO knowledge_kb_entries_fts(knowledge_kb_entries_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_kb_entries_au
AFTER UPDATE ON knowledge_kb_entries
BEGIN
    INSERT INTO knowledge_kb_entries_fts(knowledge_kb_entries_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO knowledge_kb_entries_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

-- sqlite-vec vec0 virtual table for 384-d embeddings.
-- kb_id is FK-like into knowledge_kb_entries (no actual FK on vec0 tables).
-- We delete vec0 rows AFTER the KB row is gone — see kb_delete handler.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_kb_vec_embeddings USING vec0(
    kb_id TEXT PRIMARY KEY,
    embedding FLOAT[384]
);

-- Metadata about each embedding: which model produced it, content hash for staleness.
CREATE TABLE IF NOT EXISTS knowledge_kb_embedding_meta (
    kb_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (kb_id) REFERENCES knowledge_kb_entries(kb_id) ON DELETE CASCADE
);
"""


# Core CREATE TABLE statements without FTS5 / vec0 / triggers.
# Used as a degradation fallback when SQLite is missing FTS5 or sqlite-vec
# and LORE_SEMANTIC_SEARCH is not enabled. Keep these statements byte-for-byte
# identical to the corresponding blocks in _SQLITE_SCHEMA.
_CORE_SQLITE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS knowledge_kb_entries (
        kb_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        source_doc TEXT,
        source_section TEXT,
        line_range TEXT,
        tags TEXT DEFAULT '[]',
        author TEXT,
        source_type TEXT,
        verified INTEGER,
        trust_score REAL DEFAULT 1.0
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_research_notes (
        note_id TEXT PRIMARY KEY,
        topic TEXT,
        title TEXT,
        content TEXT NOT NULL,
        tags TEXT DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_research_sources (
        source_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        kind TEXT,
        url TEXT,
        authors TEXT DEFAULT '[]',
        year INTEGER,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_research_experiments (
        experiment_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        hypothesis TEXT,
        methodology TEXT,
        results TEXT DEFAULT '{}',
        conclusion TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_research_source_links (
        link_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        experiment_id TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_journal_entries (
        entry_id TEXT PRIMARY KEY,
        date TEXT NOT NULL,
        entry_type TEXT NOT NULL,
        content TEXT NOT NULL,
        tags TEXT DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_kb_doc_sync (
        doc_path TEXT PRIMARY KEY,
        doc_hash TEXT NOT NULL,
        kb_ids TEXT DEFAULT '[]',
        last_synced_at TEXT,
        last_modified_at TEXT,
        strategy TEXT,
        metadata TEXT DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_kg_nodes (
        node_id TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        kind TEXT,
        properties TEXT DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_kg_edges (
        edge_id TEXT PRIMARY KEY,
        from_node TEXT NOT NULL,
        to_node TEXT NOT NULL,
        relation TEXT NOT NULL,
        properties TEXT DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS mcp_index_versions (
        server_name TEXT PRIMARY KEY,
        version TEXT,
        last_scanned TEXT,
        tool_count INTEGER DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge_kb_embedding_meta (
        kb_id TEXT PRIMARY KEY,
        model_name TEXT NOT NULL,
        embedding_dim INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        FOREIGN KEY (kb_id) REFERENCES knowledge_kb_entries(kb_id) ON DELETE CASCADE
    )""",
)


def _sqlite_map_table(name: str) -> str:
    """Map Supabase-style 'schema.table' to SQLite flat name 'schema_table'."""
    return name.replace(".", "_")


def _sqlite_deserialize_row(row: dict) -> dict:
    """Parse JSON strings back into Python lists/dicts for known JSON columns."""
    import json

    result = {}
    for key, value in row.items():
        if isinstance(value, str) and len(value) > 0 and value[0] in ("{", "["):
            try:
                result[key] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                result[key] = value
        else:
            result[key] = value
    return result


def _sqlite_serialize_value(value: Any) -> Any:
    """Serialize Python lists/dicts to JSON strings for SQLite storage."""
    import json

    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


class SqliteTableQuery:
    """
    Query builder for SQLite that mirrors the TableQuery / Supabase interface.

    Supports: select, insert, update, delete, upsert
    Filters: eq, neq, gt, gte, lt, lte, like, ilike, in_, is_, or_
    Modifiers: order, limit, offset, maybe_single
    """

    def __init__(self, client: "SqliteClient", table: str):
        self._client = client
        # Map schema-qualified names to flat SQLite table names
        self._table = _sqlite_map_table(table)
        self._operation = "select"
        self._columns = "*"
        self._data: Any = None
        self._filters: list[tuple] = []
        self._or_filters: list[str] = []
        self._order_by: list[tuple] = []
        self._limit_val: int | None = None
        self._offset_val: int | None = None
        self._on_conflict: str | None = None
        self._count_mode: str | None = None
        self._single_result: bool = False

    # ------------------------------------------------------------------ #
    # Operation setters                                                    #
    # ------------------------------------------------------------------ #

    def select(self, columns: str = "*", count: str = None) -> "SqliteTableQuery":
        """Select columns from table."""
        self._operation = "select"
        self._columns = columns
        self._count_mode = count
        return self

    def insert(
        self, data: Union[dict, list[dict]], upsert: bool = False
    ) -> "SqliteTableQuery":
        """Insert data into table."""
        self._operation = "upsert" if upsert else "insert"
        self._data = data if isinstance(data, list) else [data]
        return self

    def upsert(
        self, data: Union[dict, list[dict]], on_conflict: str = None
    ) -> "SqliteTableQuery":
        """Upsert (INSERT OR REPLACE) data into table."""
        self._operation = "upsert"
        self._data = data if isinstance(data, list) else [data]
        self._on_conflict = on_conflict
        return self

    def update(self, data: dict) -> "SqliteTableQuery":
        """Update rows in table."""
        self._operation = "update"
        self._data = data
        return self

    def delete(self) -> "SqliteTableQuery":
        """Delete rows from table."""
        self._operation = "delete"
        return self

    # ------------------------------------------------------------------ #
    # Filter methods                                                       #
    # ------------------------------------------------------------------ #

    def eq(self, column: str, value: Any) -> "SqliteTableQuery":
        self._filters.append((column, "=", value))
        return self

    def neq(self, column: str, value: Any) -> "SqliteTableQuery":
        self._filters.append((column, "!=", value))
        return self

    def gt(self, column: str, value: Any) -> "SqliteTableQuery":
        self._filters.append((column, ">", value))
        return self

    def gte(self, column: str, value: Any) -> "SqliteTableQuery":
        self._filters.append((column, ">=", value))
        return self

    def lt(self, column: str, value: Any) -> "SqliteTableQuery":
        self._filters.append((column, "<", value))
        return self

    def lte(self, column: str, value: Any) -> "SqliteTableQuery":
        self._filters.append((column, "<=", value))
        return self

    def like(self, column: str, pattern: str) -> "SqliteTableQuery":
        """LIKE pattern match (case-sensitive in SQLite by default)."""
        self._filters.append((column, "LIKE", pattern))
        return self

    def ilike(self, column: str, pattern: str) -> "SqliteTableQuery":
        """Case-insensitive LIKE via LOWER()."""
        # Store as a special tuple so _build_where_clause can emit LOWER(col) LIKE LOWER(?)
        self._filters.append((column, "ILIKE", pattern))
        return self

    def in_(self, column: str, values: list[Any]) -> "SqliteTableQuery":
        self._filters.append((column, "IN", tuple(values)))
        return self

    def is_(self, column: str, value: Any) -> "SqliteTableQuery":
        """IS (for NULL checks)."""
        self._filters.append((column, "IS", value))
        return self

    def or_(self, conditions: str) -> "SqliteTableQuery":
        """
        OR conditions using Supabase-style syntax.

        Example: .or_("title.wfts.query,content.wfts.query")

        SQLite fallback: LIKE '%query%' on each column, joined with OR.
        """
        self._or_filters.append(conditions)
        return self

    # ------------------------------------------------------------------ #
    # Modifier methods                                                     #
    # ------------------------------------------------------------------ #

    def order(self, column: str, desc: bool = False) -> "SqliteTableQuery":
        self._order_by.append((column, "DESC" if desc else "ASC"))
        return self

    def limit(self, count: int) -> "SqliteTableQuery":
        self._limit_val = count
        return self

    def offset(self, count: int) -> "SqliteTableQuery":
        self._offset_val = count
        return self

    def maybe_single(self) -> "SqliteTableQuery":
        """Return a single object or None instead of a list."""
        self._limit_val = 1
        self._single_result = True
        return self

    # ------------------------------------------------------------------ #
    # Execution                                                            #
    # ------------------------------------------------------------------ #

    def execute(self) -> QueryResult:
        """Execute the query and return a QueryResult."""
        conn = self._client._get_connection()
        try:
            if self._operation == "select":
                return self._execute_select(conn)
            elif self._operation == "insert":
                return self._execute_insert(conn)
            elif self._operation == "upsert":
                return self._execute_upsert(conn)
            elif self._operation == "update":
                return self._execute_update(conn)
            elif self._operation == "delete":
                return self._execute_delete(conn)
            else:
                raise ValueError(f"Unknown operation: {self._operation}")
        except Exception as exc:
            logger.error("SQLite query error on %s: %s", self._table, exc)
            raise

    # ------------------------------------------------------------------ #
    # SQL building helpers                                                 #
    # ------------------------------------------------------------------ #

    def _build_where_clause(self) -> tuple:
        """Return (where_sql, params_list) for current filters."""
        conditions: list[str] = []
        values: list[Any] = []

        for column, op, value in self._filters:
            if op == "IS":
                if value is None:
                    conditions.append(f"{column} IS NULL")
                else:
                    conditions.append(f"{column} IS NOT NULL")
            elif op == "IN":
                placeholders = ", ".join(["?"] * len(value))
                conditions.append(f"{column} IN ({placeholders})")
                values.extend(value)
            elif op == "ILIKE":
                # SQLite has no native ILIKE; use LOWER() on both sides
                conditions.append(f"LOWER({column}) LIKE LOWER(?)")
                values.append(value)
            else:
                conditions.append(f"{column} {op} ?")
                values.append(value)

        # OR conditions — Supabase wfts/plfts → LIKE fallback
        for or_condition in self._or_filters:
            parts = or_condition.split(",")
            or_parts: list[str] = []
            for part in parts:
                part = part.strip()
                if ".wfts." in part or ".plfts." in part:
                    # "column.wfts.search_term"
                    segments = part.split(".", 2)
                    if len(segments) == 3:
                        col, _op, search_term = segments
                        or_parts.append(f"{col} LIKE ?")
                        values.append(f"%{search_term}%")
                elif "." in part:
                    # Generic "column.op.value" — best-effort LIKE
                    segments = part.split(".", 2)
                    if len(segments) == 3:
                        col, _op, search_term = segments
                        or_parts.append(f"{col} LIKE ?")
                        values.append(f"%{search_term}%")
            if or_parts:
                conditions.append(f"({' OR '.join(or_parts)})")

        if not conditions:
            return "", []
        return " WHERE " + " AND ".join(conditions), values

    def _build_order_clause(self) -> str:
        if not self._order_by:
            return ""
        parts = [f"{col} {direction}" for col, direction in self._order_by]
        return " ORDER BY " + ", ".join(parts)

    def _fetch_rows(self, conn, sql: str, params: list) -> list[dict[str, Any]]:
        """Execute a SELECT and return list of dicts with JSON columns parsed."""
        cursor = conn.execute(sql, params)
        col_names = [description[0] for description in cursor.description]
        rows = []
        for raw_row in cursor.fetchall():
            row = dict(zip(col_names, raw_row))
            rows.append(_sqlite_deserialize_row(row))
        return rows

    # ------------------------------------------------------------------ #
    # Operation implementations                                            #
    # ------------------------------------------------------------------ #

    def _execute_select(self, conn) -> QueryResult:
        where_clause, where_values = self._build_where_clause()
        order_clause = self._build_order_clause()

        sql = f"SELECT {self._columns} FROM {self._table}{where_clause}{order_clause}"
        if self._limit_val is not None:
            sql += f" LIMIT {self._limit_val}"
        if self._offset_val is not None:
            sql += f" OFFSET {self._offset_val}"

        data = self._fetch_rows(conn, sql, where_values)

        count: int | None = None
        if self._count_mode == "exact":
            count_sql = f"SELECT COUNT(*) FROM {self._table}{where_clause}"
            (count,) = conn.execute(count_sql, where_values).fetchone()

        if self._single_result:
            single = data[0] if data else None
            return QueryResult(data=single, count=count)

        return QueryResult(data=data, count=count)

    def _execute_insert(self, conn) -> QueryResult:
        if not self._data:
            return QueryResult(data=[], error="No data to insert")

        columns = list(self._data[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT INTO {self._table} ({col_names}) VALUES ({placeholders})"

        results: list[dict[str, Any]] = []
        for row in self._data:
            values = [_sqlite_serialize_value(row.get(col)) for col in columns]
            conn.execute(sql, values)
            # Re-fetch the inserted row by rowid
            inserted = self._fetch_rows(
                conn,
                f"SELECT * FROM {self._table} WHERE rowid = last_insert_rowid()",
                [],
            )
            results.extend(inserted)

        conn.commit()
        return QueryResult(data=results)

    def _execute_upsert(self, conn) -> QueryResult:
        """INSERT OR REPLACE — replaces row on primary-key conflict."""
        if not self._data:
            return QueryResult(data=[], error="No data to upsert")

        columns = list(self._data[0].keys())
        col_names = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))
        sql = f"INSERT OR REPLACE INTO {self._table} ({col_names}) VALUES ({placeholders})"

        results: list[dict[str, Any]] = []
        for row in self._data:
            values = [_sqlite_serialize_value(row.get(col)) for col in columns]
            conn.execute(sql, values)
            inserted = self._fetch_rows(
                conn,
                f"SELECT * FROM {self._table} WHERE rowid = last_insert_rowid()",
                [],
            )
            results.extend(inserted)

        conn.commit()
        return QueryResult(data=results)

    def _execute_update(self, conn) -> QueryResult:
        if not self._data:
            return QueryResult(data=[], error="No data to update")

        # Always refresh updated_at for tables that have it
        data_with_ts = dict(self._data)
        data_with_ts["updated_at"] = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"

        set_parts: list[str] = []
        set_values: list[Any] = []
        for col, val in data_with_ts.items():
            if col == "updated_at":
                # Embed the SQLite datetime expression directly (not a param)
                set_parts.append(f"{col} = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
            else:
                set_parts.append(f"{col} = ?")
                set_values.append(_sqlite_serialize_value(val))

        where_clause, where_values = self._build_where_clause()

        # Fetch matching rows before update so we can return them
        pre_sql = f"SELECT rowid, * FROM {self._table}{where_clause}"
        pre_cursor = conn.execute(pre_sql, where_values)
        pre_col_names = [d[0] for d in pre_cursor.description]
        pre_rows = [dict(zip(pre_col_names, r)) for r in pre_cursor.fetchall()]
        rowids = [r["rowid"] for r in pre_rows]

        if rowids:
            update_sql = (
                f"UPDATE {self._table} SET {', '.join(set_parts)}{where_clause}"
            )
            conn.execute(update_sql, set_values + where_values)
            conn.commit()

            # Re-fetch updated rows
            placeholders = ", ".join(["?"] * len(rowids))
            data = self._fetch_rows(
                conn,
                f"SELECT * FROM {self._table} WHERE rowid IN ({placeholders})",
                rowids,
            )
        else:
            data = []

        return QueryResult(data=data)

    def _execute_delete(self, conn) -> QueryResult:
        where_clause, where_values = self._build_where_clause()

        # Fetch rows before deletion so we can return them
        pre_data = self._fetch_rows(
            conn,
            f"SELECT * FROM {self._table}{where_clause}",
            where_values,
        )

        conn.execute(f"DELETE FROM {self._table}{where_clause}", where_values)
        conn.commit()

        return QueryResult(data=pre_data)


class SqliteClient:
    """
    SQLite client that mirrors the LocalPostgresClient / Supabase interface.

    Uses Python's built-in sqlite3 module — no additional dependencies.
    The database file is created at db_path (default: ./knowledge-data/knowledge.db).
    All schema tables are auto-created on first connection.

    Table name mapping: 'knowledge.kb_entries' -> 'knowledge_kb_entries'
    JSON columns (tags, authors, properties, …) are stored as JSON strings and
    automatically parsed back into Python lists/dicts on read.
    """

    def __init__(self, db_path: str = "./knowledge-data/knowledge.db"):
        import sqlite3

        self._sqlite3 = sqlite3
        self._db_path = db_path
        self._conn: Any | None = None
        # Tracks whether sqlite-vec loaded; consumers (search module) check this
        # to decide whether the semantic path is available on this connection.
        self.vec_extension_loaded: bool = False
        self.fts5_available: bool = False
        # Ensure parent directory exists
        from pathlib import Path

        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # Open connection and initialise schema immediately
        self._get_connection()
        logger.info(
            "SqliteClient initialised at %s (vec=%s, fts5=%s)",
            db_path,
            self.vec_extension_loaded,
            self.fts5_available,
        )

    def _get_connection(self):
        """Return (and lazily create) the SQLite connection."""
        if self._conn is None:
            self._conn = self._sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                detect_types=self._sqlite3.PARSE_DECLTYPES,
            )
            self._conn.row_factory = self._sqlite3.Row
            # Try to load sqlite-vec extension *before* schema init so that the
            # vec0 virtual table in _SQLITE_SCHEMA can be created.
            self._try_load_vec_extension()
            self._init_schema()
            logger.debug("Opened SQLite connection at %s", self._db_path)
        return self._conn

    def _try_load_vec_extension(self) -> None:
        """Best-effort sqlite-vec load. Silent failure when semantic is off."""
        semantic_enabled = (
            os.getenv("LORE_SEMANTIC_SEARCH", "false").strip().lower() == "true"
        )
        try:
            self._conn.enable_load_extension(True)
        except (AttributeError, self._sqlite3.OperationalError) as exc:
            # SQLite compiled without load-extension support — rare on Linux.
            if semantic_enabled:
                logger.error("SQLite extension loading is disabled: %s", exc)
            else:
                logger.debug("SQLite extension loading unavailable: %s", exc)
            return

        try:
            import sqlite_vec
        except ImportError as exc:
            if semantic_enabled:
                logger.error(
                    "sqlite-vec not installed; install '.[semantic]' extra: %s", exc
                )
            else:
                logger.debug("sqlite-vec not installed (semantic disabled): %s", exc)
            # Re-disable load extension to keep the surface small.
            try:
                self._conn.enable_load_extension(False)
            except Exception:  # noqa: BLE001
                pass
            return

        try:
            sqlite_vec.load(self._conn)
            self.vec_extension_loaded = True
            logger.debug("sqlite-vec extension loaded")
        except Exception as exc:  # noqa: BLE001
            if semantic_enabled:
                logger.error("Failed to load sqlite-vec: %s", exc)
            else:
                logger.debug("Failed to load sqlite-vec (semantic disabled): %s", exc)
        finally:
            # Closing the extension door after the one we wanted is in.
            try:
                self._conn.enable_load_extension(False)
            except Exception:  # noqa: BLE001
                pass

    def _init_schema(self) -> None:
        """Create all tables if they do not already exist.

        Uses ``executescript`` so that CREATE TRIGGER blocks (which contain
        nested semicolons inside BEGIN..END) survive intact.

        FTS5 and vec0 are optional. When they fail, we fall back to applying
        only the statements known to be safe so that legacy callers without
        the [semantic] extra keep working. Semantic features will degrade
        gracefully — see lore.search.
        """
        conn = self._conn
        semantic_enabled = (
            os.getenv("LORE_SEMANTIC_SEARCH", "false").strip().lower() == "true"
        )

        try:
            conn.executescript(_SQLITE_SCHEMA)
            conn.commit()
            # Issue #14: add trust_score to pre-existing databases whose
            # knowledge_kb_entries table was created before this column existed
            # (CREATE TABLE IF NOT EXISTS above is a no-op for such tables).
            self._migrate_trust_score(conn)
            # Probe whether FTS5 and vec0 actually materialised.
            self._probe_optional_features()
            return
        except self._sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            # Two classes of failure we handle:
            #   1. fts5 not compiled in
            #   2. vec0 module not loaded (sqlite-vec missing)
            fts_err = "fts5" in msg or "no such module: fts5" in msg
            vec_err = "vec0" in msg or "no such module: vec0" in msg
            if not (fts_err or vec_err):
                raise
            if semantic_enabled:
                # Hard failure if user explicitly asked for semantic search.
                logger.error(
                    "LORE_SEMANTIC_SEARCH=true but required SQLite features are "
                    "unavailable (%s). Install '.[semantic]' extra and use a "
                    "Python build with FTS5 enabled.",
                    exc,
                )
                raise
            logger.warning(
                "Optional SQLite features unavailable (%s); falling back to "
                "core schema only. Enable LORE_SEMANTIC_SEARCH and install the "
                "[semantic] extra to use semantic/hybrid search.",
                exc,
            )
            self._init_core_schema_only(conn)
            # Issue #14: same idempotent column migration on the fallback path.
            self._migrate_trust_score(conn)
            self._probe_optional_features()

    def _migrate_trust_score(self, conn) -> None:
        """Add knowledge_kb_entries.trust_score to pre-existing databases.

        SQLite has no ``ADD COLUMN IF NOT EXISTS``, so we inspect the table's
        columns via ``PRAGMA table_info`` and only issue the ALTER when the
        column is absent. Fresh databases already have the column from
        ``_SQLITE_SCHEMA`` / ``_CORE_SQLITE_STATEMENTS``, making this a no-op.
        Idempotent and safe to call on every startup. Mirrors Issue #14's
        PostgreSQL ``ADD COLUMN IF NOT EXISTS trust_score REAL DEFAULT 1.0``.
        """
        try:
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(knowledge_kb_entries)")
            }
            if "trust_score" not in cols:
                conn.execute(
                    "ALTER TABLE knowledge_kb_entries ADD COLUMN trust_score REAL DEFAULT 1.0"
                )
                conn.commit()
                logger.info("Migrated knowledge_kb_entries: added trust_score column")
        except self._sqlite3.OperationalError as exc:  # pragma: no cover - defensive
            # Never let a column migration failure break schema init.
            logger.warning("trust_score column migration skipped: %s", exc)

    def _init_core_schema_only(self, conn) -> None:
        """Fallback path: apply only the plain CREATE TABLE statements.

        Skips CREATE VIRTUAL TABLE (fts5/vec0) and CREATE TRIGGER (which
        depend on the FTS5 table). Safe to call repeatedly.
        """
        for stmt in _CORE_SQLITE_STATEMENTS:
            try:
                conn.execute(stmt)
            except self._sqlite3.OperationalError as exc:
                # Should never happen for core CREATE TABLE; surface clearly.
                logger.error("Core schema statement failed: %s\nSQL:\n%s", exc, stmt)
                raise
        conn.commit()

    def _probe_optional_features(self) -> None:
        """Detect whether FTS5 and vec0 are available on this connection."""
        try:
            self._conn.execute("SELECT 1 FROM knowledge_kb_entries_fts LIMIT 0")
            self.fts5_available = True
        except Exception:  # noqa: BLE001
            self.fts5_available = False
        try:
            self._conn.execute("SELECT 1 FROM knowledge_kb_vec_embeddings LIMIT 0")
            # vec0 table only readable if extension is loaded
            if not self.vec_extension_loaded:
                # Table may persist on disk from a previous run; mark loaded.
                self.vec_extension_loaded = True
        except Exception:  # noqa: BLE001
            self.vec_extension_loaded = False

    def table(self, name: str) -> SqliteTableQuery:
        """Start a query on a table (Supabase-compatible interface)."""
        return SqliteTableQuery(self, name)

    def rpc(self, function_name: str, params: dict[str, Any] = None) -> QueryResult:
        """
        RPC stub — not used for core KB operations in the SQLite backend.

        Returns an empty QueryResult to keep callers happy.
        """
        logger.debug(
            "SqliteClient.rpc called for '%s' — returning empty result", function_name
        )
        return QueryResult(data=[])

    def close(self):
        """Close the SQLite connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug("Closed SQLite connection")


def get_db_client(
    backend: DatabaseBackend = None, **kwargs
) -> Union[LocalPostgresClient, SupabaseWrapper]:
    """
    Get database client based on backend configuration.

    Args:
        backend: Force specific backend (default: read from DB_BACKEND env var)
        **kwargs: Additional connection parameters

    Returns:
        Database client (LocalPostgresClient or SupabaseWrapper)

    Environment Variables:
        DB_BACKEND: "local"/"postgres"/"postgresql", "sqlite", or "supabase"
            (default: "supabase"). "local", "postgres", and "postgresql" all
            select the same local PostgreSQL backend.

        For local PostgreSQL (local/postgres/postgresql):
            DB_HOST: PostgreSQL host (default: localhost)
            DB_PORT: PostgreSQL port (default: 5433)
            DB_NAME: Database name (default: lore)
            DB_USER: Username (default: lore_user)
            DB_PASSWORD: Password

        For supabase:
            SUPABASE_URL: Supabase project URL
            SUPABASE_KEY: Supabase service role key
    """
    if backend is None:
        backend_str = os.getenv("DB_BACKEND", "supabase").lower()
        try:
            backend = DatabaseBackend(backend_str)
        except ValueError:
            logger.warning(
                f"Unknown DB_BACKEND '{backend_str}', falling back to supabase"
            )
            backend = DatabaseBackend.SUPABASE

    # LOCAL / POSTGRES / POSTGRESQL all map to the same local PostgreSQL client.
    # The README tells users ``export DB_BACKEND=postgres``; without the alias
    # check here that value used to fall through to the Supabase branch and
    # crash demanding SUPABASE_URL. _backend_kind() already treats these three
    # spellings as the PostgreSQL path — this keeps get_db_client() consistent.
    if backend in (
        DatabaseBackend.LOCAL,
        DatabaseBackend.POSTGRES,
        DatabaseBackend.POSTGRESQL,
    ):
        return LocalPostgresClient(
            host=kwargs.get("host", os.getenv("DB_HOST", "localhost")),
            port=int(kwargs.get("port", os.getenv("DB_PORT", "5433"))),
            database=kwargs.get("database", os.getenv("DB_NAME", "lore")),
            user=kwargs.get("user", os.getenv("DB_USER", "lore_user")),
            password=kwargs.get("password", os.getenv("DB_PASSWORD", "")),
        )
    elif backend == DatabaseBackend.SQLITE:
        from pathlib import Path

        db_path = kwargs.get(
            "db_path",
            os.getenv(
                "SQLITE_DB_PATH",
                str(
                    Path(os.getenv("KNOWLEDGE_DATA_DIR", "./knowledge-data"))
                    / "knowledge.db"
                ),
            ),
        )
        return SqliteClient(db_path=db_path)
    else:
        url = kwargs.get("url", os.getenv("SUPABASE_URL"))
        key = kwargs.get(
            "key", os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        )

        if not url or not key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_KEY environment variables are required "
                "for Supabase backend"
            )

        return SupabaseWrapper(url, key)


# Convenience function for backward compatibility
def get_database_backend() -> DatabaseBackend:
    """Get current database backend from environment."""
    backend_str = os.getenv("DB_BACKEND", "supabase").lower()
    try:
        return DatabaseBackend(backend_str)
    except ValueError:
        return DatabaseBackend.SUPABASE
