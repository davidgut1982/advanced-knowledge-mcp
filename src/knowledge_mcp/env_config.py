"""
Centralized environment configuration for all Latvian MCP servers.

This module loads environment variables from:
1. /srv/latvian_mcp/.env file (if it exists)
2. System environment variables (which override .env values)

Usage:
    from .env_config import get_env, require_env

    # Get with default
    supabase_url = get_env("SUPABASE_URL", "https://default.supabase.co")

    # Require (raises ValueError if not set)
    supabase_key = require_env("SUPABASE_KEY")
"""

import os
from pathlib import Path
from typing import Optional

# Path to centralized .env file
# Check for ENV_FILE environment variable first, then default to centralized location
ENV_FILE = Path(os.getenv("ENV_FILE", str(Path(__file__).parent.parent.parent / ".env")))


def load_env_file():
    """Load environment variables from .env file if it exists."""
    if not ENV_FILE.exists():
        return

    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#"):
                continue

            # Parse KEY=VALUE
            if "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()

                # Only set if not already in environment
                # (environment variables take precedence)
                if key not in os.environ:
                    os.environ[key] = value


# Load .env file when module is imported
load_env_file()


def get_env(key: str, default: Optional[str] = None) -> Optional[str]:
    """
    Get environment variable with optional default.

    Args:
        key: Environment variable name
        default: Default value if not set

    Returns:
        Environment variable value or default
    """
    return os.getenv(key, default)


def require_env(key: str) -> str:
    """
    Get required environment variable.

    Args:
        key: Environment variable name

    Returns:
        Environment variable value

    Raises:
        ValueError: If environment variable is not set
    """
    value = os.getenv(key)
    if value is None:
        raise ValueError(f"{key} environment variable is required but not set")
    return value
