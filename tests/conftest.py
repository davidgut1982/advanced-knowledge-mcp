"""Test configuration for Knowledge MCP tests."""

import pytest
import tempfile
import os
from pathlib import Path


@pytest.fixture(scope="session")
def temp_test_dir():
    """Create a temporary directory for the test session."""
    with tempfile.TemporaryDirectory(prefix="knowledge_mcp_test_") as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def clean_environment():
    """Clean environment variables for tests."""
    # Store original values
    original_env = {}
    env_vars_to_clean = [
        'DATABASE_URL',
        'SUPABASE_URL',
        'SUPABASE_ANON_KEY',
        'KNOWLEDGE_DATA_DIR',
        'KNOWLEDGE_LOG_LEVEL'
    ]

    for var in env_vars_to_clean:
        if var in os.environ:
            original_env[var] = os.environ[var]
            del os.environ[var]

    yield

    # Restore original values
    for var, value in original_env.items():
        os.environ[var] = value


@pytest.fixture
def json_backend_env(temp_test_dir, clean_environment):
    """Set up environment for JSON backend testing."""
    os.environ['KNOWLEDGE_DATA_DIR'] = str(temp_test_dir)
    os.environ['KNOWLEDGE_LOG_LEVEL'] = 'DEBUG'
    yield


@pytest.fixture
def database_backend_env(clean_environment):
    """Set up environment for database backend testing."""
    if os.getenv('TEST_DATABASE_URL'):
        os.environ['DATABASE_URL'] = os.getenv('TEST_DATABASE_URL')
        os.environ['KNOWLEDGE_LOG_LEVEL'] = 'DEBUG'
        yield
    else:
        pytest.skip("Database tests require TEST_DATABASE_URL environment variable")


# Add markers for different test types
def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "database: marks tests as requiring database"
    )
    config.addinivalue_line(
        "markers", "slow: marks tests as slow running"
    )