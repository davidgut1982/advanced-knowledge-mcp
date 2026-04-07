-- Create initial schema for knowledge-mcp on local PostgreSQL
-- Based on the Supabase structure but adapted for local use

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create knowledge schema
CREATE SCHEMA IF NOT EXISTS knowledge;

-- Create kb_entries table (main knowledge base entries)
CREATE TABLE IF NOT EXISTS knowledge.kb_entries (
    kb_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    topic TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_doc TEXT,
    source_section TEXT,
    line_range INTEGER[]
);

-- Create indexes for kb_entries
CREATE INDEX IF NOT EXISTS idx_kb_entries_topic ON knowledge.kb_entries(topic);
CREATE INDEX IF NOT EXISTS idx_kb_entries_title ON knowledge.kb_entries(title);
CREATE INDEX IF NOT EXISTS idx_kb_entries_tags ON knowledge.kb_entries USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_kb_entries_source_doc ON knowledge.kb_entries(source_doc);
CREATE INDEX IF NOT EXISTS idx_kb_entries_content_search ON knowledge.kb_entries USING GIN(to_tsvector('english', content));
CREATE INDEX IF NOT EXISTS idx_kb_entries_title_search ON knowledge.kb_entries USING GIN(to_tsvector('english', title));

-- Create kb_doc_sync table
CREATE TABLE IF NOT EXISTS knowledge.kb_doc_sync (
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

-- Create indexes for kb_doc_sync
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_doc_path ON knowledge.kb_doc_sync(doc_path);
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_hash ON knowledge.kb_doc_sync(doc_hash);
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_modified ON knowledge.kb_doc_sync(last_modified_at);
CREATE INDEX IF NOT EXISTS idx_kb_doc_sync_kb_ids ON knowledge.kb_doc_sync USING GIN(kb_ids);

-- Create knowledge graph nodes table
CREATE TABLE IF NOT EXISTS knowledge.kg_nodes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name TEXT NOT NULL,
    node_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create knowledge graph edges table (relationships)
CREATE TABLE IF NOT EXISTS knowledge.kg_edges (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    from_node_id UUID NOT NULL REFERENCES knowledge.kg_nodes(id) ON DELETE CASCADE,
    to_node_id UUID NOT NULL REFERENCES knowledge.kg_nodes(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT different_nodes CHECK (from_node_id != to_node_id)
);

-- Create indexes for knowledge graph tables
CREATE INDEX IF NOT EXISTS idx_kg_nodes_name ON knowledge.kg_nodes(name);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_type ON knowledge.kg_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_name_type ON knowledge.kg_nodes(name, node_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_properties ON knowledge.kg_nodes USING GIN(properties);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_search ON knowledge.kg_nodes USING GIN(to_tsvector('english', name));

CREATE INDEX IF NOT EXISTS idx_kg_edges_from ON knowledge.kg_edges(from_node_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_to ON knowledge.kg_edges(to_node_id);
CREATE INDEX IF NOT EXISTS idx_kg_edges_type ON knowledge.kg_edges(relationship_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_from_type ON knowledge.kg_edges(from_node_id, relationship_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_to_type ON knowledge.kg_edges(to_node_id, relationship_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_properties ON knowledge.kg_edges USING GIN(properties);

-- Create trigger function for updated_at timestamps
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for updated_at timestamps
CREATE TRIGGER IF NOT EXISTS update_kb_entries_updated_at
    BEFORE UPDATE ON knowledge.kb_entries
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER IF NOT EXISTS update_kb_doc_sync_updated_at
    BEFORE UPDATE ON knowledge.kb_doc_sync
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER IF NOT EXISTS update_kg_nodes_updated_at
    BEFORE UPDATE ON knowledge.kg_nodes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER IF NOT EXISTS update_kg_edges_updated_at
    BEFORE UPDATE ON knowledge.kg_edges
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();