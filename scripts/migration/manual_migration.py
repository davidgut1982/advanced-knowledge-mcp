#!/usr/bin/env python3
"""
Apply knowledge graph migration manually to Supabase database.
"""

import os
from supabase import create_client, Client

# Supabase credentials from environment
url = os.getenv("SUPABASE_URL", "https://your-project.supabase.co")
key = os.getenv("SUPABASE_KEY", "your_service_role_key_here")

supabase: Client = create_client(url, key)

# Create the tables directly using PostgreSQL compatible SQL
migration_sql = """
-- Create knowledge schema if it doesn't exist
CREATE SCHEMA IF NOT EXISTS knowledge;

-- Create knowledge graph nodes table
CREATE TABLE IF NOT EXISTS knowledge.kg_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    node_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Create knowledge graph edges table (relationships)
CREATE TABLE IF NOT EXISTS knowledge.kg_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_node_id UUID NOT NULL REFERENCES knowledge.kg_nodes(id) ON DELETE CASCADE,
    to_node_id UUID NOT NULL REFERENCES knowledge.kg_nodes(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    properties JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT different_nodes CHECK (from_node_id != to_node_id)
);

-- Create indexes for performance
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

-- Create trigger for updated_at timestamps
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_kg_nodes_updated_at BEFORE UPDATE ON knowledge.kg_nodes
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_kg_edges_updated_at BEFORE UPDATE ON knowledge.kg_edges
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
"""

try:
    print("Applying knowledge graph migration to Supabase...")

    # Execute the migration SQL
    result = supabase.rpc('exec_sql', {'sql': migration_sql}).execute()

    print("✓ Migration applied successfully!")
    print("Tables created: knowledge.kg_nodes, knowledge.kg_edges")

except Exception as e:
    print(f"Migration failed: {e}")
    print("\nNote: You may need to apply this manually in the Supabase SQL Editor:")
    print("1. Go to Supabase dashboard -> SQL Editor")
    print("2. Create a new query")
    print("3. Paste the migration SQL")
    print("4. Run the query")
    print(f"\nMigration SQL:\n{migration_sql}")