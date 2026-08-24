from unittest.mock import Mock

from listeners.actions.policy_actions import handle_policy_opt_in, handle_policy_opt_out


def _pending_body(pending_id, user_id="U1"):
    return {
        "actions": [{"action_id": "policy_opt_in_no_join", "value": pending_id}],
        "user": {"id": user_id},
    }


def _pending():
    return {"user_id": "U1", "channel_id": "C1", "thread_ts": "1.1", "message_ts": "1.1"}


def test_opt_in_confirms_without_replaying_the_message(monkeypatch):
    """Opting in must not re-run the message that triggered the consent
    prompt through the agent — that was a surprising side effect. Just a
    quiet confirmation the user can act on themselves."""
    monkeypatch.setattr("listeners.actions.policy_actions.pop_pending", lambda pid: _pending())
    monkeypatch.setattr("listeners.actions.policy_actions.record_consent", lambda *a, **k: None)
    monkeypatch.setattr("listeners.actions.policy_actions.clear_pending_for_user", lambda uid: None)

    client = Mock()
    logger = Mock()
    ack = Mock()

    handle_policy_opt_in(ack, _pending_body("pid1"), client, logger)

    ack.assert_called_once()
    client.chat_postMessage.assert_not_called()
    client.chat_postEphemeral.assert_called_once()
    kwargs = client.chat_postEphemeral.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["user"] == "U1"
    assert kwargs["thread_ts"] == "1.1"
    assert kwargs["text"] == "Done! Ask anything."


def test_opt_in_reports_error_instead_of_crashing_on_failure(monkeypatch):
    """pop_pending already removed the request before anything else runs, so a
    failure downstream must not propagate unhandled — the user needs to be told
    something went wrong, not get silence with their opt-in in an unknown state."""
    monkeypatch.setattr("listeners.actions.policy_actions.pop_pending", lambda pid: _pending())
    monkeypatch.setattr("listeners.actions.policy_actions.record_consent", lambda *a, **k: None)
    monkeypatch.setattr("listeners.actions.policy_actions.clear_pending_for_user", lambda uid: None)

    client = Mock()
    client.chat_postEphemeral.side_effect = RuntimeError("boom")
    logger = Mock()
    ack = Mock()

    handle_policy_opt_in(ack, _pending_body("pid1"), client, logger)

    ack.assert_called_once()
    logger.exception.assert_called_once()
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert "went wrong" in kwargs["text"]


def test_opt_in_join_invites_to_policy_channel(monkeypatch):
    monkeypatch.setattr("listeners.actions.policy_actions.pop_pending", lambda pid: _pending())
    record_consent = Mock()
    monkeypatch.setattr("listeners.actions.policy_actions.record_consent", record_consent)
    monkeypatch.setattr("listeners.actions.policy_actions.clear_pending_for_user", lambda uid: None)

    client = Mock()
    logger = Mock()
    ack = Mock()
    body = {
        "actions": [{"action_id": "policy_opt_in_join", "value": "pid1"}],
        "user": {"id": "U1"},
    }

    handle_policy_opt_in(ack, body, client, logger)

    client.conversations_invite.assert_called_once()
    record_consent.assert_called_once_with("U1", joined_policy_channel=True)
    client.chat_postEphemeral.assert_called_once()


def test_opt_in_ignores_mismatched_user(monkeypatch):
    monkeypatch.setattr("listeners.actions.policy_actions.pop_pending", lambda pid: _pending())
    client = Mock()
    logger = Mock()
    ack = Mock()

    handle_policy_opt_in(ack, _pending_body("pid1", user_id="U_OTHER"), client, logger)

    ack.assert_called_once()
    client.chat_postEphemeral.assert_not_called()
    client.chat_postMessage.assert_not_called()


def test_opt_out_failure_is_caught_and_logged(monkeypatch):
    monkeypatch.setattr(
        "listeners.actions.policy_actions.revoke_consent",
        Mock(side_effect=RuntimeError("boom")),
    )
    client = Mock()
    logger = Mock()
    ack = Mock()

    # Must not raise.
    handle_policy_opt_out(ack, {"user": {"id": "U1"}}, client, logger)

    ack.assert_called_once()
    logger.exception.assert_called_once()
