-- CI bootstrap schema for integration tests.
--
-- Creates the minimum schema that LocalPostgresClient._init_schema expects to
-- find on first connection.  The rest of the schema (kb_embeddings, telemetry,
-- hard_negative_pairs) is auto-created by _init_schema at runtime, so we only
-- need the base knowledge schema + kb_entries here.
--
-- Key differences from scripts/migration/create_local_schema.sql:
--   - kb_id is TEXT (not UUID) — the application generates its own prefixed IDs
--     like "kb_abc123"; UUID primary key causes FK type-mismatch errors.
--   - author, source_type, verified columns are included (required by search.py
--     SELECT queries).
--   - Trigger DDL omitted (CREATE TRIGGER IF NOT EXISTS is PostgreSQL 16+ only;
--     pg_isready passes earlier; triggers are not needed by the test suite).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS knowledge;

CREATE TABLE IF NOT EXISTS knowledge.kb_entries (
    kb_id        TEXT PRIMARY KEY,
    topic        TEXT NOT NULL,
    title        TEXT NOT NULL,
    content      TEXT NOT NULL,
    tags         JSONB DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_doc   TEXT,
    source_section TEXT,
    line_range   INTEGER[],
    author       TEXT,
    source_type  TEXT,
    verified     BOOLEAN,
    trust_score  REAL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_kb_entries_topic
    ON knowledge.kb_entries(topic);
CREATE INDEX IF NOT EXISTS idx_kb_entries_title
    ON knowledge.kb_entries(title);
CREATE INDEX IF NOT EXISTS idx_kb_entries_tags
    ON knowledge.kb_entries USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_kb_entries_source_doc
    ON knowledge.kb_entries(source_doc);
CREATE INDEX IF NOT EXISTS idx_kb_entries_content_search
    ON knowledge.kb_entries USING GIN(to_tsvector('english', content));
CREATE INDEX IF NOT EXISTS idx_kb_entries_title_search
    ON knowledge.kb_entries USING GIN(to_tsvector('english', title));

-- FTS: combined English title+content expression index (matches fts_search_postgres WHERE clause exactly)
CREATE INDEX IF NOT EXISTS idx_kb_entries_fts_english_combined
    ON knowledge.kb_entries
    USING gin(
        to_tsvector('english',
            coalesce(title, '') || ' ' || coalesce(content, ''))
    );

-- Planner tuning: SSD-appropriate random I/O cost (default 4.0 causes GIN index avoidance)
ALTER DATABASE lore SET random_page_cost = 1.1;

CREATE TABLE IF NOT EXISTS knowledge.kb_doc_sync (
    doc_path     TEXT PRIMARY KEY,
    doc_hash     TEXT NOT NULL,
    kb_ids       JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The kb_doc_sync write path always supplies both timestamps; NOT NULL
    -- DEFAULT NOW() matches production intent and prevents NULL drift.
    last_synced_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_modified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    strategy     TEXT,
    metadata     JSONB DEFAULT '{}'::jsonb
);
