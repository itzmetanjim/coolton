from unittest.mock import Mock

from listeners.actions.policy_actions import handle_policy_opt_in, handle_policy_opt_out


def _pending_body(pending_id, user_id="U1"):
    return {
        "actions": [{"action_id": "policy_opt_in_no_join", "value": pending_id}],
        "user": {"id": user_id},
    }


def test_opt_in_reports_error_instead_of_crashing_on_failure(monkeypatch):
    """pop_pending already removed the request before anything else runs, so a
    failure downstream must not propagate unhandled — the user needs to be told
    something went wrong, not get silence with their message gone."""
    pending = {
        "user_id": "U1", "channel_id": "C1", "thread_ts": "1.1",
        "message_ts": "1.1", "text": "hi", "user_token": None, "files": None,
    }
    monkeypatch.setattr("listeners.actions.policy_actions.pop_pending", lambda pid: pending)
    monkeypatch.setattr("listeners.actions.policy_actions.record_consent", lambda *a, **k: None)
    monkeypatch.setattr("listeners.actions.policy_actions.clear_pending_for_user", lambda uid: None)
    monkeypatch.setattr(
        "agent.tools.vision.download_attached_images", lambda client, files: None
    )
    monkeypatch.setattr(
        "listeners.actions.policy_actions.run_agent_turn",
        Mock(side_effect=RuntimeError("boom")),
    )

    client = Mock()
    logger = Mock()
    ack = Mock()

    handle_policy_opt_in(ack, _pending_body("pid1"), client, logger)

    ack.assert_called_once()
    logger.exception.assert_called_once()
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert "went wrong" in kwargs["text"]


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
