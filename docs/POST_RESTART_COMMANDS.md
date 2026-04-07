# Post-Restart Commands for Knowledge MCP

## Step 1: Verify All 21 KB MCP Tools Are Loaded

```bash
claude mcp list-tools | grep -E "(kb_|kg_|research_|journal_)" | wc -l
```

Expected output: **21**

To see all tools:
```bash
claude mcp list-tools | grep -E "(kb_|kg_|research_|journal_)"
```

Expected tools:
- **Knowledge Base (4):** kb_add, kb_search, kb_get, kb_list
- **Knowledge Graph (5):** kg_add_node, kg_add_edge, kg_get_node, kg_neighbors, kg_search
- **Research (8):** research_add_note, research_list_notes, research_get_note, research_add_source, research_list_sources, research_log_experiment, research_list_experiments, research_link_source_to_experiment
- **Journal (4):** journal_append, journal_list, journal_get, snapshot_config

## Step 2: Add Bugfix Documentation to Knowledge Base

Once tools are verified, use the `kb_add` MCP tool to add the bugfix documentation.

### Documentation Entry Details:

**Topic:** `mcp-server-debugging`

**Title:** `Knowledge MCP Server Bug Fixes - 2025-12-08`

**Content:** Complete bugfix documentation covering:
1. Wrong ErrorCodes constant (22 occurrences fixed)
2. Missing .env file loading (added load_dotenv())
3. Supabase initialization timing bug (moved to main())
4. Configuration updates (.env, pyproject.toml, README.md)
5. Testing results and verification steps
6. MCP server lifecycle architecture notes

**Tags:** `bugfix`, `mcp-server`, `knowledge-mcp`, `supabase`, `dotenv`, `initialization`, `troubleshooting`

**Source Path:** `/srv/latvian_mcp/servers/knowledge-mcp/BUGFIX_2025-12-08.md`

---

## Ready-to-Run Command for Claude Code

After restart, simply tell Claude:

> "Add the bugfix documentation from /srv/latvian_mcp/servers/knowledge-mcp/BUGFIX_2025-12-08.md to the knowledge base with topic 'mcp-server-debugging' and tags: bugfix, mcp-server, knowledge-mcp, supabase, dotenv, initialization, troubleshooting"

Claude will use the `kb_add` tool (which will be available after restart) to add the documentation.

---

## Verification After Adding to KB

Search for the entry:
```
Tell Claude: "Search the knowledge base for 'supabase initialization'"
```

This should return the bugfix documentation entry.

---

## Summary

✅ **Before restart:** Server code is fixed and tested
🔄 **After restart:** 21 KB MCP tools will be available
📚 **Next action:** Add BUGFIX_2025-12-08.md to knowledge base

The knowledge MCP server is now production-ready with all critical bugs resolved!
