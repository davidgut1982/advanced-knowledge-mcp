-- Migration 010: pg_trgm GIN index on knowledge.kb_entries.content (Issue #26)
--
-- The lexical / substring search paths (the journal ILIKE fallback and any
-- TableQuery.ilike() filter that compiles to ``content ILIKE '%term%'``) cannot
-- use the existing to_tsvector FTS indexes (idx_kb_search / idx_kb_search_simple
-- from migration 004), so they sequentially scan knowledge.kb_entries — a table
-- bloated by long hermes-conversations transcripts. A trigram GIN index makes
-- those ``ILIKE '%...%'`` substring queries index-accelerated WITHOUT rewriting
-- the query path. Semantic / pgvector search is unaffected by this change.
--
-- Idempotent and safe to run against the existing populated production DB:
--   * CREATE EXTENSION IF NOT EXISTS — no-op if pg_trgm is already installed.
--   * CREATE INDEX CONCURRENTLY IF NOT EXISTS — builds the index without taking
--     an ACCESS EXCLUSIVE lock, so reads and writes against the live table keep
--     working during the (potentially long) build, and a pre-existing index
--     short-circuits via IF NOT EXISTS.
--
-- IMPORTANT — run this OUTSIDE a transaction block. CREATE INDEX CONCURRENTLY
-- cannot run inside a transaction. psql in its default (autocommit) mode is fine;
-- do NOT wrap these statements in BEGIN/COMMIT and do NOT pass -1/--single-transaction.
--
-- Operator command (live DB at 192.168.1.21):
--   psql "host=192.168.1.21 port=5433 dbname=lore user=lore_user" \
--        -v ON_ERROR_STOP=1 -f migrations/010_kb_content_trgm_index.sql
--
-- If CREATE INDEX CONCURRENTLY is interrupted it can leave an INVALID index
-- behind. To recover, drop it and re-run this migration:
--   DROP INDEX CONCURRENTLY IF EXISTS knowledge.idx_kb_entries_content_trgm;
--
-- The PostgreSQL backend's LocalPostgresClient._init_schema() in db_client.py
-- creates the same extension + index on startup so FRESH databases get it too.
-- It uses the NON-CONCURRENT ``CREATE INDEX IF NOT EXISTS`` form there because a
-- fresh DB has an empty/small table (instant build) and bootstrap runs in
-- autocommit. The KB_CONTENT_TRGM_EXTENSION_DDL / KB_CONTENT_TRGM_INDEX_DDL
-- constants mirror the two statements below (sans CONCURRENTLY); a unit test
-- (tests/test_trgm_index_migration.py) enforces that parity.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kb_entries_content_trgm
    ON knowledge.kb_entries USING gin (content gin_trgm_ops);
