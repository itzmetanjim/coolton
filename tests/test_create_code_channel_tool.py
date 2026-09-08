"""agent.agent.create_code_channel_tool — the @agent.tool wrapper around
agent.tools.code_channel.create_code_channel. Only concerned with the wrapper's
own logic (Slack-only gate, argument passthrough) — agent.tools.code_channel
itself is covered by tests/test_code_channel_tool.py.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic_ai import RunContext

agent_mod = importlib.import_module("agent.agent")


def _ctx(surface):
    deps = SimpleNamespace(
        client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", surface=surface,
    )
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def test_blocked_on_web_surface():
    web_surface = SimpleNamespace(name="web")
    result = agent_mod.create_code_channel_tool(_ctx(web_surface), name="My Code Channel")
    assert "Slack-only" in result or "not available" in result.lower()


def test_allowed_with_no_surface_set_default_slack(monkeypatch):
    # deps.surface is None for a real Slack turn until get_surface() lazily
    # builds one — the tool must still resolve to Slack, not block.
    called = {}
    monkeypatch.setattr(
        "agent.tools.code_channel.create_code_channel",
        lambda client, name, task, owner_id, source_channel_id, source_thread_ts: called.update(
            client=client, name=name, task=task, owner_id=owner_id,
            source_channel_id=source_channel_id, source_thread_ts=source_thread_ts,
        ) or "Created code channel <#C1>.",
    )
    result = agent_mod.create_code_channel_tool(_ctx(None), name="Code audit and bug detection", task="fix it")
    assert "Created code channel" in result
    assert called["name"] == "Code audit and bug detection"
    assert called["task"] == "fix it"
    assert called["owner_id"] == "U1"
    assert called["source_channel_id"] == "C1"
    assert called["source_thread_ts"] == "1.1"


def test_allowed_with_explicit_slack_surface(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.code_channel.create_code_channel",
        lambda *a, **k: "Created code channel <#C1>.",
    )
    slack_surface = SimpleNamespace(name="slack")
    result = agent_mod.create_code_channel_tool(_ctx(slack_surface), name="x")
    assert "Created code channel" in result


def test_excluded_from_code_mode():
    assert "create_code_channel_tool" in agent_mod.CODE_MODE_EXCLUDED_TOOLS
