"""A turn may make at most SLACK_CALL_BUDGET Slack tool calls, counted on
both tool paths: pydantic-ai tool calls and code_mode's tool proxy."""
import importlib
import time
import uuid
from types import SimpleNamespace

import requests
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

import agent.tool_proxy as tool_proxy
from agent import slack_budget

agent_mod = importlib.import_module("agent.agent")


def test_only_slack_tools_count_and_call_201_is_refused():
    deps = SimpleNamespace(slack_calls=0)
    assert slack_budget.spend(deps, "run_linux_command") is None
    assert deps.slack_calls == 0
    for _ in range(slack_budget.SLACK_CALL_BUDGET):
        assert slack_budget.spend(deps, "read_conversation_history_tool") is None
    assert slack_budget.spend(deps, "slack_read_channel") == slack_budget.OVER_BUDGET_ERROR  # MCP tools count too


def test_hook_refuses_an_over_budget_slack_tool_without_running_it():
    ran = []

    def search_slack_tool(query: str) -> str:
        ran.append(query)
        return "results"

    def model(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("search_slack_tool", {"query": "x"})])
        return ModelResponse(parts=[TextPart("done")])

    deps = SimpleNamespace(slack_calls=slack_budget.SLACK_CALL_BUDGET)
    agent = Agent(FunctionModel(model), deps_type=SimpleNamespace, tools=[search_slack_tool], capabilities=[agent_mod._hooks])
    result = agent.run_sync("go", deps=deps)

    assert ran == []
    returns = [p for m in result.all_messages() for p in m.parts if isinstance(p, ToolReturnPart)]
    assert returns[0].content == slack_budget.OVER_BUDGET_ERROR


def test_code_mode_tool_proxy_enforces_the_same_budget():
    tool_proxy.start()
    base = f"http://{tool_proxy.LISTEN_HOST}:{tool_proxy.LISTEN_PORT}{tool_proxy.URL_PREFIX}"
    for _ in range(50):
        try:
            requests.post(f"{base}/nonexistent/nonexistent", timeout=1)
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.05)

    token, sandbox_id = uuid.uuid4().hex, uuid.uuid4().hex
    deps = SimpleNamespace(slack_calls=slack_budget.SLACK_CALL_BUDGET)
    tool_proxy.register_sandbox(sandbox_id, token, deps, lambda name: (lambda ctx, *a, **k: "read"), ["read_conversation_history_tool"])
    try:
        resp = requests.post(
            f"{base}/{sandbox_id}/read_conversation_history_tool",
            json={"args": [], "kwargs": {}}, headers={"Authorization": f"Bearer {token}"}, timeout=5,
        ).json()
    finally:
        tool_proxy.unregister_sandbox(sandbox_id)
    assert resp == {"ok": False, "error": slack_budget.OVER_BUDGET_ERROR}
