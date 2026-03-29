-- Initial schema for Knowledge MCP
-- This creates the basic tables and indexes for the knowledge management system

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Knowledge Base Tables
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content TEXT NOT NULL,
    content_hash VARCHAR(64) UNIQUE,
    embedding vector(1536),  -- OpenAI embedding dimension
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Research Tables
CREATE TABLE IF NOT EXISTS research_experiments (
    id VARCHAR(255) PRIMARY KEY,
    description TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_notes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    experiment_id VARCHAR(255) REFERENCES research_experiments(id),
    content TEXT NOT NULL,
    note_type VARCHAR(50) DEFAULT 'observation',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_sources (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    url TEXT,
    title TEXT,
    content TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS research_source_experiments (
    source_id UUID REFERENCES research_sources(id),
    experiment_id VARCHAR(255) REFERENCES research_experiments(id),
    PRIMARY KEY (source_id, experiment_id)
);

-- Journal Tables
CREATE TABLE IF NOT EXISTS journal_entries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    content TEXT NOT NULL,
    tags TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- MCP Index Tables
CREATE TABLE IF NOT EXISTS mcp_servers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(255) UNIQUE NOT NULL,
    description TEXT,
    metadata JSONB DEFAULT '{}',
    last_scanned TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcp_tools (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    server_id UUID REFERENCES mcp_servers(id),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    parameters JSONB DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(server_id, name)
);

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_knowledge_entries_created_at
    ON knowledge_entries(created_at);

CREATE INDEX IF NOT EXISTS idx_knowledge_entries_content_hash
    ON knowledge_entries(content_hash);

CREATE INDEX IF NOT EXISTS idx_knowledge_entries_metadata
    ON knowledge_entries USING gin(metadata);

-- Vector similarity index (will be created after data is inserted)
-- CREATE INDEX IF NOT EXISTS idx_knowledge_entries_embedding
--     ON knowledge_entries USING ivfflat(embedding vector_cosine_ops)
--     WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_research_notes_experiment
    ON research_notes(experiment_id);

CREATE INDEX IF NOT EXISTS idx_research_notes_created_at
    ON research_notes(created_at);

CREATE INDEX IF NOT EXISTS idx_journal_entries_created_at
    ON journal_entries(created_at);

CREATE INDEX IF NOT EXISTS idx_journal_entries_tags
    ON journal_entries USING gin(tags);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_server
    ON mcp_tools(server_id);

-- Create updated_at trigger for knowledge_entries
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_knowledge_entries_updated_at
    BEFORE UPDATE ON knowledge_entries
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_research_experiments_updated_at
    BEFORE UPDATE ON research_experiments
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();