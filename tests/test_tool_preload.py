"""agent.tool_preload: Jev picks which deferred tool groups to load up front.
It must never slow a turn past JEV_TIMEOUT_SECONDS or break it, and a Jev
that's down must be skipped outright (tracked in agent.fallback_cache)."""
import importlib
import time
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from agent import fallback_cache, provider_config
from agent import tool_preload as tp

agent_mod = importlib.import_module("agent.agent")

ENTRY = {"name": "hcai_jev", "model": "jev-latest", "url": "https://hcai.invalid/v1/jev/systemone",
         "api_key": "k", "display": "HCAI Jev", "provider": "hcai"}


def _jev_configured(monkeypatch, answers=None, delay=0.0, error=None):
    monkeypatch.setattr(provider_config, "build_jev_provider_order", lambda: [dict(ENTRY)])
    calls = []

    def fake_ask(entry, state, questions, timeout=tp.JEV_TIMEOUT_SECONDS):
        calls.append((state, questions))
        if delay:
            time.sleep(delay)
        if error:
            raise error
        return answers or {}

    monkeypatch.setattr(tp, "ask_jev", fake_ask)
    return calls


def test_groups_cover_exactly_the_deferred_function_tools():
    """Every deferred tool can be preloaded, and nothing else is listed."""
    assert tp.tools_for(set(tp.TOOL_GROUPS)) == agent_mod.DEFERRED_TOOLS


def test_loads_the_groups_jev_says_yes_to(monkeypatch):
    calls = _jev_configured(monkeypatch, answers={
        "diagrams": {"type": "noul", "noul": 0.93},
        "email": {"type": "noul", "noul": 0.2},
        "library_docs": {"type": "noul", "noul": 0.5},
    })

    groups = tp.collect_preloads(tp.start_preload("draw a flowchart of our deploy", history=[]))

    assert groups == {"diagrams", "library_docs"}
    state, questions = calls[0]
    assert state == {"message": "draw a flowchart of our deploy"}
    assert set(questions) == set(tp.TOOL_GROUPS) and questions["email"]["type"] == "noul"


def test_a_slow_jev_is_skipped_at_the_deadline_and_marked_down(monkeypatch):
    monkeypatch.setattr(tp, "JEV_TIMEOUT_SECONDS", 0.2)
    _jev_configured(monkeypatch, answers={"diagrams": {"noul": 1.0}}, delay=1.0)

    started = time.monotonic()
    groups = tp.collect_preloads(tp.start_preload("draw a diagram"))

    assert groups == set()
    assert time.monotonic() - started < 0.6
    assert "hcai_jev" in fallback_cache.get_dead_providers()


def test_a_failing_jev_is_skipped_and_marked_down(monkeypatch):
    _jev_configured(monkeypatch, error=RuntimeError("HTTP 502"))
    assert tp.collect_preloads(tp.start_preload("hi")) == set()
    assert "HTTP 502" in fallback_cache.get_dead_providers()["hcai_jev"]


def test_no_request_at_all_while_jev_is_down(monkeypatch):
    calls = _jev_configured(monkeypatch, answers={})
    fallback_cache.mark_dead("hcai_jev", "down")
    assert tp.start_preload("hi") is None
    assert calls == []


def test_no_request_while_hcai_as_a_whole_is_down(monkeypatch):
    calls = _jev_configured(monkeypatch, answers={})
    fallback_cache.mark_family_dead("hcai", "Daily spending limit reached")
    assert tp.start_preload("hi") is None
    assert calls == []


def test_no_jev_configured_means_nothing_to_wait_for(monkeypatch):
    monkeypatch.setattr(provider_config, "build_jev_provider_order", lambda: [])
    assert tp.start_preload("hi") is None
    assert tp.collect_preloads(None) == set()


def test_background_probe_brings_a_recovered_jev_back(monkeypatch):
    _jev_configured(monkeypatch, answers={"q": {"noul": 0.9}})
    fallback_cache.mark_dead("hcai_jev", "timed out earlier")
    tp.probe_jev()
    assert "hcai_jev" not in fallback_cache.get_dead_providers()


def test_background_probe_marks_a_broken_jev_down(monkeypatch):
    _jev_configured(monkeypatch, error=RuntimeError("HTTP 500"))
    tp.probe_jev()
    assert "hcai_jev" in fallback_cache.get_dead_providers()


def test_the_previous_reply_is_sent_as_context(monkeypatch):
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    calls = _jev_configured(monkeypatch, answers={})
    history = [ModelRequest(parts=[UserPromptPart(content="can you email bob?")]),
               ModelResponse(parts=[TextPart(content="want me to send it now?")])]
    tp.collect_preloads(tp.start_preload("yes", history))
    assert calls[0][0] == {"message": "yes", "coolton_previous_reply": "want me to send it now?"}


def test_a_text_only_response_reply_is_sent_as_context(monkeypatch):
    """A reply sent via the text_only_response output tool has no text part —
    without it Jev sees a bare "render it" with nothing to go on."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    calls = _jev_configured(monkeypatch, answers={})
    history = [ModelResponse(parts=[ToolCallPart(
        "text_only_response", {"emoji_name": "mermaid", "response": "here's a mermaid flowchart: ..."})])]
    tp.collect_preloads(tp.start_preload("render it duh", history))
    assert calls[0][0]["coolton_previous_reply"] == "here's a mermaid flowchart: ..."


def _run_turn(monkeypatch, preloaded, history=None):
    """run_agent with a real pydantic-ai run against a mock OpenAI-compatible
    endpoint (like HCAI), with the Slack plan block on; returns (the first wire
    request, the run's messages, deps)."""
    import json

    import httpx
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    import agent.plan_block as plan_block
    from agent.deps import AgentDeps
    from tests.test_deferred_tools import FakePlatform

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")
    monkeypatch.setattr(plan_block, "update_plan_message", lambda deps: None)
    monkeypatch.setattr(plan_block.thread_status, "set_status", lambda *a, **k: None)
    captured = {}
    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", lambda a, kw, deps, run_label=None: (
        captured.update(agent=a, kwargs=kw), (SimpleNamespace(output="ok", all_messages=lambda: []), "x"))[1])

    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0", platform=FakePlatform())
    deps.plan_ts = "9.9"
    if preloaded:
        done = Future()
        done.set_result(preloaded)
        deps.tool_preload = tp.PreloadRequest(done, time.monotonic(), "hcai_jev")
    agent_mod.run_agent("draw a flowchart", deps, message_history=history)

    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    model = OpenAIChatModel("m", provider=OpenAIProvider(base_url="https://x.invalid/v1", api_key="k",
                                                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    kwargs = {k: v for k, v in captured["kwargs"].items() if k not in ("model", "model_settings")}
    result = captured["agent"].run_sync(model=model, **kwargs)
    return sent[0], result.all_messages(), deps


def test_a_preload_is_coolton_s_own_search_run_for_it(monkeypatch):
    """Jev's picks become a search_tools call run before the first model request:
    the tool list and everything up to the user's message are byte-identical to
    an un-preloaded turn (so the shared prompt cache still hits), the model gets
    the tools' schemas for call_tool, and the plan block doesn't show it as a step."""
    import json

    plain, _, _ = _run_turn(monkeypatch, preloaded=None)
    request, _, deps = _run_turn(monkeypatch, preloaded={"diagrams"})

    assert request["tools"] == plain["tools"]
    assert request["messages"][:len(plain["messages"])] == plain["messages"]
    search, result = request["messages"][len(plain["messages"]):]
    assert search["tool_calls"][0]["function"]["name"] == "search_tools"
    [found] = json.loads(result["content"])["discovered_tools"]
    assert found["name"] == "render_mermaid_tool" and "diagram_code" in found["parameters"]["properties"]
    assert deps.plan_tasks == {}


def test_preloading_into_an_existing_thread_keeps_its_history(monkeypatch):
    from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart

    history = [ModelRequest(parts=[SystemPromptPart("SYSTEM"), UserPromptPart("hi")]), ModelResponse(parts=[TextPart("hey")])]
    request, _, _ = _run_turn(monkeypatch, preloaded={"diagrams"}, history=history)
    roles = [m["role"] for m in request["messages"] if m["role"] != "system"]
    assert roles == ["user", "assistant", "user", "assistant", "tool"]
    assert [m["content"] for m in request["messages"]].count("SYSTEM") == 1


def test_tools_already_defined_in_the_thread_are_not_loaded_again():
    """Reloading the Slack MCP tools every turn once added ~11k tokens of
    duplicate definitions per turn to one thread's history."""
    from pydantic_ai.messages import ModelRequest, ToolReturnPart

    def search_result(*tools):
        return ModelRequest(parts=[ToolReturnPart("search_tools", {"discovered_tools": list(tools)}, tool_call_id="c1")])

    history = [
        search_result({"name": "slack_create_canvas", "description": "", "parameters": {}}),
        # From before call_tool: no parameters, so the model doesn't actually have it.
        search_result({"name": "slack_read_canvas", "description": ""}),
    ]
    [response] = agent_mod._preload_search_call({"slack_canvases"}, history)
    assert response.parts[0].args == {"queries": ["slack_read_canvas", "slack_update_canvas"]}

    history.append(search_result(*({"name": n, "description": "", "parameters": {}}
                                   for n in ("slack_read_canvas", "slack_update_canvas"))))
    assert agent_mod._preload_search_call({"slack_canvases"}, history) is None


def test_mcp_groups_search_for_their_mcp_tools():
    [response] = agent_mod._preload_search_call({"library_docs"})
    assert response.parts[0].args == {"queries": ["query-docs", "resolve-library-id"]}
    assert agent_mod._preload_search_call(set()) is None


def test_jev_is_never_a_chat_model(isolated_config, monkeypatch):
    monkeypatch.setenv("P1_KEY", "k")
    isolated_config({
        "providers": [{"id": "p1", "api_url": "https://p1.example/v1", "api_key_env_var_name": "P1_KEY"}],
        "models": [
            {"provider": "p1", "model": "chat-m", "tags": ["chat"], "context_window": 100_000},
            {"provider": "p1", "model": "jev-latest", "tags": ["jev"], "kind": "jev", "api_path": "/jev/systemone"},
        ],
    })
    assert [c["model"] for _, c in provider_config.build_provider_order(None)] == ["chat-m"]
    assert "jev" not in provider_config.get_all_tags()
    [jev] = provider_config.build_jev_provider_order()
    assert jev["name"] == "p1_jev" and jev["url"] == "https://p1.example/v1/jev/systemone"
