"""agent.mentions: coolton never pings a group of people."""
import asyncio
import importlib
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ToolCallPart

from agent.mentions import defuse_mass_mentions

agent_mod = importlib.import_module("agent.agent")


@pytest.mark.parametrize("text,defused", [
    ("hey <!subteam^S0123ABC> look", "hey @S0123ABC look"),
    ("<!subteam^S0123ABC|@team>", "@S0123ABC"),
    ("<!here> <!channel> <!everyone>", "@here @channel @everyone"),
    ("<!foo|bar>", "@foo|bar"),
    ("hi <@U0123> and <#C0123>", "hi <@U0123> and <#C0123>"),  # people and channels still link
])
def test_group_pings_become_plain_text(text, defused):
    assert defuse_mass_mentions(text) == defused


def test_a_reply_and_a_posting_tool_cant_ping_a_group(monkeypatch):
    assert agent_mod._redact_output(None, output_context=None, output="ok <!here>") == "ok @here"

    seen = {}

    async def handler(args):
        seen.update(args)
        return "sent"

    deps = SimpleNamespace(slack_calls=0, abuse_stop="")
    call = ToolCallPart("post_message_tool", {"channel": "C1", "text": "<!channel> hi"}, tool_call_id="c1")
    asyncio.run(agent_mod._enforce_slack_budget(SimpleNamespace(deps=deps), call=call,
                tool_def=SimpleNamespace(name="post_message_tool"), args=call.args, handler=handler))
    assert seen["text"] == "@channel hi"
