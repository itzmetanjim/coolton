"""agent.tools.code_channel — creates a Slack "code channel" via
./codechannel.sh (never reimplemented in Python — see that module's own
docstring for why codechannelinternal.sh must never be read) and, on
success, schedules coolton joining it and picking up work there as its own
single conversation (thread_ts="" — agent.code_channel_store).
"""

import subprocess
from unittest.mock import Mock

import pytest

from agent.tools import code_channel


def _fake_run(stdout="", stderr="", returncode=0, raise_exc=None):
    def _run(*a, **k):
        if raise_exc:
            raise raise_exc
        return subprocess.CompletedProcess(args=a, returncode=returncode, stdout=stdout, stderr=stderr)
    return _run


@pytest.fixture(autouse=True)
def not_banned(monkeypatch):
    monkeypatch.setattr("agent.ban_store.is_banned", lambda uid: False)


@pytest.fixture(autouse=True)
def no_background_activation(monkeypatch):
    """By default don't actually spin up the activation thread — most tests
    only care about create_code_channel's own return value / registration.
    Tests that care about activation replace this with a synchronous stand-in.
    """
    monkeypatch.setattr(code_channel.threading, "Thread", lambda target, args, daemon: Mock(start=lambda: None))


# ---------------------------------------------------------------------------
# create_code_channel — talking to the script
# ---------------------------------------------------------------------------


def test_ok_response_returns_channel_id_and_registers(monkeypatch, tmp_path):
    monkeypatch.setattr(code_channel.subprocess, "run", _fake_run(stdout="ok\nC0C12EVC656\n"))
    registered = []
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: registered.append(a))

    result = code_channel.create_code_channel(
        client=Mock(), name="Code audit and bug detection in Coolton", task="fix bug X",
        owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )

    assert "C0C12EVC656" in result
    assert registered == [("C0C12EVC656", "Code audit and bug detection in Coolton", "U1", "C0", "1.1")]


def test_error_response_forwarded_verbatim_and_not_registered(monkeypatch):
    monkeypatch.setattr(
        code_channel.subprocess, "run",
        _fake_run(stdout='error\n{"ok":false,"error":"invalid_name"}\n'),
    )
    registered = []
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: registered.append(a))

    result = code_channel.create_code_channel(
        client=Mock(), name="", task="", owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )

    assert '{"ok":false,"error":"invalid_name"}' in result
    assert registered == []


def test_error_response_forwards_a_different_error_body_verbatim(monkeypatch):
    # The exact channel id / error body vary run to run — nothing here should
    # be hardcoded to one specific example.
    monkeypatch.setattr(
        code_channel.subprocess, "run",
        _fake_run(stdout='error\n{"ok":false,"error":"channel_creation_failed"}\n'),
    )
    result = code_channel.create_code_channel(
        client=Mock(), name="x", task="", owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )
    assert '{"ok":false,"error":"channel_creation_failed"}' in result


def test_ok_response_with_a_different_channel_id(monkeypatch):
    monkeypatch.setattr(code_channel.subprocess, "run", _fake_run(stdout="ok\nC09ZZZQQQ1\n"))
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: None)
    result = code_channel.create_code_channel(
        client=Mock(), name="x", task="", owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )
    assert "C09ZZZQQQ1" in result


def test_display_name_reaches_the_script_as_a_single_unmodified_argv_entry(monkeypatch):
    """The whole point of not validating the name ourselves: spaces, unicode,
    uppercase, and quote characters must all survive as ONE argv element,
    which only holds if this never goes through a shell string."""
    captured = {}

    def _run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\nC1\n", stderr="")

    monkeypatch.setattr(code_channel.subprocess, "run", _run)
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: None)

    tricky_name = 'Code audit — "bug" detection 🐛 in Coolton'
    code_channel.create_code_channel(
        client=Mock(), name=tricky_name, task="", owner_id="U1",
        source_channel_id="C0", source_thread_ts="1.1",
    )

    args = captured["args"]
    assert args[-1] == tricky_name
    assert args[-2] == "a"


def test_missing_script_is_surfaced_not_swallowed(monkeypatch):
    monkeypatch.setattr(code_channel.subprocess, "run", _fake_run(raise_exc=FileNotFoundError()))
    result = code_channel.create_code_channel(
        client=Mock(), name="x", task="", owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )
    assert "Error" in result


def test_nonzero_exit_unexpected_output_is_surfaced(monkeypatch):
    monkeypatch.setattr(code_channel.subprocess, "run", _fake_run(stdout="", stderr="boom", returncode=1))
    result = code_channel.create_code_channel(
        client=Mock(), name="x", task="", owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )
    assert "Error" in result
    assert "boom" in result


def test_timeout_is_surfaced(monkeypatch):
    monkeypatch.setattr(
        code_channel.subprocess, "run",
        _fake_run(raise_exc=subprocess.TimeoutExpired(cmd="codechannel.sh", timeout=60)),
    )
    result = code_channel.create_code_channel(
        client=Mock(), name="x", task="", owner_id="U1", source_channel_id="C0", source_thread_ts="1.1",
    )
    assert "Error" in result
    assert "timed out" in result.lower()


def test_ok_response_schedules_activation_thread(monkeypatch):
    monkeypatch.setattr(code_channel.subprocess, "run", _fake_run(stdout="ok\nC1\n"))
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: None)
    started = []
    monkeypatch.setattr(
        code_channel.threading, "Thread",
        lambda target, args, daemon: Mock(start=lambda: started.append((target, args, daemon))),
    )
    code_channel.create_code_channel(
        client=Mock(), name="x", task="do the thing", owner_id="U1",
        source_channel_id="C0", source_thread_ts="1.1",
    )
    assert len(started) == 1
    target, args, daemon = started[0]
    assert target is code_channel._activate_code_channel
    assert daemon is True
    assert args == (Mock, "C1", "x", "do the thing", "U1", "C0", "1.1") or (
        args[1:] == ("C1", "x", "do the thing", "U1", "C0", "1.1")
    )


# ---------------------------------------------------------------------------
# _delete_cooltonuser_auto_message
# ---------------------------------------------------------------------------


def test_delete_auto_message_finds_and_deletes_the_oldest_cooltonuser_message(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-cooltonuser")
    monkeypatch.setenv("COOLTON_USER_ID", "UCOOLTON")

    user_client = Mock()
    user_client.conversations_history.return_value = {
        "ok": True,
        "messages": [
            # Slack returns newest-first — deliberately out of ts order here.
            {"user": "UCOOLTON", "ts": "300.0", "text": "later"},
            {"user": "UCOOLTON", "ts": "100.0", "text": "the auto message"},
            {"user": "U_SOMEONE_ELSE", "ts": "50.0", "text": "not cooltonUser"},
        ],
    }
    monkeypatch.setattr(code_channel, "WebClient", lambda token: user_client)

    code_channel._delete_cooltonuser_auto_message("C1")

    user_client.conversations_history.assert_called_once_with(channel="C1", limit=200)
    user_client.chat_delete.assert_called_once_with(channel="C1", ts="100.0")


def test_delete_auto_message_noop_when_no_cooltonuser_message(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-cooltonuser")
    monkeypatch.setenv("COOLTON_USER_ID", "UCOOLTON")

    user_client = Mock()
    user_client.conversations_history.return_value = {
        "ok": True, "messages": [{"user": "U_SOMEONE_ELSE", "ts": "50.0"}],
    }
    monkeypatch.setattr(code_channel, "WebClient", lambda token: user_client)

    code_channel._delete_cooltonuser_auto_message("C1")

    user_client.chat_delete.assert_not_called()


def test_delete_auto_message_noop_without_user_token(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    monkeypatch.setenv("COOLTON_USER_ID", "UCOOLTON")
    called = []
    monkeypatch.setattr(code_channel, "WebClient", lambda token: called.append(token))

    code_channel._delete_cooltonuser_auto_message("C1")

    assert called == []


def test_delete_auto_message_noop_without_coolton_user_id(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-cooltonuser")
    monkeypatch.delenv("COOLTON_USER_ID", raising=False)
    called = []
    monkeypatch.setattr(code_channel, "WebClient", lambda token: called.append(token))

    code_channel._delete_cooltonuser_auto_message("C1")

    assert called == []


def test_delete_auto_message_history_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-cooltonuser")
    monkeypatch.setenv("COOLTON_USER_ID", "UCOOLTON")

    user_client = Mock()
    user_client.conversations_history.side_effect = Exception("boom")
    monkeypatch.setattr(code_channel, "WebClient", lambda token: user_client)

    code_channel._delete_cooltonuser_auto_message("C1")  # must not raise
    user_client.chat_delete.assert_not_called()


def test_delete_auto_message_delete_failure_does_not_raise(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-cooltonuser")
    monkeypatch.setenv("COOLTON_USER_ID", "UCOOLTON")

    user_client = Mock()
    user_client.conversations_history.return_value = {
        "ok": True, "messages": [{"user": "UCOOLTON", "ts": "100.0"}],
    }
    user_client.chat_delete.side_effect = Exception("cant_delete_message")
    monkeypatch.setattr(code_channel, "WebClient", lambda token: user_client)

    code_channel._delete_cooltonuser_auto_message("C1")  # must not raise


def test_delete_auto_message_not_ok_response_is_treated_as_no_messages(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-cooltonuser")
    monkeypatch.setenv("COOLTON_USER_ID", "UCOOLTON")

    user_client = Mock()
    user_client.conversations_history.return_value = {"ok": False, "error": "not_in_channel"}
    monkeypatch.setattr(code_channel, "WebClient", lambda token: user_client)

    code_channel._delete_cooltonuser_auto_message("C1")

    user_client.chat_delete.assert_not_called()


# ---------------------------------------------------------------------------
# _activate_code_channel
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(code_channel.time, "sleep", lambda s: None)


def test_activation_joins_invites_posts_banner_and_runs_a_turn(monkeypatch):
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

    client.conversations_join.assert_called_once_with(channel="C1")
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


def test_activation_conversations_join_failure_does_not_abort(monkeypatch):
    client = Mock()
    client.conversations_join.side_effect = Exception("already_in_channel")
    client.chat_postMessage.return_value = {"ts": "999.1"}
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    monkeypatch.setattr("thread_context.conversation_store.get_history", lambda c, t: None)
    monkeypatch.setattr("agent.ensure_coolton_user.ensure_coolton_user_in_channel", lambda *a: None)
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    code_channel._activate_code_channel(client, "C1", "name", "task", "U1", "C0", "1.1")

    assert len(turn_calls) == 1


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
