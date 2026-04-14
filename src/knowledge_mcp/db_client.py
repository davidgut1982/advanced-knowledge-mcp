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

import os
import logging
from enum import Enum
from typing import Optional, Any, Dict, List, Union
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class DatabaseBackend(Enum):
    """Supported database backends."""
    SUPABASE = "supabase"
    LOCAL = "local"


@dataclass
class QueryResult:
    """Unified query result across backends."""
    data: List[Dict[str, Any]]
    count: Optional[int] = None
    error: Optional[str] = None


class LocalPostgresClient:
    """
    PostgreSQL client that mimics Supabase's query interface.

    Provides a compatible API so code can switch backends with minimal changes.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5433,
        database: str = "mpm_system",
        user: str = "latvian_user",
        password: str = ""
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

        logger.info(f"LocalPostgresClient initialized for {database}@{host}:{port}")

    def _get_connection(self):
        """Get or create database connection."""
        if self._conn is None or self._conn.closed:
            self._conn = self._psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=self.user,
                password=self.password
            )
            # Force UTF-8 decoding even when server_encoding is SQL_ASCII.
            # Without this, psycopg2 uses ASCII and chokes on multi-byte chars (0xe2 etc).
            self._conn.set_client_encoding('UTF8')
            self._conn.autocommit = True
            logger.debug("Created new PostgreSQL connection")
        return self._conn

    def table(self, name: str) -> 'TableQuery':
        """Start a query on a table (Supabase-compatible interface)."""
        return TableQuery(self, name)

    def rpc(self, function_name: str, params: Dict[str, Any] = None) -> QueryResult:
        """Call a stored function (RPC)."""
        conn = self._get_connection()
        cursor = conn.cursor(cursor_factory=self._extras.RealDictCursor)

        if params:
            # Build named parameter syntax: function_name(param1 => %s, param2 => %s)
            param_list = ', '.join([f"{k} => %s" for k in params.keys()])
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
        self._filters: List[tuple] = []
        self._order_by: List[tuple] = []
        self._limit_val: Optional[int] = None
        self._offset_val: Optional[int] = None
        self._on_conflict: Optional[str] = None
        self._count_mode: Optional[str] = None
        self._single_result: bool = False  # For maybe_single() support
        self._or_filters: List[str] = []  # For OR conditions

    def select(self, columns: str = "*", count: str = None) -> 'TableQuery':
        """Select columns from table."""
        self._operation = "select"
        self._columns = columns
        self._count_mode = count  # "exact", "planned", or "estimated"
        return self

    def insert(self, data: Union[Dict, List[Dict]], upsert: bool = False) -> 'TableQuery':
        """Insert data into table."""
        self._operation = "upsert" if upsert else "insert"
        self._data = data if isinstance(data, list) else [data]
        return self

    def upsert(self, data: Union[Dict, List[Dict]], on_conflict: str = None) -> 'TableQuery':
        """Upsert (insert or update on conflict)."""
        self._operation = "upsert"
        self._data = data if isinstance(data, list) else [data]
        self._on_conflict = on_conflict
        return self

    def update(self, data: Dict) -> 'TableQuery':
        """Update rows in table."""
        self._operation = "update"
        self._data = data
        return self

    def delete(self) -> 'TableQuery':
        """Delete rows from table."""
        self._operation = "delete"
        return self

    # Filter methods
    def eq(self, column: str, value: Any) -> 'TableQuery':
        """Equal to."""
        self._filters.append((column, "=", value))
        return self

    def neq(self, column: str, value: Any) -> 'TableQuery':
        """Not equal to."""
        self._filters.append((column, "!=", value))
        return self

    def gt(self, column: str, value: Any) -> 'TableQuery':
        """Greater than."""
        self._filters.append((column, ">", value))
        return self

    def gte(self, column: str, value: Any) -> 'TableQuery':
        """Greater than or equal."""
        self._filters.append((column, ">=", value))
        return self

    def lt(self, column: str, value: Any) -> 'TableQuery':
        """Less than."""
        self._filters.append((column, "<", value))
        return self

    def lte(self, column: str, value: Any) -> 'TableQuery':
        """Less than or equal."""
        self._filters.append((column, "<=", value))
        return self

    def like(self, column: str, pattern: str) -> 'TableQuery':
        """LIKE pattern match."""
        self._filters.append((column, "LIKE", pattern))
        return self

    def ilike(self, column: str, pattern: str) -> 'TableQuery':
        """Case-insensitive LIKE."""
        self._filters.append((column, "ILIKE", pattern))
        return self

    def in_(self, column: str, values: List[Any]) -> 'TableQuery':
        """IN list of values."""
        self._filters.append((column, "IN", tuple(values)))
        return self

    def is_(self, column: str, value: Any) -> 'TableQuery':
        """IS (for NULL checks)."""
        self._filters.append((column, "IS", value))
        return self

    def order(self, column: str, desc: bool = False) -> 'TableQuery':
        """Order by column."""
        self._order_by.append((column, "DESC" if desc else "ASC"))
        return self

    def limit(self, count: int) -> 'TableQuery':
        """Limit number of results."""
        self._limit_val = count
        return self

    def offset(self, count: int) -> 'TableQuery':
        """Offset results."""
        self._offset_val = count
        return self

    def or_(self, conditions: str) -> 'TableQuery':
        """
        Add OR conditions (Supabase-compatible).

        Example: .or_("title.wfts.search_term,content.wfts.search_term")

        For local PostgreSQL, we parse this into proper full-text search.
        """
        # Store raw OR condition for processing in execute
        self._or_filters.append(conditions)
        return self

    def maybe_single(self) -> 'TableQuery':
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
                placeholders = ', '.join(['%s'] * len(value))
                conditions.append(f"{column} IN ({placeholders})")
                values.extend(value)
            else:
                conditions.append(f"{column} {op} %s")
                values.append(value)

        # Handle OR filters (full-text search)
        for or_condition in self._or_filters:
            # Parse Supabase-style: "title.wfts.query,content.wfts.query"
            or_parts = or_condition.split(',')
            or_conditions = []

            for part in or_parts:
                if '.wfts.' in part or '.plfts.' in part:
                    # Full-text search: "title.wfts.search_term"
                    column, operator, search_term = part.split('.', 2)

                    # Use PostgreSQL's full-text search
                    if operator == 'wfts':
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
            count = cursor.fetchone()['count']

        return QueryResult(data=data, count=count)

    def _serialize_value(self, value):
        """Serialize Python objects to PostgreSQL-compatible types."""
        import json
        if isinstance(value, dict):
            # Dicts are serialized to JSONB
            return json.dumps(value)
        elif isinstance(value, list):
            # psycopg2 natively handles Python lists as PostgreSQL arrays
            # No need to use adapt() - just pass the list directly
            # Python [] -> PostgreSQL ARRAY[]::text[]
            # Python ['a', 'b'] -> PostgreSQL ARRAY['a', 'b']
            return value
        return value

    def _execute_insert(self, cursor) -> QueryResult:
        """Execute INSERT query."""
        if not self._data:
            return QueryResult(data=[], error="No data to insert")

        columns = list(self._data[0].keys())
        col_names = ', '.join(columns)
        placeholders = ', '.join(['%s'] * len(columns))

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
        col_names = ', '.join(columns)
        placeholders = ', '.join(['%s'] * len(columns))

        # Determine conflict target
        conflict_col = self._on_conflict or columns[0]  # Default to first column

        # Build SET clause for update
        update_sets = ', '.join([f"{col} = EXCLUDED.{col}" for col in columns if col != conflict_col])

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

        sql = f"UPDATE {self.table} SET {', '.join(set_parts)}{where_clause} RETURNING *"

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

    def rpc(self, function_name: str, params: Dict[str, Any] = None) -> QueryResult:
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
                if hasattr(result, 'execute'):
                    return SupabaseTableWrapper(result)
                return result
            return wrapper
        return attr

    def execute(self) -> QueryResult:
        """Execute and return QueryResult."""
        response = self._query.execute()
        return QueryResult(
            data=response.data if response.data else [],
            count=getattr(response, 'count', None)
        )


def get_db_client(
    backend: DatabaseBackend = None,
    **kwargs
) -> Union[LocalPostgresClient, SupabaseWrapper]:
    """
    Get database client based on backend configuration.

    Args:
        backend: Force specific backend (default: read from DB_BACKEND env var)
        **kwargs: Additional connection parameters

    Returns:
        Database client (LocalPostgresClient or SupabaseWrapper)

    Environment Variables:
        DB_BACKEND: "local" or "supabase" (default: "supabase")

        For local:
            DB_HOST: PostgreSQL host (default: localhost)
            DB_PORT: PostgreSQL port (default: 5433)
            DB_NAME: Database name (default: mpm_system)
            DB_USER: Username (default: latvian_user)
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
            logger.warning(f"Unknown DB_BACKEND '{backend_str}', falling back to supabase")
            backend = DatabaseBackend.SUPABASE

    if backend == DatabaseBackend.LOCAL:
        return LocalPostgresClient(
            host=kwargs.get("host", os.getenv("DB_HOST", "localhost")),
            port=int(kwargs.get("port", os.getenv("DB_PORT", "5433"))),
            database=kwargs.get("database", os.getenv("DB_NAME", "mpm_system")),
            user=kwargs.get("user", os.getenv("DB_USER", "latvian_user")),
            password=kwargs.get("password", os.getenv("DB_PASSWORD", "")),
        )
    else:
        url = kwargs.get("url", os.getenv("SUPABASE_URL"))
        key = kwargs.get("key", os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY"))

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
