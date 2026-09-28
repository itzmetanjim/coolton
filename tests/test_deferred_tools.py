"""Rarely-used tools (and MCP servers) load on demand via search_tools instead
of being sent on every request — the full tool list was most of a ~46k-token
base prompt."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import FunctionToolset, WrapperToolset
from pydantic_ai.usage import RunUsage

from agent.platforms.slack import ResilientToolset

agent_mod = importlib.import_module("agent.agent")


class FakePlatform:
    name = "fake"
    system_prompt = "SYSTEM"

    def format_user_message(self, text, deps):
        return text

    def build_context_prompt(self, deps):
        return "context"

    def build_turn_context(self, deps, model, is_vision):
        return ""

    def toolsets(self, deps):
        return []


def test_every_deferred_name_is_a_real_tool():
    """A typo here would silently leave that tool always-loaded."""
    assert agent_mod.DEFERRED_TOOLS <= set(agent_mod.agent._function_toolset.tools)


def test_tools_most_turns_need_stay_loaded():
    for name in ("add_emoji_reaction", "send_message", "skip", "run_linux_command", "search_web_tool",
                 "read_conversation_history_tool", "post_message_tool", "get_datetime"):
        assert name not in agent_mod.DEFERRED_TOOLS


def test_run_agent_hides_deferred_tools_until_searched(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")
    captured = {}

    def fake_chain(agent_dynamic, run_kwargs, deps, run_label=None):
        captured["agent"], captured["kwargs"] = agent_dynamic, run_kwargs
        return SimpleNamespace(output="ok", all_messages=lambda: []), "anthropic"

    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", fake_chain)
    from agent.deps import AgentDeps
    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0", platform=FakePlatform())
    agent_mod.run_agent("hi", deps)

    seen = {}

    def model(messages, info: AgentInfo):
        seen["tools"] = {t.name for t in info.function_tools}
        return ModelResponse(parts=[TextPart("done")])

    kwargs = {k: v for k, v in captured["kwargs"].items() if k not in ("model", "capabilities", "model_settings")}
    captured["agent"].run_sync(model=FunctionModel(model), **kwargs)

    assert "render_mermaid_tool" not in seen["tools"]
    assert "agentmail_send_email" not in seen["tools"]
    assert "run_linux_command" in seen["tools"]


class _BrokenToolset(WrapperToolset):
    async def __aenter__(self):
        raise RuntimeError("Client failed to connect")


def test_resilient_toolset_offers_no_tools_when_its_server_is_down():
    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    async def tools_of(ts):
        async with ts:
            return await ts.get_tools(ctx)

    assert asyncio.run(tools_of(ResilientToolset(_BrokenToolset(FunctionToolset([])), label="down"))) == {}


def test_resilient_toolset_passes_tools_through_when_healthy():
    def resolve_library_id(name: str) -> str:
        return "id"

    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())

    async def tools_of(ts):
        async with ts:
            return await ts.get_tools(ctx)

    assert list(asyncio.run(tools_of(ResilientToolset(FunctionToolset([resolve_library_id]), label="ok")))) == ["resolve_library_id"]


@pytest.mark.parametrize("key,expected", [("", {}), ("secret", {"CONTEXT7_API_KEY": "secret"})])
def test_context7_sends_its_key_only_when_set(monkeypatch, key, expected):
    from agent.platforms import slack as slack_platform

    monkeypatch.setenv("CONTEXT7_API_KEY", key)
    captured = {}
    monkeypatch.setattr(slack_platform, "StreamableHttpTransport", lambda url, headers: captured.update(url=url, headers=headers))
    monkeypatch.setattr(slack_platform, "MCPToolset", lambda transport, **kw: SimpleNamespace())
    slack_platform._context7_toolset()
    assert captured == {"url": slack_platform.CONTEXT7_MCP_URL, "headers": expected}
