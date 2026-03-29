-- Initialize Knowledge MCP Database
-- This script runs when the PostgreSQL container starts for the first time

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create initial schema if needed
-- (The Knowledge MCP server will handle table creation)

-- Set up basic configuration
ALTER DATABASE knowledge SET timezone TO 'UTC';

-- Create indexes for better performance (optional)
-- These will be created by the application, but can be pre-created for performance
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_entries_created_at ON knowledge_entries(created_at);
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_knowledge_entries_embedding ON knowledge_entries USING ivfflat(embedding vector_cosine_ops);

-- Log successful initialization
DO $$
BEGIN
    RAISE NOTICE 'Knowledge MCP database initialized successfully';
END $$;