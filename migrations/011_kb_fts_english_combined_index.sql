-- Migration 011: Add GIN index for combined English-config FTS
-- (title || content expression to match fts_search_postgres WHERE clause exactly)
--
-- NOTE: Uses CREATE INDEX CONCURRENTLY — must run OUTSIDE a transaction block.
-- If this index exists but is INVALID (failed partial build), drop and recreate:
--   DROP INDEX CONCURRENTLY IF EXISTS idx_kb_entries_fts_english_combined;
--   Then re-run this script.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_kb_entries_fts_english_combined
    ON knowledge.kb_entries
    USING gin(
        to_tsvector('english',
            coalesce(title, '') || ' ' || coalesce(content, ''))
    );
