#!/usr/bin/env python3
"""
Test for automatic MCP Index re-indexing on empty search results.

This test verifies that:
1. Empty search results trigger staleness check
2. Stale index (>1 hour) triggers background re-index
3. Fresh index (<1 hour) does NOT trigger re-index
4. Response contains re_index_triggered metadata
"""

import sys
import pytest
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from knowledge_mcp.server import handle_mcp_index_search


class TestMCPIndexAutoReindex:
    """Test automatic re-indexing on empty search results."""

    @patch('knowledge_mcp.server.supabase')
    @patch('knowledge_mcp.server.MCPIndexScanner')
    @patch('knowledge_mcp.server.threading')
    def test_empty_results_stale_index_triggers_reindex(self, mock_threading, mock_scanner_class, mock_supabase):
        """Test that empty results + stale index triggers background re-index."""

        # Mock search_tools to return empty results
        mock_scanner = Mock()
        mock_scanner.search_tools.return_value = []
        mock_scanner_class.return_value = mock_scanner

        # Mock last scan time as 2 hours ago (stale)
        two_hours_ago = (datetime.utcnow() - timedelta(hours=2)).isoformat() + "Z"
        mock_supabase.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = Mock(
            data=[{"scan_time": two_hours_ago}]
        )

        # Mock threading.Thread
        mock_thread = Mock()
        mock_threading.Thread.return_value = mock_thread

        # Call the handler
        result = handle_mcp_index_search("nonexistent_tool_xyz")

        # Assertions
        assert result["ok"] is True
        assert result["data"]["re_index_triggered"] is True
        assert "re-index triggered in background" in result["message"]
        assert len(result["data"]["results"]) == 0

        # Verify background thread was started
        mock_threading.Thread.assert_called_once()
        mock_thread.start.assert_called_once()

        # Verify it's a daemon thread (non-blocking)
        assert mock_threading.Thread.call_args[1]["daemon"] is True

    @patch('knowledge_mcp.server.supabase')
    @patch('knowledge_mcp.server.MCPIndexScanner')
    @patch('knowledge_mcp.server.threading')
    def test_empty_results_fresh_index_no_reindex(self, mock_threading, mock_scanner_class, mock_supabase):
        """Test that empty results + fresh index does NOT trigger re-index."""

        # Mock search_tools to return empty results
        mock_scanner = Mock()
        mock_scanner.search_tools.return_value = []
        mock_scanner_class.return_value = mock_scanner

        # Mock last scan time as 30 minutes ago (fresh)
        thirty_minutes_ago = (datetime.utcnow() - timedelta(minutes=30)).isoformat() + "Z"
        mock_supabase.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = Mock(
            data=[{"scan_time": thirty_minutes_ago}]
        )

        # Call the handler
        result = handle_mcp_index_search("nonexistent_tool_xyz")

        # Assertions
        assert result["ok"] is True
        assert result["data"]["re_index_triggered"] is False
        assert "re-index triggered" not in result["message"]

        # Verify NO background thread was started
        mock_threading.Thread.assert_not_called()

    @patch('knowledge_mcp.server.supabase')
    @patch('knowledge_mcp.server.MCPIndexScanner')
    @patch('knowledge_mcp.server.threading')
    def test_non_empty_results_no_reindex(self, mock_threading, mock_scanner_class, mock_supabase):
        """Test that non-empty results never trigger re-index (even if stale)."""

        # Mock search_tools to return results
        mock_scanner = Mock()
        mock_scanner.search_tools.return_value = [
            {"tool_name": "kb_search", "description": "Search knowledge base"}
        ]
        mock_scanner_class.return_value = mock_scanner

        # Call the handler (no need to mock supabase since we won't check staleness)
        result = handle_mcp_index_search("search")

        # Assertions
        assert result["ok"] is True
        assert len(result["data"]["results"]) == 1
        assert result["data"]["re_index_triggered"] is False

        # Verify NO background thread was started
        mock_threading.Thread.assert_not_called()

    @patch('knowledge_mcp.server.supabase')
    @patch('knowledge_mcp.server.MCPIndexScanner')
    @patch('knowledge_mcp.server.threading')
    def test_no_scan_history_triggers_initial_scan(self, mock_threading, mock_scanner_class, mock_supabase):
        """Test that empty scan history triggers initial background scan."""

        # Mock search_tools to return empty results
        mock_scanner = Mock()
        mock_scanner.search_tools.return_value = []
        mock_scanner_class.return_value = mock_scanner

        # Mock no scan history
        mock_supabase.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = Mock(
            data=[]
        )

        # Mock threading.Thread
        mock_thread = Mock()
        mock_threading.Thread.return_value = mock_thread

        # Call the handler
        result = handle_mcp_index_search("nonexistent_tool")

        # Assertions
        assert result["ok"] is True
        assert result["data"]["re_index_triggered"] is True

        # Verify background thread was started
        mock_threading.Thread.assert_called_once()
        mock_thread.start.assert_called_once()

    @patch('knowledge_mcp.server.supabase')
    @patch('knowledge_mcp.server.MCPIndexScanner')
    def test_staleness_check_failure_graceful_degradation(self, mock_scanner_class, mock_supabase):
        """Test that staleness check failures don't crash the search."""

        # Mock search_tools to return empty results
        mock_scanner = Mock()
        mock_scanner.search_tools.return_value = []
        mock_scanner_class.return_value = mock_scanner

        # Mock supabase query to raise exception
        mock_supabase.table.side_effect = Exception("Database connection error")

        # Call the handler - should not crash
        result = handle_mcp_index_search("test")

        # Assertions - should still return valid response
        assert result["ok"] is True
        assert len(result["data"]["results"]) == 0
        assert result["data"]["re_index_triggered"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
