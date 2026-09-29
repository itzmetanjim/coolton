"""Rarely-used tools (and MCP servers) stay out of the tool list: the model
finds them with search_tools and runs them with call_tool, so the tool list —
and the cached prompt prefix — is identical on every request."""
import asyncio
import importlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.toolsets import FunctionToolset, WrapperToolset
from pydantic_ai.usage import RunUsage

from agent.deferred_tools import HiddenToolset, call_tool, search_tools
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
                 "read_conversation_history_tool", "post_message_tool", "get_datetime",
                 "search_tools", "call_tool"):
        assert name not in agent_mod.DEFERRED_TOOLS


def _chat(content=None, tool_calls=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": f"call_{name}_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(tool_calls)]
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if tool_calls else "stop", "message": message}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


def test_searching_and_calling_a_deferred_tool_never_changes_the_tool_list(monkeypatch, caplog):
    """Through a real run against an OpenAI-compatible endpoint (like HCAI): the
    model searches, calls render_mermaid_tool via call_tool, and every request
    carries the same tools. The plan block/logs show the real tool, not call_tool."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")
    rendered = []

    def render_mermaid_tool(ctx: RunContext, diagram_code: str, theme: str = "default") -> str:
        """Render a Mermaid diagram."""
        rendered.append(diagram_code)
        return "Diagram rendered and posted to the thread"

    monkeypatch.setattr(agent_mod.agent._function_toolset.tools["render_mermaid_tool"], "function", render_mermaid_tool)
    captured = {}
    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", lambda a, kw, deps, run_label=None: (
        captured.update(agent=a, kwargs=kw), (SimpleNamespace(output="ok", all_messages=lambda: []), "x"))[1])
    from agent.deps import AgentDeps
    import agent.plan_block as plan_block
    monkeypatch.setattr(plan_block, "update_plan_message", lambda deps: None)
    monkeypatch.setattr(plan_block.thread_status, "set_status", lambda *a, **k: None)
    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0", platform=FakePlatform())
    deps.plan_ts = "9.9"  # turns on the Slack plan block ("thinking" display)
    agent_mod.run_agent("draw a flowchart", deps)

    script = [
        _chat(tool_calls=[("search_tools", {"queries": ["mermaid diagram"]})]),
        _chat(tool_calls=[("call_tool", {"name": "render_mermaid_tool", "arguments": '{"diagram_code": "graph TD; A-->B"}'})]),
        _chat(content="done"),
    ]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=script[len(sent) - 1])

    model = OpenAIChatModel("m", provider=OpenAIProvider(
        base_url="https://hcai.invalid/v1", api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    kwargs = {k: v for k, v in captured["kwargs"].items() if k not in ("model", "model_settings")}
    with caplog.at_level(logging.INFO):
        captured["agent"].run_sync(model=model, **kwargs)

    assert rendered == ["graph TD; A-->B"]
    tool_lists = [[t["function"]["name"] for t in request["tools"]] for request in sent]
    assert tool_lists[0] == tool_lists[1] == tool_lists[2]
    assert {"search_tools", "call_tool", "run_linux_command"} <= set(tool_lists[0])
    assert "render_mermaid_tool" not in tool_lists[0] and "agentmail_send_email" not in tool_lists[0]
    # The search result carries the schema the model needs for call_tool.
    search_result = json.loads(sent[1]["messages"][-1]["content"])
    assert search_result["discovered_tools"][0]["name"] == "render_mermaid_tool"
    assert "diagram_code" in search_result["discovered_tools"][0]["parameters"]["properties"]
    # The plan block and logs show render_mermaid_tool and its own arguments, as if called directly.
    titles = [t["title"] for t in deps.plan_tasks.values()]
    assert titles == [plan_block._display_for_tool("search_tools"), plan_block._display_for_tool("render_mermaid_tool")]
    assert "diagram_code" in str(list(deps.plan_tasks.values())[1]["output"])
    assert "TOOL INPUT  | render_mermaid_tool" in caplog.text and "| call_tool |" not in caplog.text


def _hidden_ctx(*functions):
    deps = SimpleNamespace(hidden_toolsets=[HiddenToolset(FunctionToolset(list(functions)))])
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage())


def test_call_tool_validates_arguments_and_names():
    def schedule_task(when: str, repeat: int = 1) -> str:
        """Schedule a recurring task."""
        return f"scheduled {when} x{repeat}"

    ctx = _hidden_ctx(schedule_task)
    assert asyncio.run(call_tool(ctx, "schedule_task", '{"when": "9am", "repeat": "3"}')) == "scheduled 9am x3"
    with pytest.raises(ModelRetry, match="Invalid arguments for schedule_task"):
        asyncio.run(call_tool(ctx, "schedule_task", '{"repeat": 2}'))
    with pytest.raises(ModelRetry, match="JSON object string"):
        asyncio.run(call_tool(ctx, "schedule_task", "when=9am"))
    with pytest.raises(ModelRetry, match="search_tools"):
        asyncio.run(call_tool(ctx, "schedule_tsak", '{"when": "9am"}'))


def test_search_tools_finds_by_exact_name_or_keywords_and_hides_them_from_the_list():
    def schedule_task(when: str) -> str:
        """Create a recurring scheduled task."""
        return ""

    def send_email(to: str) -> str:
        """Send an email from coolton's inbox."""
        return ""

    ctx = _hidden_ctx(schedule_task, send_email)

    def found(queries):
        return [t["name"] for t in asyncio.run(search_tools(ctx, queries))["discovered_tools"]]

    assert found(["send_email"]) == ["send_email"]
    assert found(["recurring task"]) == ["schedule_task"]
    assert found(["zebra"]) == []
    assert asyncio.run(ctx.deps.hidden_toolsets[0].get_tools(ctx)) == {}


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
