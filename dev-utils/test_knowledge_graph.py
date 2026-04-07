#!/usr/bin/env python3
"""
Test script for knowledge graph functionality.

Tests the kg_* tools by creating nodes, edges, and searching.
"""

import os
import sys
import json
from pathlib import Path

# Add project paths
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "shared"))

from knowledge_mcp.server import (
    handle_kg_add_node,
    handle_kg_add_edge,
    handle_kg_get_node,
    handle_kg_neighbors,
    handle_kg_search,
    handle_kb_add,
    db
)
from db_client import get_db_client


def setup_test_env():
    """Set up test environment."""
    os.environ.setdefault('DB_BACKEND', 'local')
    os.environ.setdefault('DB_HOST', 'localhost')
    os.environ.setdefault('DB_PORT', '5433')
    os.environ.setdefault('DB_NAME', 'mpm_system')
    os.environ.setdefault('DB_USER', 'latvian_user')
    os.environ.setdefault('DB_PASSWORD', 'latvian_dev_password_2026')

    # Initialize database
    global db
    db = get_db_client()


def test_kg_add_node():
    """Test adding nodes."""
    print("🧪 Testing kg_add_node...")

    # Test creating a topic node
    result = handle_kg_add_node(
        name="Machine Learning",
        node_type="topic",
        properties={"test": True, "description": "AI and ML concepts"}
    )
    print(f"  Topic node: {result}")

    # Test creating a document node
    result = handle_kg_add_node(
        name="Deep Learning Fundamentals",
        node_type="document",
        properties={"test": True, "content_type": "tutorial"}
    )
    print(f"  Document node: {result}")

    # Test creating a person node
    result = handle_kg_add_node(
        name="Geoffrey Hinton",
        node_type="person",
        properties={"test": True, "role": "researcher"}
    )
    print(f"  Person node: {result}")


def test_kg_add_edge():
    """Test adding relationships."""
    print("🔗 Testing kg_add_edge...")

    # Link topic to document
    result = handle_kg_add_edge(
        from_node="Machine Learning",
        to_node="Deep Learning Fundamentals",
        relationship_type="contains",
        properties={"test": True}
    )
    print(f"  Topic -> Document: {result}")

    # Link person to document
    result = handle_kg_add_edge(
        from_node="Geoffrey Hinton",
        to_node="Deep Learning Fundamentals",
        relationship_type="contributed_to",
        properties={"test": True, "contribution": "foundational work"}
    )
    print(f"  Person -> Document: {result}")


def test_kg_get_node():
    """Test fetching node details."""
    print("🔍 Testing kg_get_node...")

    result = handle_kg_get_node(
        name="Machine Learning",
        include_edges=True
    )
    print(f"  Get ML topic: {json.dumps(result, indent=2, default=str)}")


def test_kg_neighbors():
    """Test exploring graph neighborhood."""
    print("🕸️ Testing kg_neighbors...")

    result = handle_kg_neighbors(
        name="Machine Learning",
        depth=2
    )
    print(f"  Neighbors of ML: {json.dumps(result, indent=2, default=str)}")


def test_kg_search():
    """Test searching the graph."""
    print("🔎 Testing kg_search...")

    result = handle_kg_search(
        query="learning",
        limit=10
    )
    print(f"  Search for 'learning': {json.dumps(result, indent=2, default=str)}")

    result = handle_kg_search(
        query="Geoffrey",
        node_type="person",
        limit=5
    )
    print(f"  Search for 'Geoffrey' (person): {json.dumps(result, indent=2, default=str)}")


def test_integration_with_kb():
    """Test knowledge graph integration with KB entries."""
    print("🔄 Testing integration with KB entries...")

    # Add a KB entry (should auto-create KG nodes)
    result = handle_kb_add(
        topic="Neural Networks",
        title="Introduction to Backpropagation",
        content="Backpropagation is a fundamental algorithm for training neural networks...",
        tags=["algorithms", "training", "neural-networks"]
    )
    print(f"  Added KB entry: {result}")

    # Check if KG nodes were auto-created
    result = handle_kg_search(
        query="Neural Networks",
        node_type="topic"
    )
    print(f"  Auto-created topic node: {json.dumps(result, indent=2, default=str)}")

    result = handle_kg_search(
        query="Backpropagation",
        node_type="document"
    )
    print(f"  Auto-created document node: {json.dumps(result, indent=2, default=str)}")


def test_error_handling():
    """Test error handling."""
    print("⚠️ Testing error handling...")

    # Try to get non-existent node
    result = handle_kg_get_node(name="NonExistentNode")
    print(f"  Get non-existent node: {result}")

    # Try to create edge with non-existent nodes
    result = handle_kg_add_edge(
        from_node="NonExistent1",
        to_node="NonExistent2",
        relationship_type="related_to"
    )
    print(f"  Edge with non-existent nodes: {result}")


def cleanup_test_data():
    """Clean up test data."""
    print("🧹 Cleaning up test data...")

    try:
        # Delete test nodes (edges will cascade)
        test_names = [
            "Machine Learning",
            "Deep Learning Fundamentals",
            "Geoffrey Hinton",
            "Neural Networks",
            "Introduction to Backpropagation",
            "algorithms",
            "training",
            "neural-networks"
        ]

        for name in test_names:
            try:
                node_result = db.table("knowledge.kg_nodes")\
                    .select("id")\
                    .eq("name", name)\
                    .execute()

                for node in node_result.data:
                    db.table("knowledge.kg_nodes")\
                        .delete()\
                        .eq("id", node["id"])\
                        .execute()
                    print(f"  Deleted node: {name}")
            except Exception as e:
                print(f"  Error deleting {name}: {e}")

        # Also clean up any test KB entries
        kb_result = db.table("knowledge.kb_entries")\
            .select("kb_id")\
            .eq("title", "Introduction to Backpropagation")\
            .execute()

        for entry in kb_result.data:
            db.table("knowledge.kb_entries")\
                .delete()\
                .eq("kb_id", entry["kb_id"])\
                .execute()
            print(f"  Deleted KB entry: {entry['kb_id']}")

    except Exception as e:
        print(f"  Cleanup error: {e}")


def main():
    """Run all tests."""
    print("🚀 Starting Knowledge Graph Tests")
    print("=" * 50)

    setup_test_env()

    try:
        test_kg_add_node()
        print()
        test_kg_add_edge()
        print()
        test_kg_get_node()
        print()
        test_kg_neighbors()
        print()
        test_kg_search()
        print()
        test_integration_with_kb()
        print()
        test_error_handling()
        print()

    finally:
        cleanup_test_data()

    print("=" * 50)
    print("✅ Knowledge Graph Tests Complete")


if __name__ == "__main__":
    main()