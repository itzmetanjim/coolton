"""Never let coolton ping a group of people.

Slack turns `<!here>`, `<!channel>`, `<!everyone>`, user-group mentions
(`<!subteam^S0123>`) and any other `<!...>` token into notifications. defuse_mass_mentions
rewrites every one into plain text that pings nobody: `<!subteam^S0123>` becomes
`@S0123`, and any other `<!X>` becomes `@X` (`<!here>` -> `@here`, `<!A|B>` -> `@A|B`).
Applied to everything coolton posts: her replies (agent.agent._redact_output), the
arguments of every tool that posts to Slack (agent.agent's tool hook, and
agent.tool_proxy for code_mode), and the plan block and status updates
(agent.plan_block).
"""
import re

_SUBTEAM_RE = re.compile(r"<!subteam\^([^>|]+)(?:\|[^>]*)?>")
_SPECIAL_RE = re.compile(r"<!([^>]+)>")


def defuse_mass_mentions(text: str) -> str:
    if "<!" not in text:
        return text
    return _SPECIAL_RE.sub(r"@\1", _SUBTEAM_RE.sub(r"@\1", text))


def defuse_args(value):
    """`value` (a tool's arguments) with every string in it defused, recursively."""
    if isinstance(value, str):
        return defuse_mass_mentions(value)
    if isinstance(value, dict):
        return {k: defuse_args(v) for k, v in value.items()}
    if isinstance(value, list):
        return [defuse_args(v) for v in value]
    return value
