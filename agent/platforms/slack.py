"""Slack adapter for the platform-independent agent runtime."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport
from pydantic_ai.toolsets import WrapperToolset

from agent.deferred_tools import HiddenToolset
from agent.platform import PlatformAdapter

logger = logging.getLogger(__name__)

# channel_id -> (looked up at, description); a channel's name and privacy rarely change.
_CHANNEL_DESCRIPTIONS: dict[str, tuple[float, str]] = {}
_MENTIONED_CHANNELS: dict[str, tuple[float, str]] = {}
_CHANNEL_DESCRIPTION_TTL = 600
_CHANNEL_MENTION = re.compile(r"<#([CG][A-Z0-9]{6,})(?:\|[^>]*)?>")


def describe_conversation(channel: dict) -> str:
    """What kind of conversation a conversations.info `channel` is, with its name."""
    if channel.get("is_im"):
        return "a DM"
    if channel.get("is_mpim"):
        return "a group DM"
    kind = "private" if channel.get("is_private") else "public"
    return f"a {kind} channel" + (f" (#{channel['name']})" if channel.get("name") else "")

# The prompt itself lives in system_prompt.md (next to this file) rather than as an
# inline f-string — kept as one plain markdown document instead of fragmented across
# Python string literals (backslash line-continuations, escaped braces to keep code
# examples literal, etc). Loaded once at import time; the two `${...}` placeholders
# below are its only per-deployment substitution.
#
# NOTE: moving it here fixed a real bug, not just cosmetics — the old inline f-string
# nested a Python `\n` inside an ASCII-art code EXAMPLE meant to be shown literally to
# the model; the outer f-string's own escape processing silently turned that `\n` into
# a real embedded newline, corrupting the example. Plain text has no such double
# interpretation. Otherwise keep this file's content stable turn to turn — an actually
# changed system prompt breaks provider-side prompt caching for every open thread.

_SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"


def _load_system_prompt() -> str:
    text = _SYSTEM_PROMPT_PATH.read_text()
    text = text.replace("${COOLTON_BOT_ID}", os.environ.get("COOLTON_BOT_ID", ""))
    text = text.replace("${COOLTON_USER_ID}", os.environ.get("COOLTON_USER_ID", ""))
    from agent.mention_ids import CODE_CHANNEL_BOT_ID
    text = text.replace("${COOLTON_CODE_CHANNEL_BOT_ID}", CODE_CHANNEL_BOT_ID)
    return text


SYSTEM_PROMPT = _load_system_prompt()


CONTEXT7_MCP_URL = "https://mcp.context7.com/mcp"


@dataclass
class ResilientToolset(WrapperToolset[Any]):
    """A toolset that can't fail the turn: if the wrapped one (a third-party
    MCP server, like Context7) can't connect or list its tools, it logs and
    offers no tools instead of raising. Without this, an outage of a server
    every turn loads would break every turn."""

    label: str = "toolset"
    _failed: bool = field(default=False, repr=False)

    async def __aenter__(self):
        self._failed = False
        try:
            await super().__aenter__()
        except Exception:
            self._failed = True
            logger.warning("%s unavailable this turn (connect failed); continuing without it", self.label, exc_info=True)
        return self

    async def __aexit__(self, *args):
        if self._failed:
            return None
        try:
            return await super().__aexit__(*args)
        except Exception:
            logger.warning("%s failed to disconnect cleanly", self.label, exc_info=True)
            return None

    async def get_tools(self, ctx):
        if self._failed:
            return {}
        try:
            return await super().get_tools(ctx)
        except Exception:
            self._failed = True
            logger.warning("%s unavailable this turn (listing tools failed); continuing without it", self.label, exc_info=True)
            return {}


def _context7_toolset():
    """Context7 (up-to-date library/framework docs), built in for everyone.
    Works without a key (rate-limited); CONTEXT7_API_KEY raises the limit."""
    key = os.environ.get("CONTEXT7_API_KEY", "")
    headers = {"CONTEXT7_API_KEY": key} if key else {}
    transport = StreamableHttpTransport(CONTEXT7_MCP_URL, headers=headers)
    return HiddenToolset(ResilientToolset(MCPToolset(transport, id="context7"), label="Context7 MCP"))


class SlackPlatform(PlatformAdapter):
    name = "slack"

    def __init__(self, client: Any = None):
        self.client = client

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def format_user_message(self, text: str, deps: Any) -> str:
        uid = deps.user_id or "unknown"
        name = self.display_name(uid)
        return f"{uid} ({name}):\n{text}"

    def display_name(self, user_id: str) -> str:
        if not user_id or not self.client:
            return user_id or "unknown"
        try:
            resp = self.client.users_info(user=user_id)
            if resp.get("ok"):
                profile = resp["user"].get("profile", {})
                return profile.get("display_name") or profile.get("real_name") or resp["user"].get("name") or user_id
        except Exception:
            pass
        return user_id

    def _conversation_description(self, channel_id: str) -> str:
        cached = _CHANNEL_DESCRIPTIONS.get(channel_id)
        if cached and time.time() - cached[0] < _CHANNEL_DESCRIPTION_TTL:
            return cached[1]
        try:
            description = describe_conversation(self.client.conversations_info(channel=channel_id)["channel"])
        except Exception:
            return ""
        _CHANNEL_DESCRIPTIONS[channel_id] = (time.time(), description)
        return description

    def _mentioned_channel(self, channel_id: str) -> str:
        """Whether coolton can read a channel the message mentions, by the same rule the
        Slack read tools apply (agent.tools.slack_search.assert_readable_channel). Seen
        live: given a bare <#C...> with no name, she said a public channel was private
        without trying to read it. "" when Slack can't say right now."""
        cached = _MENTIONED_CHANNELS.get(channel_id)
        if cached and time.time() - cached[0] < _CHANNEL_DESCRIPTION_TTL:
            return cached[1]
        from slack_sdk.errors import SlackApiError

        try:
            channel = self.client.conversations_info(channel=channel_id)["channel"]
        except SlackApiError as e:
            if e.response.get("error") != "channel_not_found":
                return ""
            description = "private (or doesn't exist), so you can't read it"
        except Exception:
            return ""
        else:
            if any(channel.get(k) for k in ("is_private", "is_im", "is_mpim", "is_org_shared")):
                # No name: the person asking may not be in it.
                description = "private, so you can't read it"
            else:
                name = f" (#{channel['name']})" if channel.get("name") else ""
                description = f"a public channel{name}, so you can read it"
        _MENTIONED_CHANNELS[channel_id] = (time.time(), description)
        return description

    def _mentioned_channels_note(self, deps: Any) -> str:
        mentioned = dict.fromkeys(_CHANNEL_MENTION.findall(getattr(deps, "request_text", "") or ""))
        mentioned.pop(deps.channel_id, None)
        described = [f"<#{c}> is {d}" for c in list(mentioned)[:5] if (d := self._mentioned_channel(c))]
        return f"\n- Channels mentioned in the message: {'; '.join(described)}" if described else ""

    def build_context_prompt(self, deps: Any) -> str:
        # Varies by thread and sender, so run_agent puts it in the user prompt,
        # never the system prompt (which must be identical for every request
        # to share one cached prefix across all threads).
        from agent.mention_ids import CODE_CHANNEL_BOT_ID

        code_channel_note = ""
        if not deps.thread_ts:
            from agent.code_channel_store import is_code_channel
            if is_code_channel(deps.channel_id):
                code_channel_note = (
                    "\n- This channel is a CODE CHANNEL, and you're at its channel level "
                    "(no thread_ts): the ENTIRE channel is ONE ongoing conversation with "
                    "you. Every message posted directly in the channel (not inside a "
                    "thread) is addressed to you — reply at channel level (thread_ts \"\"), "
                    "never start a thread for your reply. A thread started inside this "
                    "channel is a separate, normal conversation — mention-gated like any "
                    "other Slack thread. Load the `code-channels` skill before using this "
                    "channel's tools (tabs, canvas, context bar, slash commands) if you haven't "
                    "in this conversation yet."
                )
        description = self._conversation_description(deps.channel_id) if self.client else ""
        kind = f"\n- This conversation is {description}" if description else ""
        if self.client:
            kind += self._mentioned_channels_note(deps)
        return f"""\n## CURRENT CONTEXT
- You are in channel_id: `{deps.channel_id}` (thread_ts: `{deps.thread_ts}` if in thread, else DM){kind}
- Use this channel_id for operations in the current channel unless user specifies otherwise
- Your user_id (the HUMAN who messaged you): `{deps.user_id}`
- Your own bot user id (this is YOU, not a third party): `{os.environ.get("COOLTON_BOT_ID", "")}`
- Your code channel bot's user id (also YOU; a mention of it is a mention of you): `{CODE_CHANNEL_BOT_ID}`
- Your cooltonUser helper account id (acts on your behalf): `{os.environ.get("COOLTON_USER_ID", "")}`{code_channel_note}
"""

    def build_turn_context(self, deps: Any, model: str, is_vision: bool) -> str:
        # Changes on every turn (message_ts is unique per message; model/capability
        # can shift turn to turn with the provider fallback chain) — deliberately
        # NOT part of the system prompt, which must stay byte-identical across
        # turns for prompt caching to actually cache anything.
        capability = (
            "VISION-capable: attached images are visible to you, and you can call `see_image_from_sandbox` to view images in your sandbox."
            if is_vision else
            "NOT vision-capable: you cannot see images directly; download them to your sandbox and use `analyze_image`."
        )
        return f"""[Turn context — message timestamp: `{deps.message_ts}`, model: {model or "unknown"} ({capability})]

"""

    def toolsets(self, deps: Any) -> list[Any]:
        toolsets: list[Any] = []

        token = deps.user_token or os.environ.get("SLACK_USER_TOKEN")
        if not token:
            logger.info("Slack MCP Server disabled (no user_token)")
            from agent.admin_alerts import notify_admin
            notify_admin(
                "🔴 Slack MCP Server has no token configured (SLACK_USER_TOKEN unset) — "
                "most of coolton's Slack tools are unavailable this turn.",
                dedupe_key="mcp_no_token", min_interval_seconds=1800,
            )
        else:
            logger.info("Slack MCP Server enabled (user_token present)")
            try:
                transport = StreamableHttpTransport(
                    "https://mcp.slack.com/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                )
                # Same read and attribution rules as coolton's own Slack tools
                # (see agent/slack_mcp_guard.py).
                from agent.slack_mcp_guard import GuardedSlackMCPToolset
                toolsets.append(HiddenToolset(GuardedSlackMCPToolset(MCPToolset(transport))))
            except Exception as e:
                logger.exception("Failed to create MCP server")
                from agent.admin_alerts import notify_admin
                notify_admin(
                    f"🔴 Slack MCP Server toolset failed to construct: {e} — "
                    "most of coolton's Slack tools are unavailable this turn.",
                    dedupe_key="mcp_construct_error", min_interval_seconds=1800,
                )

        toolsets.append(_context7_toolset())
        toolsets.extend(self._user_mcp_toolsets(getattr(deps, "user_id", None)))
        return toolsets

    def _user_mcp_toolsets(self, user_id: str | None) -> list[Any]:
        """Any MCP servers this user registered from App Home (see
        agent/mcp_server_store.py). One broken server is dropped, not fatal —
        it never blocks the Slack MCP toolset or another user's servers."""
        if not user_id:
            return []
        try:
            from agent.mcp_server_store import get_server_decrypted, get_user_servers
            servers = get_user_servers(user_id)
        except Exception:
            logger.exception("Failed to load user MCP servers for %s", user_id)
            return []

        result: list[Any] = []
        for meta in servers:
            server = get_server_decrypted(user_id, meta["id"])
            if not server:
                continue
            try:
                from agent.mcp_server_store import build_transport
                transport = build_transport(server["url"], server.get("transport", "http"), server.get("token"),
                                            server.get("headers"))
                result.append(HiddenToolset(MCPToolset(transport, id=f"user_mcp_{server['id']}")))
            except Exception:
                logger.exception("Failed to build user MCP toolset %s (%s) for %s", server["id"], server["name"], user_id)
        return result
