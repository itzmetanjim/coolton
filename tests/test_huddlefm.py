"""agent.tools.huddlefm — DJ control of a HuddleFM session via DM'd JSON
commands (https://github.com/ingoau/huddlefm/blob/main/docs/bot-api.md).
coolton is allowlisted on HuddleFM's side under its own bot identity, so
every DM here goes out on the bot client (no user token needed).
"""

import json
from unittest.mock import Mock

import pytest

from agent.tools import huddlefm


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch):
    # request_control/send_command's poll windows are real-world seconds by
    # design (host approval can take minutes) — tests must never actually
    # wait for them. _poll_reply itself is tested directly with its own tiny
    # `timeout` argument, so these constants only matter to the two
    # public-function tests that never get a reply at all.
    monkeypatch.setattr(huddlefm.time, "sleep", lambda s: None)
    monkeypatch.setattr(huddlefm, "_REQUEST_CONTROL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(huddlefm, "_COMMAND_POLL_SECONDS", 0.01)


@pytest.fixture(autouse=True)
def huddlefm_user_id(monkeypatch):
    monkeypatch.setenv("HUDDLEFM_USER_ID", "UHUDDLEFM")


def _client_with_history(*message_pages):
    """A Mock client whose conversations_replies returns each page in turn
    (the last page repeats once exhausted, for a poll that never finds a
    reply)."""
    client = Mock()
    client.conversations_open.return_value = {"ok": True, "channel": {"id": "DM1"}}
    client.chat_postMessage.return_value = {"ok": True, "ts": "100.0"}
    pages = list(message_pages)

    def _replies(**kwargs):
        page = pages.pop(0) if len(pages) > 1 else pages[0]
        return {"ok": True, "messages": page}

    client.conversations_replies.side_effect = _replies
    return client


# ---------------------------------------------------------------------------
# _dm_channel / _send
# ---------------------------------------------------------------------------


def test_dm_channel_missing_env_var(monkeypatch):
    monkeypatch.delenv("HUDDLEFM_USER_ID", raising=False)
    channel_id, error = huddlefm._dm_channel(Mock())
    assert channel_id is None
    assert "HUDDLEFM_USER_ID" in error


def test_dm_channel_opens_with_the_configured_user_id():
    client = Mock()
    client.conversations_open.return_value = {"ok": True, "channel": {"id": "DM1"}}
    channel_id, error = huddlefm._dm_channel(client)
    assert error is None
    assert channel_id == "DM1"
    client.conversations_open.assert_called_once_with(users="UHUDDLEFM")


def test_dm_channel_slack_error():
    client = Mock()
    client.conversations_open.return_value = {"ok": False, "error": "user_not_found"}
    channel_id, error = huddlefm._dm_channel(client)
    assert channel_id is None
    assert "user_not_found" in error


def test_dm_channel_exception():
    client = Mock()
    client.conversations_open.side_effect = Exception("boom")
    channel_id, error = huddlefm._dm_channel(client)
    assert channel_id is None
    assert "boom" in error


def test_send_posts_json_text_to_the_dm_channel():
    client = Mock()
    client.conversations_open.return_value = {"ok": True, "channel": {"id": "DM1"}}
    client.chat_postMessage.return_value = {"ok": True, "ts": "100.0"}
    dm_channel_id, sent_ts, error = huddlefm._send(client, {"v": 1, "type": "status"})
    assert error is None
    assert dm_channel_id == "DM1"
    assert sent_ts == "100.0"
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "DM1"
    assert json.loads(kwargs["text"]) == {"v": 1, "type": "status"}


def test_send_propagates_dm_channel_error():
    client = Mock()
    client.conversations_open.return_value = {"ok": False, "error": "user_not_found"}
    dm_channel_id, sent_ts, error = huddlefm._send(client, {"v": 1})
    assert dm_channel_id is None and sent_ts is None
    assert "user_not_found" in error


def test_send_reports_postmessage_failure():
    client = Mock()
    client.conversations_open.return_value = {"ok": True, "channel": {"id": "DM1"}}
    client.chat_postMessage.return_value = {"ok": False, "error": "channel_not_found"}
    dm_channel_id, sent_ts, error = huddlefm._send(client, {"v": 1})
    assert dm_channel_id is None and sent_ts is None
    assert "channel_not_found" in error


# ---------------------------------------------------------------------------
# _poll_reply
# ---------------------------------------------------------------------------


def test_poll_reply_finds_a_valid_json_reply_from_huddlefm():
    client = Mock()
    client.conversations_replies.return_value = {
        "ok": True,
        "messages": [
            {"user": "UBOT", "ts": "100.0", "text": '{"v":1}'},  # our own sent message
            {"user": "UHUDDLEFM", "ts": "100.1", "text": '{"ok":true,"type":"status","state":"playing"}'},
        ],
    }
    reply = huddlefm._poll_reply(client, "DM1", "100.0", timeout=5)
    assert reply == {"ok": True, "type": "status", "state": "playing"}


def test_poll_reply_ignores_non_json_and_non_huddlefm_messages():
    client = Mock()
    client.conversations_replies.return_value = {
        "ok": True,
        "messages": [
            {"user": "UBOT", "ts": "100.0", "text": '{"v":1}'},
            {"user": "USOMEONE", "ts": "100.05", "text": "not json"},
            {"user": "UHUDDLEFM", "ts": "100.1", "text": '{"ok":true}'},
        ],
    }
    reply = huddlefm._poll_reply(client, "DM1", "100.0", timeout=5)
    assert reply == {"ok": True}


def test_poll_reply_times_out_with_no_reply():
    client = Mock()
    client.conversations_replies.return_value = {
        "ok": True, "messages": [{"user": "UBOT", "ts": "100.0", "text": '{"v":1}'}],
    }
    reply = huddlefm._poll_reply(client, "DM1", "100.0", timeout=0.01)
    assert reply is None


def test_poll_reply_swallows_api_errors_and_returns_none():
    client = Mock()
    client.conversations_replies.side_effect = Exception("boom")
    reply = huddlefm._poll_reply(client, "DM1", "100.0", timeout=5)
    assert reply is None


# ---------------------------------------------------------------------------
# request_control
# ---------------------------------------------------------------------------


def test_request_control_requires_channel():
    assert "channel is required" in huddlefm.request_control(Mock(), "", "add")


def test_request_control_requires_at_least_one_permission():
    assert "permission" in huddlefm.request_control(Mock(), "C1", "")


def test_request_control_sends_the_expected_payload():
    client = _client_with_history([{"user": "UBOT", "ts": "100.0", "text": "{}"}])
    huddlefm.request_control(client, "C1", "add, skip , pause", "playback.state")
    payload = json.loads(client.chat_postMessage.call_args.kwargs["text"])
    assert payload == {
        "v": 1, "type": "request_control", "channel": "C1",
        "permissions": ["add", "skip", "pause"], "events": ["playback.state"],
    }


def test_request_control_no_immediate_reply_reports_pending():
    client = _client_with_history([{"user": "UBOT", "ts": "100.0", "text": "{}"}])
    result = huddlefm.request_control(client, "C1", "add")
    assert "waiting" in result.lower() or "approval" in result.lower()


def test_request_control_surfaces_an_immediate_error():
    client = _client_with_history([
        {"user": "UBOT", "ts": "100.0", "text": "{}"},
        {"user": "UHUDDLEFM", "ts": "100.1", "text": '{"ok":false,"error":"session_not_found"}'},
    ])
    result = huddlefm.request_control(client, "C1", "add")
    assert "session_not_found" in result


def test_request_control_propagates_dm_error():
    client = Mock()
    client.conversations_open.return_value = {"ok": False, "error": "user_not_found"}
    result = huddlefm.request_control(client, "C1", "add")
    assert "user_not_found" in result


# ---------------------------------------------------------------------------
# send_command
# ---------------------------------------------------------------------------


def test_send_command_requires_a_type():
    assert "command_type is required" in huddlefm.send_command(Mock(), "")


def test_send_command_builds_payload_with_channel_and_fields():
    client = _client_with_history([
        {"user": "UBOT", "ts": "100.0", "text": "{}"},
        {"user": "UHUDDLEFM", "ts": "100.1", "text": '{"ok":true,"volumePercent":40}'},
    ])
    result = huddlefm.send_command(client, "volume", "C1", {"percent": 40})
    payload = json.loads(client.chat_postMessage.call_args.kwargs["text"])
    assert payload == {"v": 1, "type": "volume", "channel": "C1", "percent": 40}
    assert json.loads(result) == {"ok": True, "volumePercent": 40}


def test_send_command_omits_channel_when_not_given():
    client = _client_with_history([{"user": "UBOT", "ts": "100.0", "text": "{}"}])
    huddlefm.send_command(client, "status")
    payload = json.loads(client.chat_postMessage.call_args.kwargs["text"])
    assert "channel" not in payload


def test_send_command_reports_timeout():
    client = _client_with_history([{"user": "UBOT", "ts": "100.0", "text": "{}"}])
    result = huddlefm.send_command(client, "status", fields=None)
    assert "no reply" in result.lower()


def test_send_command_propagates_dm_error():
    client = Mock()
    client.conversations_open.return_value = {"ok": False, "error": "user_not_found"}
    result = huddlefm.send_command(client, "status")
    assert "user_not_found" in result
