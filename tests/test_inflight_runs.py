import json

import pytest

from agent import inflight_runs


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(inflight_runs, "STORE_PATH", tmp_path / "inflight_runs.json")


def test_record_start_then_pop_all_returns_it():
    inflight_runs.record_start("C1", "1.1", message_ts="111.111", user_id="U1", text="hi", is_slack=True)
    entries = inflight_runs.pop_all()
    assert entries == [
        {"channel_id": "C1", "thread_ts": "1.1", "message_ts": "111.111", "user_id": "U1", "text": "hi", "is_slack": True},
    ]


def test_pop_all_clears_the_store():
    inflight_runs.record_start("C1", "1.1", message_ts="111.111", user_id="U1", text="hi", is_slack=True)
    inflight_runs.pop_all()
    assert inflight_runs.pop_all() == []


def test_record_finished_removes_the_entry():
    inflight_runs.record_start("C1", "1.1", message_ts="111.111", user_id="U1", text="hi", is_slack=True)
    inflight_runs.record_finished("C1", "1.1")
    assert inflight_runs.pop_all() == []


def test_record_finished_is_a_noop_for_an_unknown_entry():
    inflight_runs.record_finished("nope", "nope")
    assert inflight_runs.pop_all() == []


def test_a_normal_start_then_finish_leaves_nothing_orphaned():
    """The happy path: a run that completes normally clears its own record —
    resume_orphaned_runs should never see it."""
    inflight_runs.record_start("web", "conv1", message_ts="5", user_id="U1", text="hi", is_slack=False)
    inflight_runs.record_finished("web", "conv1")
    assert inflight_runs.pop_all() == []


def test_multiple_entries_are_tracked_independently():
    inflight_runs.record_start("C1", "1.1", message_ts="1", user_id="U1", text="a", is_slack=True)
    inflight_runs.record_start("web", "conv1", message_ts="2", user_id="U2", text="b", is_slack=False)
    inflight_runs.record_finished("C1", "1.1")
    entries = inflight_runs.pop_all()
    assert len(entries) == 1
    assert entries[0]["channel_id"] == "web"


def test_corrupt_json_is_tolerated(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.json"
    path.write_text("not json")
    monkeypatch.setattr(inflight_runs, "STORE_PATH", path)
    assert inflight_runs.pop_all() == []


def test_non_dict_json_is_tolerated(tmp_path, monkeypatch):
    path = tmp_path / "nondict.json"
    path.write_text(json.dumps([1, 2, 3]))
    monkeypatch.setattr(inflight_runs, "STORE_PATH", path)
    assert inflight_runs.pop_all() == []


def test_missing_file_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.setattr(inflight_runs, "STORE_PATH", tmp_path / "does-not-exist.json")
    assert inflight_runs.pop_all() == []


def test_a_restarted_turn_continues_from_its_checkpoint_instead_of_starting_over(monkeypatch):
    """A real run calls a tool, then the process dies on its next model request. The
    resumed turn continues from the completed tool round: the tool isn't run again, the
    user's message isn't repeated, and the model is told it restarted."""
    import importlib
    import json as _json
    from types import SimpleNamespace
    from unittest.mock import Mock

    import httpx
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from agent.deps import AgentDeps
    from tests.test_deferred_tools import FakePlatform

    agent_mod = importlib.import_module("agent.agent")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")
    from pydantic_ai import RunContext

    clock_reads = []

    def get_datetime(ctx: RunContext) -> str:
        clock_reads.append(1)
        return "Friday, 2026-10-02 14:00:00 UTC"

    monkeypatch.setattr(agent_mod.agent._function_toolset.tools["get_datetime"], "function", get_datetime)

    def run(resume_from, script):
        captured, sent = {}, []
        monkeypatch.setattr(agent_mod, "_run_with_provider_chain", lambda a, kw, deps, run_label=None: (
            captured.update(agent=a, kwargs=kw), (SimpleNamespace(output="ok", all_messages=lambda: []), "x"))[1])
        deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0", platform=FakePlatform())
        agent_mod.run_agent("what time is it", deps, message_history=[], resume_from=resume_from)

        def handler(request):
            sent.append(_json.loads(request.content))
            reply = script[len(sent) - 1]
            if reply is None:
                raise httpx.ConnectError("process killed")
            return httpx.Response(200, json=reply)

        model = OpenAIChatModel("m", provider=OpenAIProvider(base_url="https://x.invalid/v1", api_key="k",
                                                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
        kwargs = {k: v for k, v in captured["kwargs"].items() if k not in ("model", "model_settings")}
        try:
            captured["agent"].run_sync(model=model, **kwargs)
        except Exception:
            pass
        return sent

    def reply(content=None, tool=None):
        message = {"role": "assistant", "content": content}
        if tool:
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": tool, "arguments": "{}"}}]
        return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                "choices": [{"index": 0, "finish_reason": "tool_calls" if tool else "stop", "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    inflight_runs.record_start("C1", "1.1", message_ts="1.0", user_id="U1", text="what time is it", is_slack=True)
    run(None, [reply(tool="get_datetime"), None])  # dies on the request after the tool ran
    checkpoint = inflight_runs.load_checkpoint("C1", "1.1")
    assert clock_reads == [1] and checkpoint

    sent = run(checkpoint, [reply(content="it's 14:00 UTC")])

    messages = sent[0]["messages"]
    assert clock_reads == [1]  # not run again
    assert [m["role"] for m in messages if m["role"] != "system"] == ["user", "assistant", "tool", "user"]
    assert agent_mod.RESTART_NOTE in messages[-1]["content"]
    assert sum("what time is it" in str(m.get("content")) for m in messages) == 1

    inflight_runs.record_finished("C1", "1.1")
    assert inflight_runs.load_checkpoint("C1", "1.1") is None


def test_only_turns_recorded_as_in_flight_get_checkpoints():
    inflight_runs.save_checkpoint("C9", "9.9", [])  # a subagent or test run: nothing recorded
    assert inflight_runs.load_checkpoint("C9", "9.9") is None
