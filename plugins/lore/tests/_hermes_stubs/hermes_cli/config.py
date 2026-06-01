"""Test-only stub of hermes_cli.config.cfg_get.

Mirrors the real Hermes helper (hermes_cli/config.py on CT 133):
    def cfg_get(cfg, *keys, default=None) -> Any
Safely traverses nested dict keys, returning ``default`` on any miss.
"""

from __future__ import annotations

from typing import Any, Optional


def cfg_get(cfg: dict[str, Any] | None, *keys: str, default: Any = None) -> Any:
    cur: Any = cfg
    for key in keys:
        if not isinstance(cur, dict):
            return default
        if key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default
