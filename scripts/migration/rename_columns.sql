-- Rename columns in knowledge graph tables to match expected schema
-- This is a one-time migration to make the existing data compatible

-- Drop the compatibility views first
DROP VIEW IF EXISTS knowledge.kg_nodes_compat;
DROP VIEW IF EXISTS knowledge.kg_edges_compat;

-- Add UUID column to kg_nodes as id (primary key)
ALTER TABLE knowledge.kg_nodes ADD COLUMN IF NOT EXISTS id UUID DEFAULT uuid_generate_v4();
-- Copy existing node_id values to maintain relationships
UPDATE knowledge.kg_nodes SET id = node_id::uuid WHERE id IS NULL;

-- Add name and node_type columns
ALTER TABLE knowledge.kg_nodes ADD COLUMN IF NOT EXISTS name TEXT;
ALTER TABLE knowledge.kg_nodes ADD COLUMN IF NOT EXISTS node_type TEXT;
ALTER TABLE knowledge.kg_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Copy data from existing columns
UPDATE knowledge.kg_nodes SET
    name = label,
    node_type = kind,
    updated_at = created_at
WHERE name IS NULL;

-- Add columns to kg_edges
ALTER TABLE knowledge.kg_edges ADD COLUMN IF NOT EXISTS id UUID DEFAULT uuid_generate_v4();
-- Copy existing edge_id values
UPDATE knowledge.kg_edges SET id = edge_id::uuid WHERE id IS NULL;

ALTER TABLE knowledge.kg_edges ADD COLUMN IF NOT EXISTS from_node_id UUID;
ALTER TABLE knowledge.kg_edges ADD COLUMN IF NOT EXISTS to_node_id UUID;
ALTER TABLE knowledge.kg_edges ADD COLUMN IF NOT EXISTS relationship_type TEXT;
ALTER TABLE knowledge.kg_edges ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

-- Copy data from existing columns - convert text node IDs to UUIDs
UPDATE knowledge.kg_edges SET
    from_node_id = from_node::uuid,
    to_node_id = to_node::uuid,
    relationship_type = relation,
    updated_at = created_at
WHERE from_node_id IS NULL;

-- Create proper foreign key constraints
-- First, let's make sure the new id column is the primary key for kg_nodes
ALTER TABLE knowledge.kg_nodes DROP CONSTRAINT IF EXISTS kg_nodes_pkey;
ALTER TABLE knowledge.kg_nodes ADD CONSTRAINT kg_nodes_pkey PRIMARY KEY (id);

-- Drop old foreign key constraints
ALTER TABLE knowledge.kg_edges DROP CONSTRAINT IF EXISTS kg_edges_from_node_fkey;
ALTER TABLE knowledge.kg_edges DROP CONSTRAINT IF EXISTS kg_edges_to_node_fkey;

-- Make the new id column primary key for kg_edges
ALTER TABLE knowledge.kg_edges DROP CONSTRAINT IF EXISTS kg_edges_pkey;
ALTER TABLE knowledge.kg_edges ADD CONSTRAINT kg_edges_pkey PRIMARY KEY (id);

-- Add new foreign key constraints
ALTER TABLE knowledge.kg_edges ADD CONSTRAINT kg_edges_from_node_id_fkey
    FOREIGN KEY (from_node_id) REFERENCES knowledge.kg_nodes(id) ON DELETE CASCADE;
ALTER TABLE knowledge.kg_edges ADD CONSTRAINT kg_edges_to_node_id_fkey
    FOREIGN KEY (to_node_id) REFERENCES knowledge.kg_nodes(id) ON DELETE CASCADE;