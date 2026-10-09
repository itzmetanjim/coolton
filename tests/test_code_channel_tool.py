"""agent.tools.code_channel: creates a Slack code channel with
agents.conversations.create (as the separate code channel app), adds coolton to it,
and schedules coolton picking up work there as its own single conversation
(thread_ts="", agent.code_channel_store)."""

from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackApiError

from agent.tools import code_channel


@pytest.fixture(autouse=True)
def not_banned(monkeypatch):
    monkeypatch.setattr("agent.ban_store.is_banned", lambda uid: False)


@pytest.fixture(autouse=True)
def no_background_activation(monkeypatch):
    """Don't actually spin up the activation thread: these tests only care about
    create_code_channel's own result and what it asked Slack for."""
    monkeypatch.setattr(code_channel.threading, "Thread", lambda target, args, daemon: Mock(start=lambda: None))


@pytest.fixture(autouse=True)
def ids(monkeypatch):
    monkeypatch.setenv("COOLTON_BOT_ID", "UBOT")
    monkeypatch.setenv("COOLTON_USER_ID", "UHELPER")


def _slack(*responses, monkeypatch=None):
    """(coolton's client, the code channel app's client, its create calls). The app's
    agents.conversations.create calls return `responses` in turn (an {"ok": False}
    one raised as Slack's SDK does)."""
    client = Mock()
    client.conversations_info.return_value = {"channel": {"context_team_id": "T0266FRGM"}}
    app = Mock()
    calls = []

    def api_call(method, json):
        calls.append((method, json))
        data = responses[len(calls) - 1]
        if not data.get("ok"):
            raise SlackApiError("refused", Mock(data=data))
        return Mock(data=data)

    app.api_call.side_effect = api_call
    monkeypatch.setattr(code_channel, "_code_channel_app", lambda: app)
    return client, app, calls


def _create(client, **kw):
    args = dict(name="Code audit and bug detection in Coolton", task="fix bug X", owner_id="U1",
                source_channel_id="C0", source_thread_ts="1.1", source_message_ts="1.2")
    return code_channel.create_code_channel(client, **{**args, **kw})


def test_it_creates_the_channel_linked_to_the_request_and_registers_it(monkeypatch):
    registered = []
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: registered.append(a))
    client, app, calls = _slack({"ok": True, "channel_id": "C0C12EVC656"}, monkeypatch=monkeypatch)

    result = _create(client)

    assert "<#C0C12EVC656>" in result
    [(method, params)] = calls
    assert method == "agents.conversations.create"
    assert params == {"name": "Code audit and bug detection in Coolton", "session_id": "coolton:C0:1.2",
                      "origin_channel_id": "C0", "origin_message_ts": "1.2"}
    # coolton's bot and cooltonUser are added; Slack adds the origin's author itself.
    app.conversations_invite.assert_called_once_with(channel="C0C12EVC656", users="UBOT,UHELPER", force=True)
    assert registered == [("C0C12EVC656", "Code audit and bug detection in Coolton", "U1", "C0", "1.1")]


def test_from_a_dm_it_creates_without_an_origin_and_invites_the_requester(monkeypatch):
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: None)
    client, app, calls = _slack({"ok": False, "error": "origin_channel_externally_shared"},
                                {"ok": True, "channel_id": "C9"}, monkeypatch=monkeypatch)

    assert "<#C9>" in _create(client, source_channel_id="D0")
    assert "origin_channel_id" not in calls[1][1] and calls[1][1]["team_id"] == "T0266FRGM"
    app.conversations_invite.assert_called_once_with(channel="C9", users="UBOT,UHELPER,U1", force=True)


def test_a_refusal_is_explained_and_nothing_is_registered(monkeypatch):
    registered = []
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: registered.append(a))
    client, _, _ = _slack({"ok": False, "error": "missing_scope"}, monkeypatch=monkeypatch)

    result = _create(client)

    assert "missing_scope" in result
    assert registered == []


def test_without_the_code_channel_app_nothing_is_attempted(monkeypatch):
    monkeypatch.setattr(code_channel, "_code_channel_app", lambda: None)
    assert "SLACK_CODE_CHANNEL_BOT_TOKEN" in _create(Mock())


def test_success_schedules_the_handoff(monkeypatch):
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: None)
    started = []
    monkeypatch.setattr(code_channel.threading, "Thread",
                        lambda target, args, daemon: Mock(start=lambda: started.append((target, args))))
    client, _, _ = _slack({"ok": True, "channel_id": "C1"}, monkeypatch=monkeypatch)

    _create(client, task="do the thing")

    [(target, args)] = started
    assert target is code_channel._activate_code_channel
    assert args[1:] == ("C1", "Code audit and bug detection in Coolton", "do the thing", "U1", "C0", "1.1")


# ---------------------------------------------------------------------------
# _activate_code_channel
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(code_channel.time, "sleep", lambda s: None)


def test_activation_invites_posts_banner_and_runs_a_turn(monkeypatch):
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "999.1"}
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    monkeypatch.setattr("thread_context.conversation_store.get_history", lambda c, t: ["seeded-history"])
    ensure_calls = []
    monkeypatch.setattr(
        "agent.ensure_coolton_user.ensure_coolton_user_in_channel",
        lambda client, channel_id: ensure_calls.append(channel_id),
    )
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    code_channel._activate_code_channel(
        client, "C1", "My Channel", "fix the bug", "U1", "C0", "1.1",
    )

    assert ensure_calls == ["C1"]
    assert client.chat_postMessage.call_count == 1
    assert client.chat_postMessage.call_args.kwargs["channel"] == "C1"

    assert len(turn_calls) == 1
    kwargs = turn_calls[0]
    assert kwargs["channel_id"] == "C1"
    assert kwargs["thread_ts"] == ""
    assert kwargs["message_ts"] == "999.1"
    assert kwargs["user_id"] == "U1"
    assert kwargs["history"] == ["seeded-history"]
    assert "fix the bug" in kwargs["text"]
    assert "SYSTEM" in kwargs["text"]


def test_activation_waits_for_the_origin_turn_to_finish_before_reading_history(monkeypatch):
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "999.1"}
    active_states = [True, True, False]
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: active_states.pop(0) if active_states else False)
    history_reads = []
    monkeypatch.setattr(
        "thread_context.conversation_store.get_history",
        lambda c, t: history_reads.append((c, t)) or [],
    )
    monkeypatch.setattr("agent.ensure_coolton_user.ensure_coolton_user_in_channel", lambda *a: None)
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: None)

    code_channel._activate_code_channel(client, "C1", "name", "task", "U1", "C0", "1.1")

    assert history_reads == [("C0", "1.1")]


def test_activation_skips_banner_and_turn_for_a_banned_owner(monkeypatch):
    monkeypatch.setattr("agent.ban_store.is_banned", lambda uid: True)
    client = Mock()
    monkeypatch.setattr("agent.ensure_coolton_user.ensure_coolton_user_in_channel", lambda *a: None)
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    code_channel._activate_code_channel(client, "C1", "name", "task", "U1", "C0", "1.1")

    client.chat_postMessage.assert_not_called()
    assert turn_calls == []


def test_activation_banner_post_failure_does_not_start_a_turn(monkeypatch):
    client = Mock()
    client.chat_postMessage.side_effect = Exception("channel_not_found")
    monkeypatch.setattr("agent.ensure_coolton_user.ensure_coolton_user_in_channel", lambda *a: None)
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    code_channel._activate_code_channel(client, "C1", "name", "task", "U1", "C0", "1.1")

    assert turn_calls == []


def test_activation_without_a_task_still_hands_off(monkeypatch):
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "1.0"}
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    monkeypatch.setattr("thread_context.conversation_store.get_history", lambda c, t: None)
    monkeypatch.setattr("agent.ensure_coolton_user.ensure_coolton_user_in_channel", lambda *a: None)
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    code_channel._activate_code_channel(client, "C1", "name", "", "U1", "C0", "1.1")

    assert len(turn_calls) == 1
    assert "SYSTEM" in turn_calls[0]["text"]
