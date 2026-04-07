#!/usr/bin/env python3
"""
Test script to verify PostgreSQL array parameter bug fix.
Tests kb_add and research_add_note with empty and populated tags arrays.
"""

import sys
import os
from pathlib import Path

# Add shared utilities to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))
from db_client import get_db_client

def test_array_parameters():
    """Test array parameter handling with PostgreSQL."""

    # Initialize database client (uses local PostgreSQL)
    os.environ['DB_BACKEND'] = 'local'
    os.environ['DB_HOST'] = 'localhost'
    os.environ['DB_PORT'] = '5433'
    os.environ['DB_NAME'] = 'mpm_system'
    os.environ['DB_USER'] = 'latvian_user'
    os.environ['DB_PASSWORD'] = 'latvian_dev_password_2026'

    db = get_db_client()

    print("Testing PostgreSQL array parameter handling...")

    # Test 1: Insert with empty tags array
    print("\n1. Testing kb_add with empty tags array...")
    try:
        result = db.table("knowledge.kb_entries").insert({
            "kb_id": "test_empty_tags",
            "topic": "test",
            "title": "Test Entry - Empty Tags",
            "content": "This is a test with empty tags array",
            "tags": []  # Empty array - this was causing "malformed array literal" error
        }).execute()
        print("   SUCCESS: Empty tags array inserted")
        print(f"   Result: {result.data}")
    except Exception as e:
        print(f"   FAILED: {e}")
        return False

    # Test 2: Insert with populated tags array
    print("\n2. Testing kb_add with populated tags array...")
    try:
        result = db.table("knowledge.kb_entries").insert({
            "kb_id": "test_populated_tags",
            "topic": "test",
            "title": "Test Entry - Populated Tags",
            "content": "This is a test with populated tags array",
            "tags": ["tag1", "tag2", "tag3"]
        }).execute()
        print("   SUCCESS: Populated tags array inserted")
        print(f"   Result: {result.data}")
    except Exception as e:
        print(f"   FAILED: {e}")
        return False

    # Test 3: Insert research note with empty tags
    print("\n3. Testing research_add_note with empty tags array...")
    try:
        result = db.table("knowledge.research_notes").insert({
            "note_id": "test_note_empty",
            "topic": "test",
            "title": "Test Note - Empty Tags",
            "content": "Research note with empty tags",
            "tags": []
        }).execute()
        print("   SUCCESS: Research note with empty tags inserted")
        print(f"   Result: {result.data}")
    except Exception as e:
        print(f"   FAILED: {e}")
        return False

    # Test 4: Insert research note with populated tags
    print("\n4. Testing research_add_note with populated tags array...")
    try:
        result = db.table("knowledge.research_notes").insert({
            "note_id": "test_note_populated",
            "topic": "test",
            "title": "Test Note - Populated Tags",
            "content": "Research note with populated tags",
            "tags": ["research", "testing", "arrays"]
        }).execute()
        print("   SUCCESS: Research note with populated tags inserted")
        print(f"   Result: {result.data}")
    except Exception as e:
        print(f"   FAILED: {e}")
        return False

    # Cleanup: Delete test entries
    print("\n5. Cleaning up test entries...")
    try:
        db.table("knowledge.kb_entries").delete().in_("kb_id", ["test_empty_tags", "test_populated_tags"]).execute()
        db.table("knowledge.research_notes").delete().in_("note_id", ["test_note_empty", "test_note_populated"]).execute()
        print("   SUCCESS: Test entries cleaned up")
    except Exception as e:
        print(f"   WARNING: Cleanup failed: {e}")

    print("\n" + "="*60)
    print("All tests passed! Array parameter bug is fixed.")
    print("="*60)
    return True

if __name__ == "__main__":
    success = test_array_parameters()
    sys.exit(0 if success else 1)
