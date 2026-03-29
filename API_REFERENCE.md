# API Reference

Complete documentation of all Knowledge-MCP tools and their parameters.

## 📚 Knowledge Base Tools

### `kb_add`

Add a new knowledge entry to the knowledge base.

**Usage:**
```python
kb_add(
    topic="api-design",
    title="REST Best Practices",
    content="REST APIs should use HTTP verbs appropriately...",
    tags=["api", "rest", "design"]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic` | string | ✅ | Category or subject area (e.g., "authentication", "database") |
| `title` | string | ✅ | Descriptive title for the knowledge entry |
| `content` | string | ✅ | Main content of the knowledge entry |
| `tags` | array[string] | ❌ | List of tags for categorization (default: []) |

**Response:**
```json
{
  "ok": true,
  "message": "Knowledge entry added successfully",
  "data": {
    "id": "kb_001234",
    "topic": "api-design",
    "title": "REST Best Practices",
    "created_at": "2025-03-29T10:30:00Z"
  }
}
```

**Error Conditions:**
- `INVALID_INPUT`: Missing required parameters or invalid data types
- `STORAGE_ERROR`: Database connection or storage issues
- `VALIDATION_ERROR`: Content exceeds size limits

---

### `kb_search`

Search the knowledge base with ranking by relevance.

**Usage:**
```python
kb_search(
    query="authentication patterns",
    topic="security",
    tags=["auth", "jwt"],
    limit=10
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | ✅ | Search query (searches title and content) |
| `topic` | string | ❌ | Filter by specific topic |
| `tags` | array[string] | ❌ | Filter by tags (AND logic) |
| `limit` | integer | ❌ | Maximum results to return (default: 20, max: 100) |

**Response:**
```json
{
  "ok": true,
  "message": "Found 3 knowledge entries",
  "data": {
    "query": "authentication patterns",
    "results": [
      {
        "id": "kb_001234",
        "topic": "security",
        "title": "JWT Authentication",
        "content_preview": "JSON Web Tokens provide stateless authentication...",
        "tags": ["auth", "jwt", "security"],
        "relevance_score": 0.95,
        "created_at": "2025-03-28T15:20:00Z"
      }
    ],
    "total_found": 3,
    "search_time_ms": 45
  }
}
```

**Search Features:**
- **Full-text search** across title and content
- **Tag filtering** with AND logic
- **Topic filtering** for category-specific searches
- **Relevance ranking** based on term frequency and position
- **Content preview** with search term highlighting

---

### `kb_get`

Retrieve a complete knowledge entry by ID.

**Usage:**
```python
kb_get(kb_id="kb_001234")
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `kb_id` | string | ✅ | Unique identifier of the knowledge entry |

**Response:**
```json
{
  "ok": true,
  "message": "Knowledge entry retrieved",
  "data": {
    "id": "kb_001234",
    "topic": "api-design",
    "title": "REST Best Practices",
    "content": "Full content of the knowledge entry...",
    "tags": ["api", "rest", "design"],
    "created_at": "2025-03-28T10:30:00Z",
    "updated_at": "2025-03-28T10:30:00Z",
    "source_doc": "/docs/api-design.md",
    "source_section": "## REST Principles",
    "line_range": [45, 78]
  }
}
```

**Additional Fields (if available):**
- `source_doc`: Path to source document (if ingested from file)
- `source_section`: Section header where content originated
- `line_range`: Line numbers in source document

---

### `kb_list`

List knowledge entries by topic with pagination.

**Usage:**
```python
kb_list(
    topic="database",
    limit=20,
    offset=0
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `topic` | string | ❌ | Filter by topic (if omitted, returns all topics) |
| `limit` | integer | ❌ | Number of entries to return (default: 20, max: 100) |
| `offset` | integer | ❌ | Number of entries to skip (for pagination) |

**Response:**
```json
{
  "ok": true,
  "message": "Found 15 knowledge entries",
  "data": {
    "topic": "database",
    "entries": [
      {
        "id": "kb_005678",
        "title": "PostgreSQL Indexing",
        "content_preview": "B-tree indexes are the default...",
        "tags": ["postgres", "performance"],
        "created_at": "2025-03-27T14:15:00Z"
      }
    ],
    "pagination": {
      "limit": 20,
      "offset": 0,
      "total": 15,
      "has_more": false
    }
  }
}
```

## 🔬 Research Tools

### `research_add_note`

Add a research note with optional source linking.

**Usage:**
```python
research_add_note(
    title="Microservices vs Monoliths",
    content="Comparing architectural approaches...",
    tags=["architecture", "microservices"]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | ✅ | Research note title |
| `content` | string | ✅ | Note content (supports Markdown) |
| `tags` | array[string] | ❌ | Categorization tags |

**Response:**
```json
{
  "ok": true,
  "message": "Research note added successfully",
  "data": {
    "id": "rn_789012",
    "title": "Microservices vs Monoliths",
    "created_at": "2025-03-29T11:00:00Z",
    "tags": ["architecture", "microservices"]
  }
}
```

---

### `research_list_notes`

List research notes with filtering and pagination.

**Usage:**
```python
research_list_notes(
    tags=["architecture"],
    limit=10,
    offset=0
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tags` | array[string] | ❌ | Filter by tags (AND logic) |
| `limit` | integer | ❌ | Results limit (default: 20, max: 100) |
| `offset` | integer | ❌ | Pagination offset |

**Response:**
```json
{
  "ok": true,
  "message": "Found 5 research notes",
  "data": {
    "notes": [
      {
        "id": "rn_789012",
        "title": "Microservices vs Monoliths",
        "content_preview": "Comparing architectural approaches...",
        "tags": ["architecture", "microservices"],
        "created_at": "2025-03-29T11:00:00Z"
      }
    ],
    "pagination": {
      "total": 5,
      "limit": 10,
      "offset": 0,
      "has_more": false
    }
  }
}
```

---

### `research_get_note`

Retrieve complete research note by ID.

**Usage:**
```python
research_get_note(note_id="rn_789012")
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `note_id` | string | ✅ | Research note ID |

**Response:**
```json
{
  "ok": true,
  "message": "Research note retrieved",
  "data": {
    "id": "rn_789012",
    "title": "Microservices vs Monoliths",
    "content": "Full research note content...",
    "tags": ["architecture", "microservices"],
    "created_at": "2025-03-29T11:00:00Z",
    "updated_at": "2025-03-29T11:30:00Z",
    "linked_sources": ["rs_345678"],
    "linked_experiments": ["re_901234"]
  }
}
```

---

### `research_add_source`

Add a research source (URL, paper, book, etc.).

**Usage:**
```python
research_add_source(
    url="https://martinfowler.com/articles/microservices.html",
    title="Microservices by Martin Fowler",
    summary="Comprehensive overview of microservices architecture...",
    tags=["architecture", "fowler"]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `url` | string | ✅ | Source URL or identifier |
| `title` | string | ✅ | Source title |
| `summary` | string | ❌ | Brief summary or description |
| `tags` | array[string] | ❌ | Categorization tags |

**Response:**
```json
{
  "ok": true,
  "message": "Research source added successfully",
  "data": {
    "id": "rs_345678",
    "url": "https://martinfowler.com/articles/microservices.html",
    "title": "Microservices by Martin Fowler",
    "created_at": "2025-03-29T11:15:00Z"
  }
}
```

---

### `research_list_sources`

List research sources with filtering.

**Usage:**
```python
research_list_sources(
    tags=["architecture"],
    limit=20
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tags` | array[string] | ❌ | Filter by tags |
| `limit` | integer | ❌ | Results limit (default: 20) |
| `offset` | integer | ❌ | Pagination offset |

**Response:**
```json
{
  "ok": true,
  "message": "Found 3 research sources",
  "data": {
    "sources": [
      {
        "id": "rs_345678",
        "url": "https://martinfowler.com/articles/microservices.html",
        "title": "Microservices by Martin Fowler",
        "summary": "Comprehensive overview...",
        "tags": ["architecture", "fowler"],
        "created_at": "2025-03-29T11:15:00Z"
      }
    ],
    "pagination": {
      "total": 3,
      "limit": 20,
      "offset": 0
    }
  }
}
```

---

### `research_log_experiment`

Log a research experiment with hypothesis and results.

**Usage:**
```python
research_log_experiment(
    title="Redis vs Memcached Performance",
    hypothesis="Redis will outperform Memcached for our workload",
    methodology="Load test with 1000 concurrent connections...",
    results="Redis: 15ms avg latency, Memcached: 18ms avg latency",
    conclusions="Redis performed 17% better under high concurrency",
    tags=["performance", "caching"]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | ✅ | Experiment title |
| `hypothesis` | string | ❌ | Initial hypothesis or expectation |
| `methodology` | string | ❌ | How the experiment was conducted |
| `results` | string | ❌ | Observed results and data |
| `conclusions` | string | ❌ | Analysis and conclusions |
| `tags` | array[string] | ❌ | Categorization tags |

**Response:**
```json
{
  "ok": true,
  "message": "Research experiment logged successfully",
  "data": {
    "id": "re_901234",
    "title": "Redis vs Memcached Performance",
    "created_at": "2025-03-29T12:00:00Z",
    "tags": ["performance", "caching"]
  }
}
```

---

### `research_list_experiments`

List research experiments with filtering.

**Usage:**
```python
research_list_experiments(
    tags=["performance"],
    limit=10
)
```

**Parameters & Response**: Similar structure to `research_list_notes`

---

### `research_link_source_to_experiment`

Link a research source to an experiment.

**Usage:**
```python
research_link_source_to_experiment(
    source_id="rs_345678",
    experiment_id="re_901234"
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_id` | string | ✅ | Research source ID |
| `experiment_id` | string | ✅ | Research experiment ID |

**Response:**
```json
{
  "ok": true,
  "message": "Source linked to experiment successfully",
  "data": {
    "source_id": "rs_345678",
    "experiment_id": "re_901234",
    "linked_at": "2025-03-29T12:30:00Z"
  }
}
```

## 📖 Journal Tools

### `journal_append`

Add an entry to the decision journal.

**Usage:**
```python
journal_append(
    content="Decided to use PostgreSQL over MongoDB for better ACID guarantees",
    tags=["database", "decision"]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | string | ✅ | Journal entry content |
| `tags` | array[string] | ❌ | Categorization tags |

**Response:**
```json
{
  "ok": true,
  "message": "Journal entry added successfully",
  "data": {
    "id": "je_567890",
    "content_preview": "Decided to use PostgreSQL over MongoDB...",
    "created_at": "2025-03-29T13:00:00Z",
    "tags": ["database", "decision"]
  }
}
```

---

### `journal_list`

List journal entries with filtering and pagination.

**Usage:**
```python
journal_list(
    tags=["decision"],
    limit=20,
    offset=0
)
```

**Parameters & Response**: Similar structure to other list endpoints

---

### `journal_get`

Retrieve a complete journal entry by ID.

**Usage:**
```python
journal_get(entry_id="je_567890")
```

**Parameters & Response**: Similar structure to other get endpoints

---

### `snapshot_config`

Create a snapshot of configuration files under a label.

**Usage:**
```python
snapshot_config(
    label="v2.1-release",
    config_paths=[
        "/etc/app/config.yaml",
        "/etc/nginx/nginx.conf",
        "/etc/postgresql/postgresql.conf"
    ]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `label` | string | ✅ | Snapshot label (e.g., version, release, date) |
| `config_paths` | array[string] | ✅ | List of configuration file paths |

**Response:**
```json
{
  "ok": true,
  "message": "Configuration snapshot created: v2.1-release",
  "data": {
    "snapshot_id": "cs_123456",
    "label": "v2.1-release",
    "files_captured": 3,
    "total_size_bytes": 15680,
    "created_at": "2025-03-29T14:00:00Z"
  }
}
```

## 🔍 Document Intelligence Tools

### `kb_ingest_doc`

Ingest a single document into the knowledge base with change detection.

**Usage:**
```python
kb_ingest_doc(
    doc_path="/docs/api-guide.md",
    strategy="chunked",
    chunk_size=2000,
    tags=["api", "documentation"],
    overwrite=False
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `doc_path` | string | ✅ | Absolute path to document |
| `strategy` | string | ❌ | Ingestion strategy: "full", "chunked", "summary" (default: "chunked") |
| `chunk_size` | integer | ❌ | Max tokens per chunk (default: 2000) |
| `tags` | array[string] | ❌ | Additional tags to apply |
| `overwrite` | boolean | ❌ | Force re-ingest even if unchanged (default: false) |

**Response:**
```json
{
  "ok": true,
  "message": "Ingested api-guide.md: 5 KB entries created",
  "data": {
    "doc_path": "/docs/api-guide.md",
    "kb_entries_created": 5,
    "kb_ids": ["kb_001", "kb_002", "kb_003", "kb_004", "kb_005"],
    "doc_hash": "sha256:a1b2c3d4e5f6...",
    "status": "created",
    "strategy": "chunked",
    "processing_time_ms": 1250
  }
}
```

**Ingestion Strategies:**

| Strategy | Description | Best For |
|----------|-------------|----------|
| `full` | Entire document as single KB entry | Short docs (<3000 tokens) |
| `chunked` | Split by headers and token limits | Long docs, structured content |
| `summary` | AI-generated summary (planned) | Very long docs, overviews |

---

### `kb_ingest_dir`

Batch ingest all documents in a directory.

**Usage:**
```python
kb_ingest_dir(
    dir_path="/docs",
    pattern="*.md",
    strategy="chunked",
    recursive=True,
    exclude_patterns=["**/node_modules/**", "**/.git/**"]
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `dir_path` | string | ✅ | Directory path to scan |
| `pattern` | string | ❌ | File pattern to match (default: "*.md") |
| `strategy` | string | ❌ | Ingestion strategy (default: "chunked") |
| `recursive` | boolean | ❌ | Scan subdirectories (default: true) |
| `exclude_patterns` | array[string] | ❌ | Glob patterns to exclude |

**Response:**
```json
{
  "ok": true,
  "message": "Processed 35 files: 12 created, 8 updated, 15 unchanged",
  "data": {
    "dir_path": "/docs",
    "files_processed": 35,
    "files_created": 12,
    "files_updated": 8,
    "files_unchanged": 15,
    "files_errored": 0,
    "total_kb_entries": 127,
    "processing_time_ms": 8750,
    "errors": []
  }
}
```

---

### `kb_sync_status`

Check synchronization status for documents in a directory.

**Usage:**
```python
kb_sync_status(
    dir_path="/docs",
    pattern="*.md"
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `dir_path` | string | ✅ | Directory to check |
| `pattern` | string | ❌ | File pattern (default: "*.md") |

**Response:**
```json
{
  "ok": true,
  "message": "Sync status: 20 synced, 8 modified, 7 new, 3 orphaned",
  "data": {
    "dir_path": "/docs",
    "total_docs": 35,
    "synced": 20,
    "modified": 8,
    "new": 7,
    "orphaned_kb_entries": 3,
    "details": [
      {
        "doc_path": "/docs/api-guide.md",
        "status": "modified",
        "last_synced": "2025-03-28T10:30:00Z",
        "doc_modified": "2025-03-29T09:15:00Z",
        "kb_ids": ["kb_001", "kb_002", "kb_003"]
      },
      {
        "doc_path": "/docs/installation.md",
        "status": "new",
        "doc_modified": "2025-03-29T11:00:00Z",
        "kb_ids": []
      }
    ]
  }
}
```

**Status Types:**
- `synced`: Document unchanged since last sync
- `modified`: Document changed since last sync
- `new`: Document not yet synced
- `orphaned`: KB entries exist but source document is missing

---

### `kb_link_to_source`

Get source document information for a KB entry.

**Usage:**
```python
kb_link_to_source(kb_id="kb_001234")
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `kb_id` | string | ✅ | Knowledge base entry ID |

**Response:**
```json
{
  "ok": true,
  "message": "Source: api-guide.md, section: Authentication",
  "data": {
    "kb_id": "kb_001234",
    "source_doc": "/docs/api-guide.md",
    "source_section": "## Authentication",
    "line_range": [89, 156],
    "last_synced": "2025-03-29T10:30:00Z",
    "doc_exists": true,
    "doc_modified": "2025-03-29T09:15:00Z"
  }
}
```

## 🔧 Advanced Tools

### `multi_search`

Search across all knowledge systems (KB, Research, Journal).

**Usage:**
```python
multi_search(
    query="authentication implementation",
    include_kb=True,
    include_research=True,
    include_journal=True,
    tags=["auth", "security"],
    limit=20
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | string | ✅ | Search query |
| `include_kb` | boolean | ❌ | Search knowledge base (default: true) |
| `include_research` | boolean | ❌ | Search research notes (default: true) |
| `include_journal` | boolean | ❌ | Search journal entries (default: true) |
| `tags` | array[string] | ❌ | Filter by tags across all systems |
| `limit` | integer | ❌ | Total results limit (default: 20) |

**Response:**
```json
{
  "ok": true,
  "message": "Found 15 results across KB, Research, and Journal",
  "data": {
    "query": "authentication implementation",
    "results": [
      {
        "type": "kb_entry",
        "id": "kb_001234",
        "title": "JWT Implementation Guide",
        "content_preview": "Implementation details for JSON Web Tokens...",
        "relevance_score": 0.92,
        "source": "knowledge_base"
      },
      {
        "type": "research_note",
        "id": "rn_567890",
        "title": "Auth0 vs Custom JWT",
        "content_preview": "Comparison of authentication approaches...",
        "relevance_score": 0.87,
        "source": "research"
      },
      {
        "type": "journal_entry",
        "id": "je_789012",
        "content_preview": "Decided to implement JWT with refresh tokens...",
        "relevance_score": 0.75,
        "source": "journal"
      }
    ],
    "breakdown": {
      "kb_entries": 8,
      "research_notes": 4,
      "journal_entries": 3
    },
    "search_time_ms": 67
  }
}
```

### `deduplicate_results`

Remove duplicate content across different knowledge systems.

**Usage:**
```python
deduplicate_results(
    similarity_threshold=0.8,
    dry_run=True
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `similarity_threshold` | float | ❌ | Similarity threshold (0.0-1.0, default: 0.8) |
| `dry_run` | boolean | ❌ | Preview changes without executing (default: true) |

## 📊 MCP Index Tools

### `mcp_index_search`

Search the MCP server index for tools and servers.

**Usage:**
```python
mcp_index_search(
    query="file operations",
    server_filter="filesystem"
)
```

### `mcp_index_get_tool`

Get detailed information about an MCP tool.

### `mcp_index_get_server`

Get information about an MCP server and its tools.

### `mcp_index_rebuild`

Rebuild the MCP index by scanning configured servers.

## 🚨 Error Handling

### Standard Error Response Format

```json
{
  "ok": false,
  "error": "ERROR_CODE",
  "message": "Human-readable error description",
  "data": {
    "details": "Additional error context",
    "retry_after": 30,
    "suggested_action": "Check database connection"
  }
}
```

### Common Error Codes

| Code | Description | Typical Cause |
|------|-------------|---------------|
| `INVALID_INPUT` | Invalid parameters | Missing required fields, wrong types |
| `NOT_FOUND` | Resource not found | Invalid ID, deleted entry |
| `STORAGE_ERROR` | Database/storage issue | Connection failed, disk full |
| `VALIDATION_ERROR` | Data validation failed | Content too large, invalid format |
| `PERMISSION_DENIED` | Access denied | File permissions, authentication |
| `RATE_LIMITED` | Too many requests | Exceeded rate limits |
| `INTERNAL_ERROR` | Server error | Unexpected exception |

### Retry Logic

Most tools support automatic retry with exponential backoff:

```json
{
  "error": "TEMPORARY_ERROR",
  "data": {
    "retry_after": 30,
    "max_retries": 3,
    "current_attempt": 1
  }
}
```

## 📏 Limits & Constraints

### Size Limits

| Resource | Limit | Configurable |
|----------|-------|--------------|
| KB Entry Content | 50,000 characters | Yes (MAX_CONTENT_SIZE) |
| KB Entry Title | 200 characters | No |
| Research Note Content | 100,000 characters | Yes |
| Journal Entry Content | 10,000 characters | Yes |
| Tags per Entry | 20 tags | Yes |
| Tag Length | 50 characters | No |
| Batch Operations | 100 items | Yes (BATCH_SIZE) |

### Rate Limits

| Operation | Default Limit | Scope |
|-----------|---------------|-------|
| Search Operations | 60/minute | Per MCP session |
| Write Operations | 120/minute | Per MCP session |
| Batch Ingestion | 10 concurrent | Server-wide |
| File Operations | 1000/hour | Per directory |

### Performance Guidelines

| Operation | Expected Latency | Notes |
|-----------|------------------|-------|
| `kb_search` | <100ms | PostgreSQL backend |
| `kb_add` | <50ms | Single entry |
| `kb_ingest_doc` | 200ms-2s | Depends on document size |
| `multi_search` | <200ms | Across all systems |
| `research_add_note` | <50ms | Single note |

---

**Related Documentation**:
- [Installation Guide](INSTALLATION.md) - Setup instructions
- [Claude Integration](CLAUDE_INTEGRATION.md) - Usage with Claude Code
- [Architecture Guide](ARCHITECTURE.md) - Technical implementation details