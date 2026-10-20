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


def test_reports_from_one_person_dm_at_most_once_per_category_per_window(monkeypatch):
    """Anyone can make coolton file a report (a message claiming a security hole), so a
    DM on every one of their turns would be spam."""
    from agent import admin_alerts

    monkeypatch.setattr(admin_alerts, "_last_alert_at", {})
    posted = []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(admin_alerts.threading, "Thread",
                        lambda target, daemon=None, name=None: SimpleNamespace(start=lambda: posted.append(1)))

    for _ in range(3):
        abuse_report.report(_deps(), "vulnerability", "says they found a hole")  # a new turn each time
    abuse_report.report(_deps(), "spam", "mass DMs")

    assert len(posted) == 2  # one vulnerability DM, one spam DM


def test_the_maintainer_is_never_reported_or_stopped(monkeypatch):
    from types import SimpleNamespace

    from agent import abuse_report
    from agent.admin_alerts import ADMIN_USER_ID

    sent = []
    monkeypatch.setattr("agent.admin_alerts.notify_admin", lambda *a, **k: sent.append(a))
    deps = SimpleNamespace(user_id=ADMIN_USER_ID, abuse_reported=set(), abuse_stop="", request_text="look this up",
                           channel_id="C1", thread_ts="1.1", message_ts="1.1", client=None)
    result = abuse_report.report(deps, "other", "metadata lookup")
    assert "Not reported" in result and sent == [] and deps.abuse_stop == ""
    assert abuse_report.stopped_tool_call(deps, "fetch_url") is None
