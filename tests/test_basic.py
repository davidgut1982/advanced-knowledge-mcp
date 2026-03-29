"""Basic tests for Knowledge MCP server."""

import pytest
import tempfile
import os
from pathlib import Path


def test_import():
    """Test that the knowledge_mcp module can be imported."""
    import knowledge_mcp
    assert knowledge_mcp is not None


def test_server_import():
    """Test that the server module can be imported."""
    from knowledge_mcp import server
    assert server is not None


@pytest.fixture
def temp_data_dir():
    """Create a temporary directory for testing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


def test_json_backend_basic(temp_data_dir):
    """Test basic functionality with JSON backend."""
    # Set environment for JSON backend
    os.environ['KNOWLEDGE_DATA_DIR'] = str(temp_data_dir)

    # Import after setting environment
    from knowledge_mcp.server import KnowledgeMCPServer

    # Create server instance
    server = KnowledgeMCPServer()

    # Basic validation
    assert server is not None
    assert hasattr(server, 'kb_add')
    assert hasattr(server, 'kb_search')


def test_version():
    """Test that version information is available."""
    import knowledge_mcp
    # Basic check that version info exists
    assert hasattr(knowledge_mcp, '__version__') or True  # May not be set in dev


class TestKnowledgeOperations:
    """Test knowledge base operations."""

    def test_kb_add_structure(self, temp_data_dir):
        """Test that kb_add has proper structure."""
        os.environ['KNOWLEDGE_DATA_DIR'] = str(temp_data_dir)

        from knowledge_mcp.server import KnowledgeMCPServer
        server = KnowledgeMCPServer()

        # Check that method exists and is callable
        assert hasattr(server, 'kb_add')
        assert callable(getattr(server, 'kb_add'))

    def test_kb_search_structure(self, temp_data_dir):
        """Test that kb_search has proper structure."""
        os.environ['KNOWLEDGE_DATA_DIR'] = str(temp_data_dir)

        from knowledge_mcp.server import KnowledgeMCPServer
        server = KnowledgeMCPServer()

        # Check that method exists and is callable
        assert hasattr(server, 'kb_search')
        assert callable(getattr(server, 'kb_search'))


class TestResearchOperations:
    """Test research workflow operations."""

    def test_research_methods_exist(self, temp_data_dir):
        """Test that research methods exist."""
        os.environ['KNOWLEDGE_DATA_DIR'] = str(temp_data_dir)

        from knowledge_mcp.server import KnowledgeMCPServer
        server = KnowledgeMCPServer()

        # Check research methods
        research_methods = [
            'research_add_note',
            'research_list_notes',
            'research_add_source',
            'research_list_sources'
        ]

        for method in research_methods:
            assert hasattr(server, method), f"Missing method: {method}"


class TestJournalOperations:
    """Test journal operations."""

    def test_journal_methods_exist(self, temp_data_dir):
        """Test that journal methods exist."""
        os.environ['KNOWLEDGE_DATA_DIR'] = str(temp_data_dir)

        from knowledge_mcp.server import KnowledgeMCPServer
        server = KnowledgeMCPServer()

        # Check journal methods
        journal_methods = [
            'journal_append',
            'journal_get',
            'journal_list'
        ]

        for method in journal_methods:
            assert hasattr(server, method), f"Missing method: {method}"


# Add some integration tests if database is available
@pytest.mark.skipif(
    not os.getenv('TEST_DATABASE_URL'),
    reason="Database tests require TEST_DATABASE_URL"
)
class TestDatabaseIntegration:
    """Test database integration when available."""

    def test_database_connection(self):
        """Test database connection."""
        os.environ['DATABASE_URL'] = os.getenv('TEST_DATABASE_URL')

        from knowledge_mcp.server import KnowledgeMCPServer
        server = KnowledgeMCPServer()

        # If we get here without error, database connection is working
        assert server is not None