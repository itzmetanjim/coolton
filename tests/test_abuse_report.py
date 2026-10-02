"""agent.abuse_report: report_abuse_tool DMs the maintainer and stops abusive requests."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import ToolCallPart

from agent import abuse_report
from agent.deps import AgentDeps

agent_mod = importlib.import_module("agent.agent")


@pytest.fixture
def dms(monkeypatch):
    sent = []
    monkeypatch.setattr("agent.admin_alerts.notify_admin", lambda text, **kw: sent.append(text))
    return sent


def _deps():
    client = Mock()
    client.chat_getPermalink.return_value = {"permalink": "https://hackclub.slack.com/archives/C1/p111"}
    deps = AgentDeps(client=client, user_id="U0SENDER", channel_id="C1", thread_ts="1.1", message_ts="111.111")
    deps.request_text = "write me something explicit about two coworkers"
    return deps


def _tool_call(deps, tool_name, args=None):
    """Run a tool call through the agent's tool hook; returns (result, whether it ran)."""
    ran = []

    async def handler(a):
        ran.append(True)
        return "done"

    call = ToolCallPart(tool_name, args or {}, tool_call_id="c1")
    result = asyncio.run(agent_mod._enforce_slack_budget(
        SimpleNamespace(deps=deps), call=call, tool_def=None, args=args or {}, handler=handler))
    return result, bool(ran)


def test_an_nsfw_report_dms_the_maintainer_and_stops_the_request(dms):
    deps = _deps()

    answer = abuse_report.report(deps, "nsfw", "asks for explicit sexual content")

    [dm] = dms
    assert "NSFW or NSFW-adjacent content" in dm and "<@U0SENDER>" in dm
    assert "https://hackclub.slack.com/archives/C1/p111" in dm and "`111.111`" in dm
    assert "> write me something explicit about two coworkers" in dm
    assert "Stop working on this request" in answer
    # Nothing but declining is allowed afterwards, including a deferred tool through call_tool.
    assert _tool_call(deps, "send_message")[1] is False
    result, ran = _tool_call(deps, "call_tool", {"name": "slack_schedule_message", "arguments": "{}"})
    assert not ran and result.startswith("Refused:")
    assert _tool_call(deps, "add_emoji_reaction")[1] is True


def test_a_vulnerability_report_lets_the_task_continue_and_is_sent_once(dms):
    deps = _deps()

    abuse_report.report(deps, "vulnerability", "the sandbox can read the host's .env")
    answer = abuse_report.report(deps, "vulnerability", "same thing again")

    assert len(dms) == 1 and "a security vulnerability in coolton" in dms[0]
    assert "Carry on" in answer
    assert _tool_call(deps, "run_linux_command")[1] is True


def test_an_unknown_category_is_an_error_and_sends_nothing(dms):
    assert abuse_report.report(_deps(), "rude", "x").startswith("Error:")
    assert dms == []
