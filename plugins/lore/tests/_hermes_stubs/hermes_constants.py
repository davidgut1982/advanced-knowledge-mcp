"""Test-only stub of hermes_constants.

Mirrors the real Hermes helpers used by the plugin:
    get_hermes_home() -> Path
    display_hermes_home() -> str

For tests, HERMES_HOME defaults to the env var or a temp-friendly path.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/hermes/home"))


def display_hermes_home() -> str:
    return os.environ.get("HERMES_HOME", "$HERMES_HOME")
