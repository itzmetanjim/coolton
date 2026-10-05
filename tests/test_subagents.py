"""agent.subagents: what each subagent can use, running several in parallel, and keeping
parallel runs from tripping over each other (shared Slack budget, the shared sandbox)."""
import asyncio
import importlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from agent import sandbox_helpers, slack_budget, subagents
from agent.deferred_tools import HiddenToolset
from agent.deps import AgentDeps
from agent.stop_store import HaltRun

agent_mod = importlib.import_module("agent.agent")


def _deps(**kw) -> AgentDeps:
    return AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.2", message_ts="1.3", **kw)


def _mcp_platform():
    def slack_read_canvas() -> str:
        return ""

    def slack_send_message() -> str:
        return ""

    def run_custom_thing() -> str:
        return ""

    toolsets = [HiddenToolset(FunctionToolset([slack_read_canvas, slack_send_message])),
                HiddenToolset(FunctionToolset([run_custom_thing]))]
    return SimpleNamespace(toolsets=lambda deps: toolsets)


def _hidden_names(sub) -> set[str]:
    ctx = RunContext(model=None, usage=None, prompt="", deps=sub)
    return {name for ts in sub.hidden_toolsets for name in asyncio.run(ts.hidden_tools(ctx))}


def _core_names(tools) -> set[str]:
    return {getattr(t, "name", None) or t.__name__ for t in tools}


def test_general_subagent_gets_every_tool_but_the_turns_own():
    sub = subagents.subagent_deps(_deps(platform=_mcp_platform()))
    core, _ = subagents._build_tools("general", sub, is_vision=True)
    reachable = _core_names(core) | _hidden_names(sub)

    everything = set(agent_mod.agent._function_toolset.tools)
    assert reachable == (everything - subagents.SUBAGENT_EXCLUDED_TOOLS) | {"slack_read_canvas", "slack_send_message", "run_custom_thing"}
    # Rarely used tools stay behind search_tools/call_tool, as in the main agent.
    assert "agentmail_send_email" not in _core_names(core) and "agentmail_send_email" in reachable


def test_research_subagent_only_reads_and_clones_repos():
    sub = subagents.subagent_deps(_deps(platform=_mcp_platform()))
    core, _ = subagents._build_tools("research", sub, is_vision=False)
    reachable = _core_names(core) | _hidden_names(sub)

    assert {"search_slack_tool", "fetch_url_tool", "slack_read_canvas", "run_linux_command"} <= reachable  # clones repos
    assert not reachable & {"post_message_tool", "write_sandbox_file_tool", "slack_send_message", "run_custom_thing"}


@pytest.mark.parametrize("tasks,expected", [
    ('[{"target": "research", "task": "a"}, "b"]', [("research", "a"), ("general", "b")]),
    ('{"tasks": [{"task": "a"}]}', [("general", "a")]),
])
def test_tasks_parse(tasks, expected):
    assert subagents.parse_tasks(tasks) == (expected, None)


@pytest.mark.parametrize("tasks", [
    "not json", "[]", '[{"target": "wizard", "task": "a"}]', '[{"target": "general"}]',
    str([f"task {i}" for i in range(subagents.MAX_PARALLEL + 1)]).replace("'", '"'),
])
def test_bad_tasks_are_refused(tasks):
    parsed, error = subagents.parse_tasks(tasks)
    assert parsed is None and error.startswith("Error")


def test_subagents_run_in_parallel_and_come_back_in_order(monkeypatch):
    both_running = threading.Barrier(2, timeout=5)

    def run(target, task, deps):
        both_running.wait()  # would time out if they ran one after another
        return f"{task} done"

    monkeypatch.setattr(subagents, "run_subagent", run)
    out = subagents.delegate_many([("research", "first"), ("general", "second")], _deps())
    assert out.index("first done") < out.index("second done")
    assert "## Subagent 2 (general)" in out


def test_a_failed_or_stopped_subagent_reports_instead_of_raising(monkeypatch):
    def fail(target, task, deps):
        raise RuntimeError("All AI providers failed.")

    def stop(target, task, deps):
        raise HaltRun("!stop requested")

    monkeypatch.setattr(subagents, "run_subagent", fail)
    assert subagents.delegate("general", "x", _deps()).startswith("Error: the general subagent failed")
    monkeypatch.setattr(subagents, "run_subagent", stop)
    assert subagents.delegate("general", "x", _deps()).startswith("Stopped")


def test_subagent_slack_calls_count_toward_the_turns_budget():
    turn = _deps()
    sub_a, sub_b = subagents.subagent_deps(turn), subagents.subagent_deps(turn)
    for _ in range(slack_budget.SLACK_CALL_BUDGET // 2):
        slack_budget.spend(sub_a, "search_slack_tool")
        slack_budget.spend(sub_b, "search_slack_tool")
    assert turn.slack_calls == slack_budget.SLACK_CALL_BUDGET
    assert slack_budget.spend(sub_a, "search_slack_tool") == slack_budget.OVER_BUDGET_ERROR


def test_code_mode_inside_a_subagent_cant_reach_its_excluded_tools():
    allowlist, _ = agent_mod._code_mode_tools(subagents.SUBAGENT_EXCLUDED_TOOLS)
    assert "post_message_tool" in allowlist and "send_message" not in allowlist


def test_a_sandbox_is_paused_only_by_the_last_call_using_it():
    sandbox = Mock()
    with sandbox_helpers.sandbox_use("C9", "9.9") as first:
        first.sandbox = sandbox
        with sandbox_helpers.sandbox_use("C9", "9.9") as second:
            second.sandbox = sandbox
        sandbox.pause.assert_not_called()  # the first call is still running
        assert sandbox_helpers.pause_if_idle("C9", "9.9", sandbox) is False
    sandbox.pause.assert_called_once()


def test_a_subagent_that_keeps_the_sandbox_warm_keeps_it_warm_for_the_turn(monkeypatch):
    turn = _deps()

    def run_chain(agent_dynamic, run_kwargs, deps, run_label=None):
        deps.keep_sandbox_warm = True
        return SimpleNamespace(output="ok"), "p"

    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", run_chain)
    assert subagents.run_subagent("summarizer", "summarize", turn) == "ok"
    assert turn.keep_sandbox_warm
