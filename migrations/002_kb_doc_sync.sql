-- Migration: Add document sync tracking for knowledge-mcp
-- Version: v1.3
-- Date: 2025-12-08

-- Create kb_doc_sync table
CREATE TABLE IF NOT EXISTS kb_doc_sync (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  doc_path TEXT UNIQUE NOT NULL,
  doc_hash TEXT NOT NULL,
  kb_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
  last_synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_modified_at TIMESTAMPTZ NOT NULL,
  strategy TEXT DEFAULT 'chunked' CHECK (strategy IN ('full', 'chunked', 'summary')),
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_doc_path ON kb_doc_sync(doc_path);
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_hash ON kb_doc_sync(doc_hash);
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_modified ON kb_doc_sync(last_modified_at);
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_kb_ids ON kb_doc_sync USING GIN(kb_ids);

-- Enable Row Level Security
ALTER TABLE kb_doc_sync ENABLE ROW LEVEL SECURITY;

-- Policy for service role (full access)
DROP POLICY IF EXISTS "Service role has full access" ON kb_doc_sync;
CREATE POLICY "Service role has full access" ON kb_doc_sync
  FOR ALL
  USING (auth.role() = 'service_role');

-- Add source_doc and source_section fields to kb_entries for bidirectional linking
ALTER TABLE kb_entries ADD COLUMN IF NOT EXISTS source_doc TEXT;
ALTER TABLE kb_entries ADD COLUMN IF NOT EXISTS source_section TEXT;
ALTER TABLE kb_entries ADD COLUMN IF NOT EXISTS line_range INTEGER[];

-- Create index on source_doc for reverse lookups
CREATE INDEX IF NOT EXISTS idx_kb_entries_source_doc ON kb_entries(source_doc);

COMMENT ON TABLE kb_doc_sync IS 'Tracks document-to-KB sync state for change detection and bidirectional linking';
COMMENT ON COLUMN kb_doc_sync.doc_path IS 'Absolute path to source markdown file';
COMMENT ON COLUMN kb_doc_sync.doc_hash IS 'SHA-256 hash of document content for change detection';
COMMENT ON COLUMN kb_doc_sync.kb_ids IS 'Array of KB entry IDs created from this document';
COMMENT ON COLUMN kb_doc_sync.strategy IS 'Ingestion strategy: full, chunked, or summary';
