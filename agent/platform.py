"""Platform boundary for the agent runtime.

The agent owns model selection, history, skills, and tool execution. A platform
adapter owns the transport-specific prompt, identity formatting, and model
context/toolsets. Slack is the first implementation; another platform can
provide the same small interface without changing the agent loop.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PlatformAdapter(Protocol):
    """The platform services required by the model-facing agent runtime."""

    name: str

    @property
    def system_prompt(self) -> str:
        """The platform-specific system prompt, unchanged for that platform."""
        ...

    def format_user_message(self, text: str, deps: Any) -> str:
        """Attribute a message using the platform's native identity format."""
        ...

    def build_context_prompt(self, deps: Any, model: str, is_vision: bool) -> str:
        """Build the platform context appended to the system prompt."""
        ...

    def toolsets(self, deps: Any) -> list[Any]:
        """Return platform-native MCP/tool sets for this turn."""
        ...
