"""Web adapter for the platform-independent agent runtime.

coolton on the web UI is the exact same agent as coolton on Slack: same tools,
same sandbox, same Slack MCP access. This adapter only owns the parts that are
genuinely about WHICH platform, not which conversation — the extra bit of
system prompt telling the model it's on the web, and the CURRENT CONTEXT block.
Everything genuinely Slack-shaped (the Slack MCP toolset, user-registered MCP
servers, display-name lookups) is delegated straight to SlackPlatform, since
none of that is about the current conversation — it's Slack itself, and stays
available on the web exactly as it is on Slack (deps.client is a real Slack
WebClient here too — see agent.deps.AgentDeps).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from agent.platform import PlatformAdapter
from agent.platforms.slack import SYSTEM_PROMPT as _SLACK_SYSTEM_PROMPT
from agent.platforms.slack import SlackPlatform

_WEB_PROMPT_PATH = Path(__file__).resolve().parent / "web_prompt.md"


def _load_web_prompt() -> str:
    return _WEB_PROMPT_PATH.read_text()


# Appended, not merged in — each platform's own prefix stays byte-stable across
# turns (see agent.platforms.slack's own note on this), so provider prompt
# caching still works per platform.
WEB_SYSTEM_PROMPT = _SLACK_SYSTEM_PROMPT + _load_web_prompt()


class WebPlatform(PlatformAdapter):
    name = "web"

    def __init__(self, client: Any = None):
        self.client = client
        self._slack = SlackPlatform(client)

    @property
    def system_prompt(self) -> str:
        return WEB_SYSTEM_PROMPT

    def format_user_message(self, text: str, deps: Any) -> str:
        # Same sender-tag format Slack uses (see system_prompt.md's MESSAGE FORMAT
        # section) — deps.user_id is a real Slack user id (from Hack Club Auth's
        # slack_id scope), so the same users.info lookup resolves a real name.
        return self._slack.format_user_message(text, deps)

    def build_context_prompt(self, deps: Any) -> str:
        return f"""\n## CURRENT CONTEXT
- You are in a conversation on coolton's web UI (coolton.tanjim.org). Conversation id: `{deps.thread_ts}`
- Your user_id (the HUMAN who messaged you): `{deps.user_id}`
- Your own bot user id (this is YOU, not a third party): `{os.environ.get("COOLTON_BOT_ID", "")}`
- Your cooltonUser helper account id (acts on your behalf): `{os.environ.get("COOLTON_USER_ID", "")}`
"""

    def build_turn_context(self, deps: Any, model: str, is_vision: bool) -> str:
        return self._slack.build_turn_context(deps, model, is_vision)

    def toolsets(self, deps: Any) -> list[Any]:
        return self._slack.toolsets(deps)
