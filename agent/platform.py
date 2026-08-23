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

    def build_context_prompt(self, deps: Any) -> str:
        """Build the platform context appended to the system prompt.

        Only stable-per-thread facts belong here (channel/thread/user ids) —
        this text becomes part of the cached system-prompt prefix, so anything
        that changes turn to turn (see build_turn_context) would silently
        break prompt caching for every turn of the conversation.
        """
        ...

    def build_turn_context(self, deps: Any, model: str, is_vision: bool) -> str:
        """Build the per-turn context (message id, current model/capability)
        prepended to the user prompt instead of the system prompt, since it
        changes on every turn and must not sit inside the cached prefix.
        """
        ...

    def toolsets(self, deps: Any) -> list[Any]:
        """Return platform-native MCP/tool sets for this turn."""
        ...
