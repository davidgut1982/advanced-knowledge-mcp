-- Create compatibility views for knowledge-mcp server
-- Maps the migrated column names to the expected column names

-- Drop existing views if they exist
DROP VIEW IF EXISTS knowledge.kg_nodes_compat;
DROP VIEW IF EXISTS knowledge.kg_edges_compat;

-- Create kg_nodes view with expected column names
CREATE VIEW knowledge.kg_nodes_compat AS
SELECT
    uuid_generate_v4() as id,  -- Generate UUID for id column (server expects this)
    label as name,             -- Map label -> name
    kind as node_type,         -- Map kind -> node_type
    properties,
    created_at,
    created_at as updated_at   -- Add updated_at column (required by server)
FROM knowledge.kg_nodes;

-- Create kg_edges view with expected column names
CREATE VIEW knowledge.kg_edges_compat AS
SELECT
    uuid_generate_v4() as id,           -- Generate UUID for id column
    from_node as from_node_id,          -- Map from_node -> from_node_id
    to_node as to_node_id,              -- Map to_node -> to_node_id
    relation as relationship_type,      -- Map relation -> relationship_type
    properties,
    created_at,
    created_at as updated_at           -- Add updated_at column
FROM knowledge.kg_edges;

-- Grant permissions to latvian_user
GRANT SELECT ON knowledge.kg_nodes_compat TO latvian_user;
GRANT SELECT ON knowledge.kg_edges_compat TO latvian_user;