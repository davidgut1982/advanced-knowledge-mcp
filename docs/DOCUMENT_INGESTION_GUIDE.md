# Document Ingestion Guide

## Overview

The knowledge-mcp server (v1.3) includes 4 new tools for ingesting markdown documents into the knowledge base with automatic change detection and bidirectional linking.

## New Tools

### 1. `kb_ingest_doc` - Ingest Single Document

Ingest a markdown file into KB with change detection.

**Example:**
```json
{
  "name": "kb_ingest_doc",
  "arguments": {
    "doc_path": "/srv/latvian_xtts/docs/AUDIO_PIPELINE.md",
    "strategy": "chunked",
    "chunk_size": 2000,
    "tags": ["xtts", "audio"],
    "overwrite": false
  }
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Ingested AUDIO_PIPELINE.md: 5 KB entries created",
  "data": {
    "doc_path": "/srv/latvian_xtts/docs/AUDIO_PIPELINE.md",
    "kb_entries_created": 5,
    "kb_ids": ["kb_a1b2c3", "kb_d4e5f6", "kb_g7h8i9", "kb_j0k1l2", "kb_m3n4o5"],
    "doc_hash": "sha256:a1b2c3d4...",
    "status": "created"
  }
}
```

**Strategies:**
- `full` - One KB entry for entire document (best for short docs <3000 tokens)
- `chunked` - Split by headers and/or token count (default, best for long docs)
- `summary` - GPT-generated summary (not yet implemented)

### 2. `kb_ingest_dir` - Batch Ingest Directory

Ingest all markdown files in a directory.

**Example:**
```json
{
  "name": "kb_ingest_dir",
  "arguments": {
    "dir_path": "/srv/latvian_xtts/docs",
    "pattern": "*.md",
    "strategy": "chunked",
    "recursive": true,
    "exclude_patterns": ["**/node_modules/**", "**/.git/**"]
  }
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Processed 35 files: 12 created, 8 updated, 15 unchanged",
  "data": {
    "processed": 35,
    "created": 12,
    "updated": 8,
    "unchanged": 15,
    "errors": []
  }
}
```

### 3. `kb_sync_status` - Check Sync State

Check which documents have changed since last sync.

**Example:**
```json
{
  "name": "kb_sync_status",
  "arguments": {
    "dir_path": "/srv/latvian_xtts/docs"
  }
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Sync status: 20 synced, 8 modified, 7 new, 3 orphaned",
  "data": {
    "total_docs": 35,
    "synced": 20,
    "modified": 8,
    "new": 7,
    "orphaned_kb_entries": 3,
    "details": [
      {
        "doc_path": "/srv/latvian_xtts/docs/AUDIO_PIPELINE.md",
        "status": "modified",
        "last_synced": "2025-12-07T10:30:00Z",
        "doc_modified": "2025-12-08T09:15:00Z",
        "kb_ids": ["kb_001", "kb_002", "kb_003"]
      }
    ]
  }
}
```

### 4. `kb_link_to_source` - Get Source Document

Get the source document for a KB entry.

**Example:**
```json
{
  "name": "kb_link_to_source",
  "arguments": {
    "kb_id": "kb_00123"
  }
}
```

**Response:**
```json
{
  "ok": true,
  "message": "Source: AUDIO_PIPELINE.md",
  "data": {
    "kb_id": "kb_00123",
    "source_doc": "/srv/latvian_xtts/docs/AUDIO_PIPELINE.md",
    "source_section": "## Stage 2: Whisper Extraction",
    "line_range": [189, 218],
    "last_synced": "2025-12-08T10:30:00Z",
    "doc_exists": true
  }
}
```

## Frontmatter Support

Documents can include YAML frontmatter for metadata:

```markdown
---
topic: xtts
tags: [audio, pipeline, v2]
---

# Audio Processing Pipeline
...
```

## Change Detection

The system uses SHA-256 hashing to detect document changes:
1. On first ingest: Compute hash, store sync record
2. On subsequent ingests: Compare hashes
3. If unchanged: Return early with `status: "unchanged"`
4. If changed: Re-ingest and update KB entries

## Bidirectional Linking

Each KB entry includes:
- `source_doc` - Absolute path to source file
- `source_section` - Header that chunk came from
- `line_range` - Line numbers in source file

The `kb_doc_sync` table maps:
- `doc_path` → `kb_ids[]`

This enables both:
- "Show me the source for this KB entry" → `kb_link_to_source`
- "What KB entries came from this doc?" → Query `kb_doc_sync`

## Database Schema

### kb_doc_sync Table

```sql
CREATE TABLE kb_doc_sync (
  id UUID PRIMARY KEY,
  doc_path TEXT UNIQUE NOT NULL,
  doc_hash TEXT NOT NULL,
  kb_ids JSONB NOT NULL DEFAULT '[]',
  last_synced_at TIMESTAMPTZ NOT NULL,
  last_modified_at TIMESTAMPTZ NOT NULL,
  strategy TEXT DEFAULT 'chunked',
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
```

### kb_entries Additions

New columns:
- `source_doc TEXT` - Path to source document
- `source_section TEXT` - Section header (e.g., "## Introduction")
- `line_range INTEGER[]` - [start_line, end_line]

## Usage Workflow

### Initial Sync

```bash
# 1. Check what needs syncing
kb_sync_status(dir_path="/srv/latvian_xtts/docs")

# 2. Ingest all new/modified docs
kb_ingest_dir(
  dir_path="/srv/latvian_xtts/docs",
  strategy="chunked",
  recursive=true
)
```

### Incremental Updates

```bash
# 1. Check for changes
status = kb_sync_status(dir_path="/srv/latvian_xtts/docs")

# 2. If status.modified > 0 or status.new > 0:
kb_ingest_dir(dir_path="/srv/latvian_xtts/docs")
```

### Automated Sync (via orchestrator-mcp)

Use the `sync_docs_to_kb` recipe:

```json
{
  "name": "sync_docs_to_kb",
  "steps": [
    {
      "tool": "knowledge-mcp.kb_sync_status",
      "params": { "dir_path": "/srv/latvian_xtts/docs" }
    },
    {
      "tool": "conditional",
      "condition": "sync_status.modified > 0 OR sync_status.new > 0",
      "then": [
        {
          "tool": "knowledge-mcp.kb_ingest_dir",
          "params": { "dir_path": "/srv/latvian_xtts/docs" }
        }
      ]
    }
  ]
}
```

## Implementation Details

### DocumentProcessor Class

Located in `src/knowledge_mcp/doc_processor.py`:

- `read_document()` - Extract frontmatter and content
- `compute_hash()` - SHA-256 hashing
- `count_tokens()` - Token counting via tiktoken
- `chunk_by_sections()` - Split by headers
- `extract_topic_from_path()` - Infer topic from path
- `generate_title()` - Extract title from H1 or filename

### Handlers

All handlers in `src/knowledge_mcp/server.py`:

- `handle_kb_ingest_doc()` - Lines 967-1096
- `handle_kb_ingest_dir()` - Lines 1099-1174 (parallel processing)
- `handle_kb_sync_status()` - Lines 1177-1251
- `handle_kb_link_to_source()` - Lines 1254-1299

## Migration

Run migration to create `kb_doc_sync` table:

```bash
# Via Supabase Dashboard
# Go to SQL Editor → Run migrations/002_kb_doc_sync.sql

# Or via psql
psql $DATABASE_URL -f migrations/002_kb_doc_sync.sql
```

## Testing

1. Create test document:

```bash
cat > /tmp/test_doc.md <<EOF
---
topic: testing
tags: [test, ingestion]
---

# Test Document

## Section 1
Content here.

## Section 2
More content.
EOF
```

2. Ingest via Claude Code:

```
Tell Claude: "Ingest /tmp/test_doc.md into the knowledge base using chunked strategy"
```

3. Check sync status:

```
Tell Claude: "Check sync status for /tmp directory"
```

4. Get source reference:

```
Tell Claude: "Show me the source document for KB entry kb_xxxxx"
```

## Benefits

- **Single Source of Truth**: Docs remain in version control
- **Automatic Sync Detection**: SHA-256 hashing prevents redundant processing
- **Bidirectional Linking**: Navigate from KB → source or source → KB
- **Flexible Strategies**: Choose ingestion strategy per document
- **Parallel Processing**: `kb_ingest_dir` processes multiple docs concurrently
- **Graceful Error Handling**: One doc failure doesn't stop batch processing

## Spec Reference

Implementation follows **LATVIAN_LAB_MCP_MASTER_SPEC_v1.3 § 4.1.5**
