"""ask_human_for_challenge_tool: control of the desktop goes to the requester only, and never
outlasts what it was given for."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic_ai import RunContext

from agent.tools import human_control


def _fake_desktop(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.sandbox_helpers.get_or_create_sandbox", lambda c, t: ("sandbox", "proxy"))
    monkeypatch.setattr("agent.desktop_helpers.start_control", lambda s, p, lifetime_seconds=0: calls.append("start") or "https://desk/vnc.html?password=x")
    monkeypatch.setattr("agent.desktop_helpers.stop_control", lambda s, p: calls.append("stop"))
    return calls


def test_the_control_link_goes_only_to_the_requester_and_finished_turns_it_off(monkeypatch):
    agent_mod = importlib.import_module("agent.agent")
    calls = _fake_desktop(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: None)
    client = Mock()
    deps = SimpleNamespace(client=client, channel_id="C1", thread_ts="1.1", user_id="U1", sandbox_keepalive_seconds=0.0,
                           platform=None, surface=None)
    monkeypatch.setattr(agent_mod, "_surface", lambda deps: SimpleNamespace())
    monkeypatch.setattr(importlib.import_module("agent.tools.agent_browser_stream"), "headless_note", lambda c, t: "")
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)

    result = agent_mod.ask_human_for_challenge_tool(ctx, "Solve the CAPTCHA on the sign-up page")
    assert "private link" in result and human_control.active("C1", "1.1")
    sent = client.chat_postEphemeral.call_args.kwargs
    assert sent["user"] == "U1" and sent["channel"] == "C1" and "https://desk/vnc.html" in sent["text"]
    assert not client.chat_postMessage.called  # never posted where others could see it

    assert agent_mod.ask_human_for_challenge_tool(ctx, finished=True) == "Control link turned off."
    assert calls == ["start", "stop"] and not human_control.active("C1", "1.1")


def test_control_turns_itself_off_after_the_time_limit(monkeypatch):
    calls = _fake_desktop(monkeypatch)
    monkeypatch.setattr(human_control, "CONTROL_SECONDS", 0.05)
    human_control.start("C2", "2.2")
    import time
    time.sleep(0.3)
    assert calls == ["start", "stop"] and not human_control.active("C2", "2.2")


def test_control_survives_the_turn_ending_while_coolton_waits(monkeypatch):
    """wait_tool ends the turn and resumes later: the end of that turn must not take
    the person's control away (it used to, so the link died seconds after it was sent)."""
    calls = _fake_desktop(monkeypatch)
    paused = []
    monkeypatch.setattr("agent.sandbox_helpers.pause_if_running", lambda c, t: paused.append((c, t)))
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    human_control.start("C3", "3.3")
    assert human_control.active("C3", "3.3")  # what run_agent's turn-end checks before pausing
    human_control.stop("C3", "3.3")
    assert calls == ["start", "stop"] and paused == [("C3", "3.3")]



def test_no_link_while_the_browser_is_headless_and_invisible(monkeypatch):
    agent_mod = importlib.import_module("agent.agent")
    calls = _fake_desktop(monkeypatch)
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(importlib.import_module("agent.tools.agent_browser_stream"), "headless_note", lambda c, t: "headless")
    client = Mock()
    deps = SimpleNamespace(client=client, channel_id="C4", thread_ts="4.4", user_id="U1", sandbox_keepalive_seconds=0.0)
    result = agent_mod.ask_human_for_challenge_tool(RunContext(model=None, usage=None, prompt="", deps=deps), "captcha")
    assert result.startswith("Not sent") and "agent-browser close" in result
    assert calls == [] and not client.chat_postEphemeral.called


def test_im_done_wakes_coolton_from_its_wait_and_keeps_control_on(monkeypatch, tmp_path):
    from agent import scheduler
    from listeners.actions.human_control_actions import handle_human_control_done

    _fake_desktop(monkeypatch)
    monkeypatch.setattr(scheduler, "WAIT_TASKS_FILE", str(tmp_path / "wait_tasks.json"))
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    woken = []
    monkeypatch.setattr("agent.background_jobs_poller._wake_executor.submit", lambda *a: woken.append(a))
    human_control.start("C4", "4.4")
    scheduler.create_wait("U1", "C4", "4.4", "the person to solve the CAPTCHA", 30)
    scheduler.create_wait("U1", "C5", "5.5", "something else", 30)

    respond = Mock()
    handle_human_control_done(Mock(), {"actions": [{"value": "C4|4.4"}]}, respond)

    assert len(woken) == 1 and woken[0][1:3] == ("C4", "4.4")
    assert "I'm done" in woken[0][5] and "the person to solve the CAPTCHA" in woken[0][5]
    assert [w["fired"] for w in scheduler._load_waits()["waits"]] == [True, False]
    assert human_control.active("C4", "4.4")  # coolton turns it off once it has looked
    assert "checking the page" in respond.call_args.kwargs["text"]
    human_control.stop("C4", "4.4")
