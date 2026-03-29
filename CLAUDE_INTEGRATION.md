# Claude Code Integration Guide

Complete guide for integrating Knowledge-MCP with Claude Code for intelligent knowledge management workflows.

## 🎯 Overview

Knowledge-MCP transforms Claude Code agents from simple chat assistants into intelligent knowledge workers capable of:

- **Persistent Learning**: Automatically capturing and organizing insights
- **Research Workflows**: Tracking sources, experiments, and findings
- **Document Intelligence**: Auto-syncing documentation with change detection
- **Decision Logging**: Maintaining audit trails of architectural decisions

## 🔧 MCP Configuration

### Basic Configuration

Add to your Claude Code MCP configuration file:

#### Location: `~/.claude/mcp_servers.json` (macOS/Linux) or `%APPDATA%\.claude\mcp_servers.json` (Windows)

```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "knowledge-mcp",
      "env": {
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/knowledge_db",
        "KNOWLEDGE_DATA_DIR": "/opt/knowledge-data"
      }
    }
  }
}
```

### Advanced Configuration

```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "command": "knowledge-mcp",
      "args": ["--config", "/etc/knowledge-mcp/config.yaml"],
      "env": {
        "DATABASE_URL": "postgresql://knowledge_user:secure_pass@localhost:5432/knowledge_db",
        "SUPABASE_URL": "https://your-project.supabase.co",
        "SUPABASE_KEY": "your-service-role-key",
        "KNOWLEDGE_DATA_DIR": "/var/lib/knowledge-data",
        "SENTRY_DSN": "https://your-sentry-dsn",
        "SENTRY_ENVIRONMENT": "production",
        "LOG_LEVEL": "INFO"
      },
      "timeout": 30000,
      "restart": true
    }
  }
}
```

### Environment Configuration Options

| Variable | Purpose | Default | Required |
|----------|---------|---------|----------|
| `DATABASE_URL` | PostgreSQL connection | None | No (uses JSON fallback) |
| `SUPABASE_URL` | Supabase project URL | None | No |
| `SUPABASE_KEY` | Supabase service key | None | No |
| `KNOWLEDGE_DATA_DIR` | JSON storage path | `./data` | No |
| `LOG_LEVEL` | Logging verbosity | `INFO` | No |
| `SENTRY_DSN` | Error tracking | None | No |

## 🚀 Getting Started with Claude Code

### 1. Verify Integration

After configuring the MCP server, restart Claude Code and test:

```
Tell Claude: "List the available knowledge management tools"
```

Expected response should include:
- `kb_add`, `kb_search`, `kb_get`, `kb_list`
- `research_add_note`, `research_list_notes`, etc.
- `journal_append`, `journal_list`, etc.

### 2. Basic Usage Patterns

#### Knowledge Base Operations

```
"Add this information to the knowledge base under 'api-design':
REST APIs should use nouns for resources, not verbs.
Use POST for creation, GET for retrieval, PUT for updates."
```

```
"Search the knowledge base for authentication patterns"
```

```
"Show me all knowledge entries about database optimization"
```

#### Research Workflows

```
"Start a research note about microservices architecture.
Add the Martin Fowler article as a source."
```

```
"Log an experiment: tested Redis vs Memcached for session storage.
Redis performed 15% better under high load."
```

```
"Show me all research notes tagged with 'performance'"
```

#### Document Intelligence

```
"Sync the documentation in /project/docs to the knowledge base"
```

```
"Check which documentation files have changed since last sync"
```

```
"Show me the source document for knowledge entry kb_001234"
```

## 🎭 Agent Usage Patterns

### Documentation Agent

Create an agent specialized in documentation management:

```json
{
  "agent": "documentation-manager",
  "instructions": [
    "You are a documentation specialist.",
    "Automatically sync project documentation to the knowledge base.",
    "Detect changes and update relevant knowledge entries.",
    "Maintain bidirectional links between KB entries and source files."
  ],
  "tools": [
    "knowledge-mcp.kb_ingest_dir",
    "knowledge-mcp.kb_sync_status",
    "knowledge-mcp.kb_link_to_source"
  ]
}
```

### Research Assistant

Create an agent for research workflows:

```json
{
  "agent": "research-assistant",
  "instructions": [
    "You help manage research projects and experiments.",
    "Track sources, hypotheses, and results systematically.",
    "Link related research notes and maintain research continuity.",
    "Generate research summaries and progress reports."
  ],
  "tools": [
    "knowledge-mcp.research_add_note",
    "knowledge-mcp.research_add_source",
    "knowledge-mcp.research_log_experiment"
  ]
}
```

### Architecture Decision Recorder

```json
{
  "agent": "adr-manager",
  "instructions": [
    "You help record and track architectural decisions.",
    "Use the journal system to log decision rationale.",
    "Reference related knowledge base entries.",
    "Maintain decision audit trails."
  ],
  "tools": [
    "knowledge-mcp.journal_append",
    "knowledge-mcp.snapshot_config",
    "knowledge-mcp.kb_search"
  ]
}
```

## 🔄 Automated Workflows

### Continuous Documentation Sync

Set up automated documentation synchronization:

```bash
# Add to crontab or CI/CD pipeline
*/15 * * * * claude-agent "Check /project/docs for changes and update knowledge base"
```

### Research Progress Tracking

Weekly research summaries:

```bash
# Weekly research digest
0 9 * * 1 claude-agent "Generate a summary of last week's research progress"
```

### Configuration Monitoring

Track configuration changes:

```bash
# After deployment
claude-agent "Snapshot production configuration under label 'v2.1-deployment'"
```

## 🛠️ Advanced Integration

### Custom Tool Combinations

#### Smart Document Processing

```python
# Pseudo-code for Claude Code agent behavior
async def process_documentation_update(file_path):
    # 1. Check if file changed
    status = await kb_sync_status(dir_path=os.path.dirname(file_path))

    if file_needs_update(file_path, status):
        # 2. Ingest with appropriate strategy
        result = await kb_ingest_doc(
            doc_path=file_path,
            strategy="chunked" if file_is_large(file_path) else "full",
            tags=extract_tags_from_path(file_path)
        )

        # 3. Log the update
        await journal_append(
            content=f"Updated documentation: {file_path} -> {result['kb_entries_created']} KB entries"
        )

        return result

    return {"status": "no_changes"}
```

#### Research Project Management

```python
async def start_research_project(topic, initial_sources):
    # 1. Create initial research note
    note = await research_add_note(
        title=f"Research Project: {topic}",
        content="Initial project setup and objectives...",
        tags=[topic.lower().replace(' ', '-')]
    )

    # 2. Add initial sources
    source_ids = []
    for source in initial_sources:
        source_result = await research_add_source(
            url=source['url'],
            title=source['title'],
            tags=[topic.lower().replace(' ', '-')]
        )
        source_ids.append(source_result['id'])

    # 3. Log project start
    await journal_append(
        content=f"Started research project: {topic}\nSources: {len(source_ids)}\nNote ID: {note['id']}"
    )

    return {
        "note_id": note['id'],
        "source_ids": source_ids,
        "status": "project_started"
    }
```

### Multi-Backend Orchestration

For high-availability setups, use multiple backends:

```json
{
  "mcpServers": {
    "knowledge-primary": {
      "command": "knowledge-mcp",
      "env": {
        "DATABASE_URL": "postgresql://user:pass@primary:5432/knowledge_db",
        "BACKEND_PRIORITY": "postgresql"
      }
    },
    "knowledge-backup": {
      "command": "knowledge-mcp",
      "env": {
        "SUPABASE_URL": "https://backup-project.supabase.co",
        "SUPABASE_KEY": "backup-key",
        "BACKEND_PRIORITY": "supabase"
      }
    }
  }
}
```

## 📊 Monitoring & Observability

### Health Monitoring

Monitor Knowledge-MCP health through Claude Code:

```
"Check the knowledge base health and report any issues"
```

### Performance Tracking

Track knowledge base growth and performance:

```
"Generate a monthly knowledge base usage report"
```

### Error Handling

Configure error reporting and recovery:

```json
{
  "env": {
    "SENTRY_DSN": "https://your-sentry-dsn",
    "SENTRY_ENVIRONMENT": "production"
  }
}
```

## 🔍 Debugging & Troubleshooting

### Enable Debug Logging

```json
{
  "env": {
    "LOG_LEVEL": "DEBUG"
  }
}
```

### Test MCP Connection

```bash
# Test MCP server directly
echo '{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}' | knowledge-mcp

# Expected: List of available tools
```

### Verify Database Connection

```
Tell Claude: "Test the knowledge base database connection"
```

### Common Issues

#### 1. MCP Server Not Found

**Problem**: Claude Code reports "knowledge-mcp command not found"

**Solution**:
```bash
# Ensure knowledge-mcp is in PATH
which knowledge-mcp

# If not found, install in environment used by Claude Code
pip install knowledge-mcp

# Or use full path in configuration
{
  "command": "/usr/local/bin/knowledge-mcp"
}
```

#### 2. Database Connection Errors

**Problem**: PostgreSQL connection failures

**Solution**:
```bash
# Test database connectivity
psql $DATABASE_URL -c "SELECT 1;"

# Check if database exists
createdb knowledge_db

# Verify credentials
```

#### 3. Permission Errors

**Problem**: Cannot write to data directory

**Solution**:
```bash
# Fix permissions
sudo chown -R $USER:$USER /opt/knowledge-data
chmod 755 /opt/knowledge-data
```

#### 4. Tool Timeouts

**Problem**: MCP tools timeout on large operations

**Solution**:
```json
{
  "mcpServers": {
    "knowledge-mcp": {
      "timeout": 60000,  // 60 seconds
      "env": {
        "BATCH_SIZE": "50"  // Reduce batch size
      }
    }
  }
}
```

## 📈 Performance Optimization

### Database Optimization

For PostgreSQL backends:

```sql
-- Optimize for knowledge workloads
CREATE INDEX CONCURRENTLY idx_kb_entries_topic ON kb_entries(topic);
CREATE INDEX CONCURRENTLY idx_kb_entries_tags ON kb_entries USING GIN(tags);
CREATE INDEX CONCURRENTLY idx_research_notes_tags ON research_notes USING GIN(tags);

-- Enable pg_trgm for fuzzy search
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX CONCURRENTLY idx_kb_content_trgm ON kb_entries USING GIN(content gin_trgm_ops);
```

### Batch Operations

Process large document sets efficiently:

```python
# Process documents in batches
async def batch_process_docs(doc_paths, batch_size=10):
    for i in range(0, len(doc_paths), batch_size):
        batch = doc_paths[i:i+batch_size]
        await kb_ingest_dir(dir_path=batch[0], recursive=False)
        # Small delay to prevent overwhelming the system
        await asyncio.sleep(1)
```

### Caching

Enable query result caching:

```json
{
  "env": {
    "ENABLE_QUERY_CACHE": "true",
    "CACHE_TTL_SECONDS": "300"
  }
}
```

## 🔐 Security Best Practices

### Environment Security

```bash
# Use environment files instead of hardcoded values
echo "DATABASE_URL=postgresql://..." > ~/.knowledge-mcp.env
chmod 600 ~/.knowledge-mcp.env

# Reference in MCP config
{
  "args": ["--env-file", "/home/user/.knowledge-mcp.env"]
}
```

### Database Security

```sql
-- Create restricted database user
CREATE USER knowledge_app WITH PASSWORD 'secure_password';
GRANT CONNECT ON DATABASE knowledge_db TO knowledge_app;
GRANT USAGE ON SCHEMA public TO knowledge_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO knowledge_app;
```

### Network Security

```bash
# Restrict database access to local only
# postgresql.conf
listen_addresses = 'localhost'

# pg_hba.conf
local   knowledge_db   knowledge_app   md5
```

## 🎯 Best Practices

### 1. Knowledge Organization

- Use consistent topic naming conventions
- Apply relevant tags for categorization
- Structure research projects hierarchically
- Regular cleanup of obsolete entries

### 2. Document Synchronization

- Set up automated sync for documentation directories
- Use appropriate chunking strategies per document type
- Monitor sync status regularly
- Maintain source-to-KB linking integrity

### 3. Research Workflows

- Link related notes, sources, and experiments
- Use descriptive titles and tags
- Maintain hypothesis-to-result traceability
- Regular progress summarization

### 4. Performance Management

- Monitor database size and query performance
- Use appropriate indexing strategies
- Implement batch processing for large operations
- Regular database maintenance

## 📚 Example Workflows

### Complete Research Project

```bash
# 1. Start new research project
claude-agent "Start research on 'Kubernetes autoscaling' with initial sources from CNCF documentation"

# 2. Add research notes throughout investigation
claude-agent "Add research note: tested HPA vs VPA, HPA more responsive for CPU spikes"

# 3. Log experiments
claude-agent "Log experiment: VPA memory predictions, 85% accuracy over 7 days"

# 4. Generate summary
claude-agent "Generate research summary for 'Kubernetes autoscaling' project"
```

### Documentation Management

```bash
# 1. Initial documentation sync
claude-agent "Sync all documentation from /project/docs to knowledge base"

# 2. Daily change detection
claude-agent "Check for documentation changes and update knowledge base"

# 3. Find source for KB entry
claude-agent "Show me the source document for knowledge entry kb_789456"

# 4. Cross-reference check
claude-agent "Find all KB entries that came from the API documentation"
```

## 📞 Support & Resources

- **Integration Issues**: [GitHub Issues](https://github.com/your-username/knowledge-mcp/issues)
- **Usage Questions**: [GitHub Discussions](https://github.com/your-username/knowledge-mcp/discussions)
- **Feature Requests**: [Feature Request Template](https://github.com/your-username/knowledge-mcp/issues/new?template=feature_request.md)
- **API Documentation**: [API Reference](API_REFERENCE.md)

---

**Next Steps**: [API Reference Documentation](API_REFERENCE.md)