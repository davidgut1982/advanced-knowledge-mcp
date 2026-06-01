"""Test-only stub of agent.memory_provider.MemoryProvider.

This mirrors the real Hermes ABC signatures (hermes-agent 0.14.0,
agent/memory_provider.py on CT 133) closely enough for the Lore plugin to
import and subclass it during local testing, where hermes-agent is not
installed. It is NOT shipped to CT 133 — the real ABC lives in the hermes
venv there. Kept under tests/_hermes_stubs/ and added to sys.path only by
conftest.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class MemoryProvider(ABC):
    """Stub ABC matching the real Hermes MemoryProvider interface."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None: ...

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]: ...

    # Optional hooks (defaults match the real ABC)
    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None: ...

    def sync_turn(
        self, user_content: str, assistant_content: str, *, session_id: str = ""
    ) -> None: ...

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None: ...

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None: ...

    def on_session_end(self, messages: list[dict[str, Any]]) -> None: ...

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None: ...

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        return ""

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs
    ) -> None: ...

    def get_config_schema(self) -> list[dict[str, Any]]:
        return []

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None: ...

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...
