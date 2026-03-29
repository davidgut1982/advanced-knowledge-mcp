#!/usr/bin/env python3
"""Test docker-mcp indexing fix."""

import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Set environment variables
os.environ['SUPABASE_URL'] = os.getenv('SUPABASE_URL', '')
os.environ['SUPABASE_KEY'] = os.getenv('SUPABASE_KEY', '')
os.environ['SENTRY_DSN'] = ''

from knowledge_mcp.mcp_index_scanner import MCPIndexScanner


class MockSupabase:
    """Mock Supabase client for testing."""

    def __init__(self):
        self.tables = {
            'mcp_servers': {},
            'mcp_tools': {},
            'mcp_index_versions': []
        }

    def table(self, name):
        self._current_table = name
        return self

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def ilike(self, *args, **kwargs):
        return self

    def or_(self, *args, **kwargs):
        return self

    def upsert(self, data, on_conflict=None):
        if isinstance(data, list):
            for item in data:
                key = item.get('tool_id') or item.get('server_id')
                self.tables[self._current_table][key] = item
        else:
            key = data.get('tool_id') or data.get('server_id')
            self.tables[self._current_table][key] = data
        return self

    def update(self, data):
        return self

    def insert(self, data):
        return self

    def execute(self):
        result = type('Result', (), {})()

        if self._current_table == 'mcp_servers':
            result.data = list(self.tables['mcp_servers'].values())
        elif self._current_table == 'mcp_tools':
            result.data = list(self.tables['mcp_tools'].values())
        elif self._current_table == 'mcp_index_versions':
            result.data = [{'version_id': len(self.tables['mcp_index_versions']) + 1}]
        else:
            result.data = []

        return result


def test_docker_mcp_indexing():
    """Test that docker-mcp tools are properly indexed."""

    print("\n" + "="*70)
    print("TEST: Docker-MCP Indexing Fix")
    print("="*70)

    supabase = MockSupabase()
    scanner = MCPIndexScanner(supabase)

    print("\n1. Scanning all MCP servers...")
    result = scanner.scan_all_servers(triggered_by="test")

    print(f"\n2. Results:")
    print(f"   - Servers scanned: {result['servers_scanned']}")
    print(f"   - Tools indexed: {result['tools_indexed']}")
    print(f"   - Scan duration: {result['scan_duration_ms']}ms")

    if result['errors']:
        print(f"\n   Errors ({len(result['errors'])}):")
        for error in result['errors'][:3]:
            print(f"     - {error}")

    print(f"\n3. Servers in index:")
    for server in sorted(result['changes']['added']):
        print(f"   - {server}")

    # Check docker-mcp specifically
    docker_tools = [t for t in supabase.tables['mcp_tools'].values()
                   if t.get('server_id') == 'docker-mcp']

    print(f"\n4. Docker-MCP Analysis:")
    print(f"   - Tools indexed: {len(docker_tools)}")

    if docker_tools:
        print(f"\n   Tools:")
        for tool in sorted(docker_tools, key=lambda x: x.get('tool_name', ''))[:5]:
            print(f"     - {tool.get('tool_name')}: {tool.get('tool_id')}")
        if len(docker_tools) > 5:
            print(f"     ... and {len(docker_tools) - 5} more")

        # Verify tool properties
        first_tool = docker_tools[0]
        print(f"\n   First tool details:")
        print(f"     - tool_name: {first_tool.get('tool_name')}")
        print(f"     - tool_id: {first_tool.get('tool_id')}")
        print(f"     - full_name: {first_tool.get('full_name')}")
        print(f"     - category: {first_tool.get('category')}")
        print(f"     - has input_schema: {'input_schema' in first_tool}")

    # Final verdict
    print(f"\n" + "="*70)
    if len(docker_tools) == 19:
        print("✓ SUCCESS: All 19 docker-mcp tools indexed correctly!")
        print("="*70)
        return True
    else:
        print(f"✗ FAILURE: Expected 19 tools, got {len(docker_tools)}")
        print("="*70)
        return False


if __name__ == "__main__":
    success = test_docker_mcp_indexing()
    sys.exit(0 if success else 1)
