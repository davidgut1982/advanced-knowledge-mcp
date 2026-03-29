#!/usr/bin/env python3
"""
Knowledge-MCP Usage Examples

This file demonstrates common usage patterns for Knowledge-MCP tools.
These examples can be used directly with Claude Code or adapted for other MCP clients.
"""

import asyncio
import json
from pathlib import Path

# Example data for demonstrations
EXAMPLE_API_DOC = """
---
topic: api-design
tags: [rest, api, authentication]
---

# Authentication API

## Overview

Our API uses JWT tokens for authentication with refresh token support.

## Endpoints

### POST /auth/login

Authenticate user and receive JWT tokens.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "secure_password"
}
```

**Response:**
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...",
  "expires_in": 3600
}
```

### POST /auth/refresh

Refresh access token using refresh token.
"""

async def basic_knowledge_operations():
    """Demonstrate basic knowledge base operations."""
    print("=== Basic Knowledge Operations ===\n")

    # Example 1: Adding knowledge entry
    print("1. Adding knowledge entry...")
    kb_add_example = {
        "tool": "kb_add",
        "arguments": {
            "topic": "api-design",
            "title": "JWT Authentication Flow",
            "content": "JWT tokens provide stateless authentication. Include refresh tokens for security.",
            "tags": ["jwt", "authentication", "security"]
        }
    }
    print(f"Tool call: {json.dumps(kb_add_example, indent=2)}")
    print("Expected: Knowledge entry created with ID")
    print()

    # Example 2: Searching knowledge base
    print("2. Searching knowledge base...")
    kb_search_example = {
        "tool": "kb_search",
        "arguments": {
            "query": "authentication patterns",
            "topic": "api-design",
            "tags": ["authentication"],
            "limit": 10
        }
    }
    print(f"Tool call: {json.dumps(kb_search_example, indent=2)}")
    print("Expected: List of matching entries with relevance scores")
    print()

    # Example 3: Getting specific entry
    print("3. Getting specific knowledge entry...")
    kb_get_example = {
        "tool": "kb_get",
        "arguments": {
            "kb_id": "kb_001234"
        }
    }
    print(f"Tool call: {json.dumps(kb_get_example, indent=2)}")
    print("Expected: Complete entry with metadata and source information")
    print()

async def document_ingestion_examples():
    """Demonstrate document ingestion capabilities."""
    print("=== Document Ingestion Examples ===\n")

    # Example 1: Single document ingestion
    print("1. Ingesting single document...")
    ingest_doc_example = {
        "tool": "kb_ingest_doc",
        "arguments": {
            "doc_path": "/docs/api-authentication.md",
            "strategy": "chunked",
            "chunk_size": 2000,
            "tags": ["api", "authentication"],
            "overwrite": False
        }
    }
    print(f"Tool call: {json.dumps(ingest_doc_example, indent=2)}")
    print("Expected: Document processed into multiple KB entries with bidirectional linking")
    print()

    # Example 2: Directory ingestion
    print("2. Batch ingesting directory...")
    ingest_dir_example = {
        "tool": "kb_ingest_dir",
        "arguments": {
            "dir_path": "/project/docs",
            "pattern": "*.md",
            "strategy": "chunked",
            "recursive": True,
            "exclude_patterns": ["**/drafts/**", "**/node_modules/**"]
        }
    }
    print(f"Tool call: {json.dumps(ingest_dir_example, indent=2)}")
    print("Expected: All markdown files processed with change detection")
    print()

    # Example 3: Checking sync status
    print("3. Checking sync status...")
    sync_status_example = {
        "tool": "kb_sync_status",
        "arguments": {
            "dir_path": "/project/docs"
        }
    }
    print(f"Tool call: {json.dumps(sync_status_example, indent=2)}")
    print("Expected: Report of changed, new, and orphaned documents")
    print()

async def research_workflow_examples():
    """Demonstrate research workflow capabilities."""
    print("=== Research Workflow Examples ===\n")

    # Example 1: Adding research note
    print("1. Adding research note...")
    research_note_example = {
        "tool": "research_add_note",
        "arguments": {
            "title": "Microservices vs Monoliths Comparison",
            "content": "Research comparing architectural approaches for scalability and maintainability.",
            "tags": ["architecture", "microservices", "scalability"]
        }
    }
    print(f"Tool call: {json.dumps(research_note_example, indent=2)}")
    print("Expected: Research note created with ID")
    print()

    # Example 2: Adding research source
    print("2. Adding research source...")
    research_source_example = {
        "tool": "research_add_source",
        "arguments": {
            "url": "https://martinfowler.com/articles/microservices.html",
            "title": "Microservices by Martin Fowler",
            "summary": "Comprehensive overview of microservices architecture patterns and practices.",
            "tags": ["microservices", "fowler", "architecture"]
        }
    }
    print(f"Tool call: {json.dumps(research_source_example, indent=2)}")
    print("Expected: Source added with unique ID")
    print()

    # Example 3: Logging experiment
    print("3. Logging research experiment...")
    experiment_example = {
        "tool": "research_log_experiment",
        "arguments": {
            "title": "API Gateway Performance Comparison",
            "hypothesis": "Kong will outperform Nginx for our microservices architecture",
            "methodology": "Load test with 1000 concurrent requests measuring latency and throughput",
            "results": "Kong: 50ms avg latency, 2000 RPS. Nginx: 45ms avg latency, 2200 RPS",
            "conclusions": "Nginx performed better but Kong offers better feature set",
            "tags": ["performance", "api-gateway", "kong", "nginx"]
        }
    }
    print(f"Tool call: {json.dumps(experiment_example, indent=2)}")
    print("Expected: Experiment logged with structured data")
    print()

    # Example 4: Linking source to experiment
    print("4. Linking source to experiment...")
    link_example = {
        "tool": "research_link_source_to_experiment",
        "arguments": {
            "source_id": "rs_345678",
            "experiment_id": "re_901234"
        }
    }
    print(f"Tool call: {json.dumps(link_example, indent=2)}")
    print("Expected: Bidirectional link established")
    print()

async def journal_examples():
    """Demonstrate journal and configuration management."""
    print("=== Journal Examples ===\n")

    # Example 1: Adding journal entry
    print("1. Adding decision journal entry...")
    journal_example = {
        "tool": "journal_append",
        "arguments": {
            "content": "Decided to use PostgreSQL over MongoDB for better ACID guarantees and complex query support. Migration planned for next sprint.",
            "tags": ["decision", "database", "postgresql"]
        }
    }
    print(f"Tool call: {json.dumps(journal_example, indent=2)}")
    print("Expected: Journal entry created with timestamp")
    print()

    # Example 2: Configuration snapshot
    print("2. Creating configuration snapshot...")
    snapshot_example = {
        "tool": "snapshot_config",
        "arguments": {
            "label": "v2.1-release",
            "config_paths": [
                "/etc/app/config.yaml",
                "/etc/nginx/nginx.conf",
                "/etc/postgresql/postgresql.conf"
            ]
        }
    }
    print(f"Tool call: {json.dumps(snapshot_example, indent=2)}")
    print("Expected: Configuration files captured with checksums")
    print()

    # Example 3: Listing journal entries
    print("3. Listing recent journal entries...")
    journal_list_example = {
        "tool": "journal_list",
        "arguments": {
            "days": 30,
            "tags": ["decision"],
            "limit": 10
        }
    }
    print(f"Tool call: {json.dumps(journal_list_example, indent=2)}")
    print("Expected: Recent decision entries")
    print()

async def advanced_search_examples():
    """Demonstrate advanced search capabilities."""
    print("=== Advanced Search Examples ===\n")

    # Example 1: Multi-system search
    print("1. Cross-system search...")
    multi_search_example = {
        "tool": "multi_search",
        "arguments": {
            "query": "authentication implementation",
            "include_kb": True,
            "include_research": True,
            "include_journal": True,
            "tags": ["authentication", "security"],
            "limit": 20
        }
    }
    print(f"Tool call: {json.dumps(multi_search_example, indent=2)}")
    print("Expected: Combined results from KB, research notes, and journal")
    print()

    # Example 2: Source linking
    print("2. Finding source for KB entry...")
    source_link_example = {
        "tool": "kb_link_to_source",
        "arguments": {
            "kb_id": "kb_001234"
        }
    }
    print(f"Tool call: {json.dumps(source_link_example, indent=2)}")
    print("Expected: Source document path, section, and line numbers")
    print()

def create_sample_documents():
    """Create sample documents for testing."""
    print("=== Creating Sample Documents ===\n")

    # Create sample directory
    docs_dir = Path("/tmp/knowledge-mcp-examples")
    docs_dir.mkdir(exist_ok=True)

    # API documentation
    api_doc = docs_dir / "api-authentication.md"
    with open(api_doc, "w") as f:
        f.write(EXAMPLE_API_DOC)

    # Architecture decision record
    adr_doc = docs_dir / "adr-001-database-choice.md"
    adr_content = """
---
topic: architecture
tags: [adr, database, decision]
---

# ADR-001: Database Choice for User Data

## Status
Accepted

## Context
We need to choose a database for storing user data and application state.

## Decision
Use PostgreSQL as the primary database.

## Consequences
- ACID compliance for critical operations
- Complex query support
- Mature ecosystem and tooling
- Learning curve for NoSQL-familiar developers
"""
    with open(adr_doc, "w") as f:
        f.write(adr_content)

    # Performance guide
    perf_doc = docs_dir / "performance-optimization.md"
    perf_content = """
---
topic: performance
tags: [optimization, caching, database]
---

# Performance Optimization Guide

## Database Optimization

### Indexing
- Create indexes on frequently queried columns
- Use partial indexes for filtered queries
- Monitor query performance with EXPLAIN

### Connection Pooling
- Use pgBouncer for connection management
- Configure pool size based on workload
- Monitor connection usage

## Caching Strategies

### Redis Caching
- Cache frequently accessed data
- Use appropriate TTL values
- Implement cache invalidation
"""
    with open(perf_doc, "w") as f:
        f.write(perf_content)

    print(f"Sample documents created in: {docs_dir}")
    print("Files:")
    for doc in docs_dir.glob("*.md"):
        print(f"  - {doc.name}")
    print()

async def complete_workflow_example():
    """Demonstrate complete knowledge management workflow."""
    print("=== Complete Workflow Example ===\n")

    print("Scenario: Setting up authentication system documentation and research")
    print()

    # Step 1: Ingest existing documentation
    print("Step 1: Ingest existing documentation...")
    workflow_steps = [
        {
            "description": "Check what needs syncing",
            "tool": "kb_sync_status",
            "arguments": {"dir_path": "/tmp/knowledge-mcp-examples"}
        },
        {
            "description": "Ingest all new documentation",
            "tool": "kb_ingest_dir",
            "arguments": {"dir_path": "/tmp/knowledge-mcp-examples", "strategy": "chunked"}
        }
    ]

    for step in workflow_steps:
        print(f"  {step['description']}")
        print(f"  Tool: {step['tool']}")
        print(f"  Args: {json.dumps(step['arguments'], indent=4)}")
        print()

    # Step 2: Add research context
    print("Step 2: Add research context...")
    research_steps = [
        {
            "description": "Add research note about auth patterns",
            "tool": "research_add_note",
            "arguments": {
                "title": "Authentication Pattern Research",
                "content": "Researching JWT vs session-based authentication for our API",
                "tags": ["authentication", "research"]
            }
        },
        {
            "description": "Add relevant source material",
            "tool": "research_add_source",
            "arguments": {
                "url": "https://jwt.io/introduction/",
                "title": "JWT Introduction",
                "tags": ["jwt", "authentication"]
            }
        }
    ]

    for step in research_steps:
        print(f"  {step['description']}")
        print(f"  Tool: {step['tool']}")
        print(f"  Args: {json.dumps(step['arguments'], indent=4)}")
        print()

    # Step 3: Document decision
    print("Step 3: Document architectural decision...")
    decision_step = {
        "description": "Log architecture decision in journal",
        "tool": "journal_append",
        "arguments": {
            "content": "After researching authentication patterns, decided to implement JWT with refresh tokens. Provides good security with stateless operation.",
            "tags": ["decision", "authentication", "architecture"]
        }
    }

    print(f"  {decision_step['description']}")
    print(f"  Tool: {decision_step['tool']}")
    print(f"  Args: {json.dumps(decision_step['arguments'], indent=4)}")
    print()

    # Step 4: Cross-reference search
    print("Step 4: Find related information...")
    search_step = {
        "description": "Search across all systems for authentication info",
        "tool": "multi_search",
        "arguments": {
            "query": "authentication JWT",
            "include_kb": True,
            "include_research": True,
            "include_journal": True
        }
    }

    print(f"  {search_step['description']}")
    print(f"  Tool: {search_step['tool']}")
    print(f"  Args: {json.dumps(search_step['arguments'], indent=4)}")
    print()

async def main():
    """Run all examples."""
    print("Knowledge-MCP Usage Examples")
    print("============================\n")

    # Create sample documents
    create_sample_documents()

    # Run example sections
    await basic_knowledge_operations()
    await document_ingestion_examples()
    await research_workflow_examples()
    await journal_examples()
    await advanced_search_examples()
    await complete_workflow_example()

    print("=== Summary ===")
    print("These examples demonstrate the key capabilities of Knowledge-MCP:")
    print("1. Intelligent document processing with change detection")
    print("2. Unified knowledge base with semantic search")
    print("3. Research workflow with source and experiment tracking")
    print("4. Decision journaling with configuration snapshots")
    print("5. Cross-system search and relationship discovery")
    print()
    print("For more information, see:")
    print("- API Reference: API_REFERENCE.md")
    print("- Claude Integration: CLAUDE_INTEGRATION.md")
    print("- Architecture Guide: ARCHITECTURE.md")

if __name__ == "__main__":
    asyncio.run(main())