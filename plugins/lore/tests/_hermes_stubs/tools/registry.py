"""Test-only stub of tools.registry.tool_error.

Mirrors the real Hermes helper (tools/registry.py on CT 133):
    def tool_error(message, **extra) -> str
returning a JSON error string.
"""

from __future__ import annotations

import json


def tool_error(message, **extra) -> str:
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)
