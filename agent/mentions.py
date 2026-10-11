"""Never let coolton ping a group of people.

Slack turns `<!here>`, `<!channel>`, `<!everyone>`, user-group mentions
(`<!subteam^S0123>`) and any other `<!...>` token into notifications. defuse_mass_mentions
rewrites every one into plain text that pings nobody: `<!subteam^S0123>` becomes
`@S0123`, and any other `<!X>` becomes `@X` (`<!here>` -> `@here`, `<!A|B>` -> `@A|B`).
Applied to everything coolton posts: her replies (agent.agent._redact_output), the
arguments of every tool that posts to Slack (agent.agent's tool hook, and
agent.tool_proxy for code_mode), and the plan block and status updates
(agent.plan_block).

A posting tool's arguments can ping a group two more ways, which defuse_args also
closes: Block Kit rich text's `broadcast` (@here/@channel/@everyone) and `usergroup`
elements become plain text, and `link_names` (which links a plain "@channel") is
dropped. Arguments that are JSON passed as a string (slack_api_call's
api_parameters, a blocks string) are parsed and defused the same way.
"""
import json
import re

_SUBTEAM_RE = re.compile(r"<!subteam\^([^>|]+)(?:\|[^>]*)?>")
_SPECIAL_RE = re.compile(r"<!([^>]+)>")


def defuse_mass_mentions(text: str) -> str:
    if "<!" not in text:
        return text
    return _SPECIAL_RE.sub(r"@\1", _SUBTEAM_RE.sub(r"@\1", text))


def _group_element_as_text(element: dict) -> dict:
    """A rich text element that pings a group, as plain text naming it."""
    if element.get("type") == "broadcast":
        return {"type": "text", "text": f"@{element.get('range') or 'here'}"}
    if element.get("type") == "usergroup":
        return {"type": "text", "text": f"@{element.get('usergroup_id') or 'group'}"}
    return element


def _defuse_string(text: str):
    if text.lstrip()[:1] in ("[", "{"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            defused = defuse_args(parsed)
            return text if defused == parsed else json.dumps(defused)
    return defuse_mass_mentions(text)


def defuse_args(value):
    """`value` (a tool's arguments) with nothing in it that can ping a group, recursively
    (see the module docstring)."""
    if isinstance(value, str):
        return _defuse_string(value)
    if isinstance(value, dict):
        value = _group_element_as_text(value)
        return {k: defuse_args(v) for k, v in value.items() if k != "link_names"}
    if isinstance(value, list):
        return [defuse_args(v) for v in value]
    return value
