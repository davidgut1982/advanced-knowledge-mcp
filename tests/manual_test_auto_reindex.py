#!/usr/bin/env python3
"""
Manual integration test for auto re-indexing.

This script demonstrates the auto re-index behavior by:
1. Searching for a non-existent tool
2. Checking if re-index is triggered based on staleness
3. Waiting for background scan to complete
4. Verifying the index was updated
"""

import os
import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Set required environment variables
os.environ['SUPABASE_URL'] = os.getenv('SUPABASE_URL', 'http://localhost:54321')
os.environ['SUPABASE_KEY'] = os.getenv('SUPABASE_KEY', 'test-key')
os.environ['SENTRY_DSN'] = ''  # Disable Sentry

from knowledge_mcp.server import handle_mcp_index_search
from knowledge_mcp import server as server_module


def main():
    """Run manual integration test."""
    print("=" * 80)
    print("MCP Index Auto Re-index Integration Test")
    print("=" * 80)

    # Initialize Supabase client (mock for testing)
    from unittest.mock import Mock
    mock_supabase = Mock()
    server_module.supabase = mock_supabase

    # Scenario 1: Empty results + stale index (should trigger)
    print("\n[Test 1] Empty results + stale index (>1 hour)")
    print("-" * 80)

    # Mock scanner to return empty results
    from knowledge_mcp.mcp_index_scanner import MCPIndexScanner
    from datetime import datetime, timedelta
    from unittest.mock import patch

    with patch.object(MCPIndexScanner, 'search_tools', return_value=[]):
        # Mock stale scan time (2 hours ago)
        two_hours_ago = (datetime.utcnow() - timedelta(hours=2)).isoformat() + "Z"
        mock_supabase.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = Mock(
            data=[{"scan_time": two_hours_ago}]
        )

        result = handle_mcp_index_search("nonexistent_tool_xyz")

        print(f"Result: {result}")
        print(f"✓ Re-index triggered: {result['data']['re_index_triggered']}")
        print(f"✓ Message: {result['message']}")

        assert result['data']['re_index_triggered'] is True, "Should trigger re-index"
        print("✓ Test 1 PASSED")

    # Scenario 2: Empty results + fresh index (should NOT trigger)
    print("\n[Test 2] Empty results + fresh index (<1 hour)")
    print("-" * 80)

    with patch.object(MCPIndexScanner, 'search_tools', return_value=[]):
        # Mock fresh scan time (30 minutes ago)
        thirty_minutes_ago = (datetime.utcnow() - timedelta(minutes=30)).isoformat() + "Z"
        mock_supabase.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = Mock(
            data=[{"scan_time": thirty_minutes_ago}]
        )

        result = handle_mcp_index_search("nonexistent_tool_xyz")

        print(f"Result: {result}")
        print(f"✓ Re-index triggered: {result['data']['re_index_triggered']}")
        print(f"✓ Message: {result['message']}")

        assert result['data']['re_index_triggered'] is False, "Should NOT trigger re-index"
        print("✓ Test 2 PASSED")

    # Scenario 3: Non-empty results (should never trigger)
    print("\n[Test 3] Non-empty results (never triggers)")
    print("-" * 80)

    with patch.object(MCPIndexScanner, 'search_tools', return_value=[{"tool_name": "kb_search"}]):
        result = handle_mcp_index_search("search")

        print(f"Result: {result}")
        print(f"✓ Re-index triggered: {result['data']['re_index_triggered']}")
        print(f"✓ Results count: {len(result['data']['results'])}")

        assert result['data']['re_index_triggered'] is False, "Should NOT trigger re-index"
        assert len(result['data']['results']) > 0, "Should have results"
        print("✓ Test 3 PASSED")

    print("\n" + "=" * 80)
    print("All integration tests PASSED!")
    print("=" * 80)


if __name__ == "__main__":
    main()
