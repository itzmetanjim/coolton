"""agent.agent.huddlefm_request_control_tool / huddlefm_command_tool — the
@agent.tool wrappers around agent.tools.huddlefm. Only concerned with the
wrappers' own plumbing (argument passthrough, fields JSON parsing) —
agent.tools.huddlefm itself is covered by tests/test_huddlefm.py.
"""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic_ai import RunContext

agent_mod = importlib.import_module("agent.agent")


def _ctx():
    deps = SimpleNamespace(client=Mock())
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def test_request_control_tool_delegates(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "agent.tools.huddlefm.request_control",
        lambda client, channel, permissions, events: called.update(
            client=client, channel=channel, permissions=permissions, events=events,
        ) or "Requested control of C1 (add).",
    )
    ctx = _ctx()
    result = agent_mod.huddlefm_request_control_tool(ctx, channel="C1", permissions="add,skip", events="track")
    assert result == "Requested control of C1 (add)."
    assert called["client"] is ctx.deps.client
    assert called["channel"] == "C1"
    assert called["permissions"] == "add,skip"
    assert called["events"] == "track"


def test_command_tool_parses_fields_and_delegates(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "agent.tools.huddlefm.send_command",
        lambda client, command_type, channel, fields: called.update(
            client=client, command_type=command_type, channel=channel, fields=fields,
        ) or '{"ok":true}',
    )
    ctx = _ctx()
    result = agent_mod.huddlefm_command_tool(ctx, command_type="volume", channel="C1", fields='{"percent": 40}')
    assert result == '{"ok":true}'
    assert called["command_type"] == "volume"
    assert called["channel"] == "C1"
    assert called["fields"] == {"percent": 40}


def test_command_tool_rejects_invalid_fields_json():
    ctx = _ctx()
    result = agent_mod.huddlefm_command_tool(ctx, command_type="volume", fields="not json")
    assert "Error" in result
    assert "fields" in result


def test_command_tool_defaults_to_no_fields(monkeypatch):
    called = {}
    monkeypatch.setattr(
        "agent.tools.huddlefm.send_command",
        lambda client, command_type, channel, fields: called.update(fields=fields) or "ok",
    )
    ctx = _ctx()
    agent_mod.huddlefm_command_tool(ctx, command_type="status")
    assert called["fields"] == {}
