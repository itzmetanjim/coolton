"""Surface: everything a running turn needs in order to talk about THE CURRENT
CONVERSATION — as opposed to Slack itself, which every genuinely-Slack tool
(posting to another channel, Slack search, the Slack MCP toolset) keeps
reaching `deps.client` for directly, unchanged.

Split out so a second transport (the web UI) can implement the same handful of
"post into this conversation" operations without touching any tool that isn't
about the current thread. Mirrors agent.platform.PlatformAdapter's own
loose-Protocol style — duck typing, not a strict abc.ABC, since tests build
minimal fakes against this shape.

AgentDeps.surface is None for every caller that hasn't been touched (kevinton,
subagents, the scheduler's one-off deps, summarize_thread's direct call) —
AgentDeps.get_surface() lazily builds a SlackSurface from deps.client in that
case, so those callers need zero changes.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Surface(Protocol):
    """The current-conversation operations a running turn can perform."""

    def post_text(self, text: str) -> None:
        """Post a plain progress/status message into the current conversation."""
        ...

    def post_final(self, text: str) -> None:
        """Post (or stream) the turn's final answer."""
        ...

    def post_error(self, text: str) -> None:
        """Report an unrecoverable error to the current conversation."""
        ...

    def post_image(self, image_url: str, alt_text: str) -> str | None:
        """Post an image. Returns None on success, or an error string."""
        ...

    def post_file_link(self, url: str, filename: str, title: str = "", comment: str = "") -> str:
        """Post a link to a hosted file."""
        ...

    def post_embed(self, url: str, title: str, text: str, thumbnail_url: str | None = None) -> str:
        """Post a rich embed (whiteboard, HTML, agent-browser/desktop stream)."""
        ...

    def react(self, emoji_name: str) -> str:
        """React to the message that started this turn."""
        ...

    def remove_reaction(self, emoji_name: str, timestamp: str = "") -> str:
        """Remove a reaction from a message in this conversation."""
        ...

    def set_activity(self, text: str) -> None:
        """Live 'what's coolton doing right now' status, refreshed through the turn."""
        ...

    def download_attachments(self, sandbox: Any, limit: int = 20) -> str:
        """Download this conversation's attachments into the sandbox."""
        ...

    def summarize(self) -> str:
        """Summarize the current conversation."""
        ...

    def set_engaged(self, engaged: bool) -> str:
        """Toggle whether future messages in this conversation get a turn without
        an explicit mention. Platforms without that concept (web: every message is
        addressed to coolton) report it as not applicable."""
        ...

    def build_hooks(self, deps: Any) -> Any | None:
        """Return a pydantic_ai Hooks capability (or None) for showing live
        turn progress — the "thinking block" equivalent for this platform."""
        ...

    def set_model(self, deps: Any, model_used: str) -> None:
        """Show which model this attempt is using, live. Slack renders this as
        part of its plan block directly (agent.plan_block.set_model_task handles
        it without going through here); non-Slack surfaces implement this."""
        ...

    def finish_turn(self, deps: Any) -> None:
        """Called exactly once, at the very end of a turn that didn't already
        end through post_error (an exception, or an early `[!WITH:tag]` error —
        both `return`/jump past this call, so it's never double-fired). Slack's
        plan block already conveys "done" through complete_plan_message /
        set_plan_error, so this is a no-op there; a surface that needs an
        explicit end-of-turn signal (the web UI's turn_end event) implements it."""
        ...


def get_surface(deps: Any) -> Surface:
    """Resolve deps.surface, lazily building a SlackSurface from deps' own
    client/channel_id/thread_ts/message_ts/user_token if nothing set it.

    A free function, not a method on AgentDeps: several call sites (this file's
    own callers, and a chunk of the existing test suite) pass a lighter duck-typed
    stand-in for AgentDeps (e.g. a SimpleNamespace) rather than a real instance,
    and this has to work for both without either one needing a get_surface method
    of its own.
    """
    surface = getattr(deps, "surface", None)
    if surface is not None:
        return surface
    from agent.surfaces.slack import SlackSurface

    surface = SlackSurface(
        deps.client, deps.channel_id, deps.thread_ts,
        getattr(deps, "message_ts", ""), getattr(deps, "user_token", None),
    )
    try:
        deps.surface = surface
    except Exception:
        pass
    return surface
