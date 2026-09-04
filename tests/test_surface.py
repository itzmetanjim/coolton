"""agent.surface.get_surface + SlackSurface — the split between "talk about the
current conversation" and "talk to Slack itself" that lets a second transport
(the web UI) reuse every tool that isn't about the current thread unchanged.
"""

from types import SimpleNamespace
from unittest.mock import Mock

from agent.surface import get_surface
from agent.surfaces.slack import SlackSurface


def test_get_surface_builds_a_slack_surface_from_a_real_agent_deps():
    from agent.deps import AgentDeps

    client = Mock()
    deps = AgentDeps(client=client, user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.2")
    surface = get_surface(deps)
    assert isinstance(surface, SlackSurface)
    assert surface.client is client
    assert surface.channel_id == "C1"
    assert surface.thread_ts == "1.1"
    assert surface.message_ts == "1.2"


def test_get_surface_caches_on_the_deps_object():
    from agent.deps import AgentDeps

    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.2")
    first = get_surface(deps)
    second = get_surface(deps)
    assert first is second


def test_get_surface_returns_an_explicitly_set_surface_unchanged():
    from agent.deps import AgentDeps

    explicit = object()
    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.2", surface=explicit)
    assert get_surface(deps) is explicit


def test_get_surface_works_against_a_minimal_simplenamespace_deps():
    """A lot of the existing test suite (and every tool that predates the Surface
    split) builds `deps` as a bare SimpleNamespace, not a real AgentDeps — this
    must never require a `.get_surface()` method on deps itself."""
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.1")
    surface = get_surface(deps)
    assert isinstance(surface, SlackSurface)
    assert surface.message_ts == ""  # missing on the namespace, defaults gracefully
    assert surface.user_token is None


def test_get_surface_is_a_free_function_not_a_deps_method_requirement():
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.1", message_ts="1.2", user_token="tok")
    surface = get_surface(deps)
    assert surface.user_token == "tok"


def test_slack_surface_post_text_sends_markdown_to_the_current_thread():
    client = Mock()
    surface = SlackSurface(client, "C1", "1.1", "1.2")
    surface.post_text("hello **world**")
    client.chat_postMessage.assert_called_once_with(
        channel="C1", thread_ts="1.1", markdown_text="hello **world**",
    )


def test_slack_surface_post_text_swallows_errors():
    client = Mock()
    client.chat_postMessage.side_effect = Exception("boom")
    surface = SlackSurface(client, "C1", "1.1", "1.2")
    surface.post_text("hi")  # must not raise


def test_slack_surface_react_delegates_to_reactions_add():
    client = Mock()
    surface = SlackSurface(client, "C1", "1.1", "1.2")
    result = surface.react("tada")
    client.reactions_add.assert_called_once_with(channel="C1", timestamp="1.2", name="tada")
    assert result == "Reacted with :tada:"


def test_slack_surface_post_file_link_includes_thread_ts_when_present():
    client = Mock()
    client.chat_postMessage.return_value = {"ok": True}
    surface = SlackSurface(client, "C1", "1.1", "1.2")
    result = surface.post_file_link("https://example.com/f", "report.csv")
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["thread_ts"] == "1.1"
    assert "report.csv" in kwargs["text"]
    assert "Uploaded report.csv" in result


def test_slack_surface_post_file_link_reports_a_failed_post():
    client = Mock()
    client.chat_postMessage.return_value = {"ok": False, "error": "channel_not_found"}
    surface = SlackSurface(client, "C1", "1.1", "1.2")
    result = surface.post_file_link("https://example.com/f", "report.csv")
    assert "posting the link failed" in result


def test_slack_surface_set_engaged_delegates_to_leave_thread_store(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.leave_thread_store.join_thread", lambda c, t: calls.append(("join", c, t)) or "joined")
    monkeypatch.setattr("agent.leave_thread_store.leave_thread", lambda c, t: calls.append(("leave", c, t)) or "left")
    surface = SlackSurface(Mock(), "C1", "1.1", "1.2")
    assert surface.set_engaged(True) == "joined"
    assert surface.set_engaged(False) == "left"
    assert calls == [("join", "C1", "1.1"), ("leave", "C1", "1.1")]


def test_slack_surface_build_hooks_is_none_without_a_plan_message():
    surface = SlackSurface(Mock(), "C1", "1.1", "1.2")
    deps = SimpleNamespace(plan_ts=None)
    assert surface.build_hooks(deps) is None


def test_slack_surface_build_hooks_returns_something_when_a_plan_message_exists():
    surface = SlackSurface(Mock(), "C1", "1.1", "1.2")
    deps = SimpleNamespace(plan_ts="100.100")
    assert surface.build_hooks(deps) is not None


def test_slack_surface_set_model_and_finish_turn_are_inert_noops():
    surface = SlackSurface(Mock(), "C1", "1.1", "1.2")
    # Slack renders these straight through agent.plan_block instead — must not
    # raise, and must not touch deps at all.
    deps = SimpleNamespace()
    surface.set_model(deps, "anthropic / claude")
    surface.finish_turn(deps)
