import json
import time
from unittest.mock import Mock

from agent import admin_alerts
from listeners.actions.feedback_buttons import handle_feedback_button
from listeners.views.feedback_views import build_feedback_modal, handle_feedback_submit


def _wait_for(mock, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock.called:
            return
        time.sleep(0.02)


def test_feedback_button_opens_modal_not_ephemeral():
    ack = Mock()
    client = Mock()
    context = Mock(channel_id="C1", user_id="U1")
    body = {
        "message": {"ts": "111.222"},
        "actions": [{"value": "good-feedback"}],
        "trigger_id": "trigger-abc",
    }
    logger = Mock()

    handle_feedback_button(ack, body, client, context, logger)

    ack.assert_called_once()
    client.views_open.assert_called_once()
    client.chat_postEphemeral.assert_not_called()
    kwargs = client.views_open.call_args.kwargs
    assert kwargs["trigger_id"] == "trigger-abc"
    metadata = json.loads(kwargs["view"]["private_metadata"])
    assert metadata == {"channel_id": "C1", "message_ts": "111.222", "feedback_value": "good-feedback"}


def test_feedback_button_falls_back_to_ephemeral_on_error():
    ack = Mock()
    client = Mock()
    client.views_open.side_effect = RuntimeError("expired trigger")
    context = Mock(channel_id="C1", user_id="U1")
    body = {"message": {"ts": "111.222"}, "actions": [{"value": "good-feedback"}], "trigger_id": "t"}
    logger = Mock()

    handle_feedback_button(ack, body, client, context, logger)

    client.chat_postEphemeral.assert_called_once()


def test_build_feedback_modal_positive_vs_negative_copy():
    good = build_feedback_modal("good-feedback", "C1", "1.1")
    bad = build_feedback_modal("bad-feedback", "C1", "1.1")
    assert good["title"]["text"] != bad["title"]["text"]


def test_feedback_submit_dms_admin_with_link_and_comment(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    post_mock = Mock(return_value=Mock(json=lambda: {"ok": True}))
    monkeypatch.setattr(admin_alerts.requests, "post", post_mock)

    ack = Mock()
    client = Mock()
    client.chat_getPermalink.return_value = {"ok": True, "permalink": "https://slack.com/archives/C1/p1112220000"}
    context = Mock(user_id="U1")
    body = {
        "view": {
            "private_metadata": json.dumps({"channel_id": "C1", "message_ts": "111.222", "feedback_value": "bad-feedback"}),
            "state": {"values": {"comment": {"value": {"value": "it hallucinated"}}}},
        }
    }
    logger = Mock()

    handle_feedback_submit(ack, body, client, context, logger)

    ack.assert_called_once()
    _wait_for(post_mock)
    post_mock.assert_called_once()
    sent_text = post_mock.call_args.kwargs["json"]["text"]
    assert "👎" in sent_text
    assert "U1" in sent_text
    assert "https://slack.com/archives/C1/p1112220000" in sent_text
    assert "it hallucinated" in sent_text

    client.chat_postEphemeral.assert_called_once()


def test_feedback_submit_handles_missing_permalink_and_empty_comment(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    post_mock = Mock(return_value=Mock(json=lambda: {"ok": True}))
    monkeypatch.setattr(admin_alerts.requests, "post", post_mock)

    ack = Mock()
    client = Mock()
    client.chat_getPermalink.side_effect = RuntimeError("no scope")
    context = Mock(user_id="U2")
    body = {
        "view": {
            "private_metadata": json.dumps({"channel_id": "C2", "message_ts": "2.2", "feedback_value": "good-feedback"}),
            "state": {"values": {"comment": {"value": {"value": None}}}},
        }
    }
    logger = Mock()

    handle_feedback_submit(ack, body, client, context, logger)  # must not raise

    _wait_for(post_mock)
    sent_text = post_mock.call_args.kwargs["json"]["text"]
    assert "(no comment)" in sent_text
    assert "👍" in sent_text
