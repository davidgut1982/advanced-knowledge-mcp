#!/usr/bin/env python3
"""
Comprehensive test for PostgreSQL array parameter bug fix.

This test verifies that the fix correctly handles:
1. Empty arrays []
2. Populated arrays ['tag1', 'tag2']
3. All handlers that use tags parameter (kb_add, research_add_note, journal_append)
4. Both INSERT and UPDATE operations

Bug details:
- Previously, psycopg2.extensions.adapt() was used for list serialization
- This returned an ISQLQuote object that caused "malformed array literal" errors
- Fix: Let psycopg2 handle Python lists natively (just return the list)
"""

import sys
import os
from pathlib import Path

# Add shared utilities to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
from db_client import get_db_client

def test_db_layer():
    """Test database layer directly."""
    print("=" * 70)
    print("PART 1: Testing database layer (db_client.py)")
    print("=" * 70)

    os.environ['DB_BACKEND'] = 'local'
    os.environ['DB_HOST'] = 'localhost'
    os.environ['DB_PORT'] = '5433'
    os.environ['DB_NAME'] = 'mpm_system'
    os.environ['DB_USER'] = 'latvian_user'
    os.environ['DB_PASSWORD'] = 'latvian_dev_password_2026'

    db = get_db_client()

    # Test 1: INSERT with empty tags
    print("\n1. Testing INSERT with empty tags array...")
    try:
        result = db.table("knowledge.kb_entries").insert({
            "kb_id": "test_empty_tags",
            "topic": "test",
            "title": "Empty Tags Test",
            "content": "Content",
            "tags": []
        }).execute()
        print("   ✓ SUCCESS: Empty tags inserted")
        assert result.data[0]['tags'] == [], f"Expected empty array, got {result.data[0]['tags']}"
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Test 2: INSERT with populated tags
    print("\n2. Testing INSERT with populated tags array...")
    try:
        result = db.table("knowledge.kb_entries").insert({
            "kb_id": "test_populated_tags",
            "topic": "test",
            "title": "Populated Tags Test",
            "content": "Content",
            "tags": ["tag1", "tag2", "tag3"]
        }).execute()
        print("   ✓ SUCCESS: Populated tags inserted")
        assert result.data[0]['tags'] == ["tag1", "tag2", "tag3"], \
            f"Expected ['tag1', 'tag2', 'tag3'], got {result.data[0]['tags']}"
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Test 3: UPDATE with empty tags
    print("\n3. Testing UPDATE with empty tags array...")
    try:
        result = db.table("knowledge.kb_entries")\
            .update({"tags": []})\
            .eq("kb_id", "test_populated_tags")\
            .execute()
        print("   ✓ SUCCESS: Tags updated to empty array")
        assert result.data[0]['tags'] == [], f"Expected empty array, got {result.data[0]['tags']}"
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Test 4: UPDATE with populated tags
    print("\n4. Testing UPDATE with populated tags array...")
    try:
        result = db.table("knowledge.kb_entries")\
            .update({"tags": ["updated1", "updated2"]})\
            .eq("kb_id", "test_empty_tags")\
            .execute()
        print("   ✓ SUCCESS: Tags updated to populated array")
        assert result.data[0]['tags'] == ["updated1", "updated2"], \
            f"Expected ['updated1', 'updated2'], got {result.data[0]['tags']}"
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Cleanup
    db.table("knowledge.kb_entries").delete().in_("kb_id", ["test_empty_tags", "test_populated_tags"]).execute()

    print("\n✓ All database layer tests passed!")
    return True


def test_mcp_handlers():
    """Test MCP server handlers."""
    print("\n" + "=" * 70)
    print("PART 2: Testing MCP handlers (server.py)")
    print("=" * 70)

    # Set environment
    os.environ['DB_BACKEND'] = 'local'
    os.environ['DB_HOST'] = 'localhost'
    os.environ['DB_PORT'] = '5433'
    os.environ['DB_NAME'] = 'mpm_system'
    os.environ['DB_USER'] = 'latvian_user'
    os.environ['DB_PASSWORD'] = 'latvian_dev_password_2026'

    # Import after env setup
    sys.path.insert(0, str(Path(__file__).parent / "src"))
    from knowledge_mcp.server import (
        handle_kb_add,
        handle_research_add_note,
        handle_journal_append
    )
    import knowledge_mcp.server as server_module
    from db_client import get_db_client

    # Initialize server's db global
    server_module.db = get_db_client()

    kb_ids = []
    note_ids = []
    entry_ids = []

    # Test kb_add
    print("\n1. Testing kb_add with empty tags...")
    try:
        result = handle_kb_add(topic="test", title="KB Test", content="Content", tags=[])
        assert result['ok'] == True, f"Expected ok=True, got {result}"
        kb_ids.append(result['data']['kb_id'])
        print("   ✓ SUCCESS: kb_add with empty tags")
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    print("\n2. Testing kb_add with populated tags...")
    try:
        result = handle_kb_add(topic="test", title="KB Test 2", content="Content", tags=["kb", "test"])
        assert result['ok'] == True, f"Expected ok=True, got {result}"
        kb_ids.append(result['data']['kb_id'])
        print("   ✓ SUCCESS: kb_add with populated tags")
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Test research_add_note
    print("\n3. Testing research_add_note with empty tags...")
    try:
        result = handle_research_add_note(topic="test", title="Research Test", content="Content", tags=[])
        assert result['ok'] == True, f"Expected ok=True, got {result}"
        note_ids.append(result['data']['note_id'])
        print("   ✓ SUCCESS: research_add_note with empty tags")
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    print("\n4. Testing research_add_note with populated tags...")
    try:
        result = handle_research_add_note(topic="test", title="Research Test 2", content="Content", tags=["research", "note"])
        assert result['ok'] == True, f"Expected ok=True, got {result}"
        note_ids.append(result['data']['note_id'])
        print("   ✓ SUCCESS: research_add_note with populated tags")
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Test journal_append
    print("\n5. Testing journal_append with empty tags...")
    try:
        result = handle_journal_append(entry_type="daily", content="Journal content", tags=[])
        assert result['ok'] == True, f"Expected ok=True, got {result}"
        entry_ids.append(result['data']['entry_id'])
        print("   ✓ SUCCESS: journal_append with empty tags")
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    print("\n6. Testing journal_append with populated tags...")
    try:
        result = handle_journal_append(entry_type="milestone", content="Journal content 2", tags=["journal", "test"])
        assert result['ok'] == True, f"Expected ok=True, got {result}"
        entry_ids.append(result['data']['entry_id'])
        print("   ✓ SUCCESS: journal_append with populated tags")
    except Exception as e:
        print(f"   ✗ FAILED: {e}")
        return False

    # Cleanup
    db = get_db_client()
    for kb_id in kb_ids:
        db.table("knowledge.kb_entries").delete().eq("kb_id", kb_id).execute()
    for note_id in note_ids:
        db.table("knowledge.research_notes").delete().eq("note_id", note_id).execute()
    for entry_id in entry_ids:
        db.table("knowledge.journal_entries").delete().eq("entry_id", entry_id).execute()

    print("\n✓ All MCP handler tests passed!")
    return True


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("PostgreSQL Array Parameter Bug Fix - Comprehensive Test Suite")
    print("=" * 70)
    print("\nBug: psycopg2.extensions.adapt() caused 'malformed array literal' errors")
    print("Fix: Let psycopg2 handle Python lists natively")
    print()

    # Run tests
    db_passed = test_db_layer()
    if not db_passed:
        print("\n✗ Database layer tests failed!")
        sys.exit(1)

    handler_passed = test_mcp_handlers()
    if not handler_passed:
        print("\n✗ MCP handler tests failed!")
        sys.exit(1)

    # Success
    print("\n" + "=" * 70)
    print("ALL TESTS PASSED ✓")
    print("=" * 70)
    print("\nThe PostgreSQL array parameter bug has been successfully fixed.")
    print("\nFixed file: /srv/latvian_mcp/shared/db_client.py")
    print("Method: _serialize_value() in LocalPostgresClient class")
    print("\nAffected tools:")
    print("  - kb_add")
    print("  - research_add_note")
    print("  - journal_append")
    print("  - All other MCP tools using array parameters")
    print()
    sys.exit(0)
