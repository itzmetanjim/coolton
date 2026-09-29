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
    endpoint (like HCAI); returns (the first wire request, the run's messages)."""
    import json

    import httpx
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from agent.deps import AgentDeps
    from tests.test_deferred_tools import FakePlatform

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")
    captured = {}
    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", lambda a, kw, deps, run_label=None: (
        captured.update(agent=a, kwargs=kw), (SimpleNamespace(output="ok", all_messages=lambda: []), "x"))[1])

    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0", platform=FakePlatform())
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
    kwargs = {k: v for k, v in captured["kwargs"].items() if k not in ("model", "capabilities", "model_settings")}
    result = captured["agent"].run_sync(model=model, **kwargs)
    return sent[0], result.all_messages()


def test_preloaded_tools_arrive_exactly_as_if_coolton_searched_for_them(monkeypatch):
    """Jev's picks go in as a search_tools exchange after the user's message —
    the same thing coolton's own search adds — so the system prompt and the
    request up to this turn stay identical to an un-preloaded turn."""
    plain, _ = _run_turn(monkeypatch, preloaded=None)
    request, messages = _run_turn(monkeypatch, preloaded={"diagrams"})

    tools = [t["function"]["name"] for t in request["tools"]]
    assert "render_mermaid_tool" in tools and "agentmail_send_email" not in tools
    assert request["messages"][0] == plain["messages"][0]  # system prompt, unchanged
    roles = [m["role"] for m in request["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert request["messages"][1] == plain["messages"][1]  # the user's message, unchanged
    assert request["messages"][2]["tool_calls"][0]["function"]["name"] == "search_tools"
    assert "render_mermaid_tool" in request["messages"][3]["content"]
    # Stored like a real search, so the next turn keeps the tool loaded.
    follow_up, _ = _run_turn(monkeypatch, preloaded=None, history=messages)
    assert "render_mermaid_tool" in [t["function"]["name"] for t in follow_up["tools"]]


def test_preloading_into_an_existing_thread_keeps_its_history(monkeypatch):
    from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart

    history = [ModelRequest(parts=[SystemPromptPart("SYSTEM"), UserPromptPart("hi")]), ModelResponse(parts=[TextPart("hey")])]
    request, _ = _run_turn(monkeypatch, preloaded={"diagrams"}, history=history)
    assert [m["role"] for m in request["messages"]] == ["system", "user", "assistant", "user", "assistant", "tool"]
    assert sum(m["role"] == "system" for m in request["messages"]) == 1


def test_mcp_groups_reveal_their_mcp_tools():
    exchange = agent_mod._preload_search_exchange({"library_docs"})
    names = [m["name"] for m in exchange[1].parts[0].content["discovered_tools"]]
    assert names == ["query-docs", "resolve-library-id"]
    assert agent_mod._preload_search_exchange(set()) is None


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
