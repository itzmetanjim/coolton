from unittest.mock import Mock

from listeners.actions.byok_actions import handle_byok_add, handle_byok_edit


def _ctx(user_id="U1"):
    context = Mock()
    context.user_id = user_id
    return context


def test_add_endpoint_notifies_user_when_views_open_fails():
    client = Mock()
    client.views_open.side_effect = RuntimeError("trigger_id expired")
    ack = Mock()

    handle_byok_add(ack, {"trigger_id": "t1"}, client, _ctx())

    ack.assert_called_once()
    client.chat_postEphemeral.assert_called_once()
    kwargs = client.chat_postEphemeral.call_args.kwargs
    assert kwargs["channel"] == "U1"
    assert "try again" in kwargs["text"]


def test_add_endpoint_notify_failure_itself_never_raises():
    """The ephemeral fallback is itself best-effort — a second failure there
    must not propagate and crash the handler."""
    client = Mock()
    client.views_open.side_effect = RuntimeError("boom")
    client.chat_postEphemeral.side_effect = RuntimeError("also boom")
    ack = Mock()

    handle_byok_add(ack, {"trigger_id": "t1"}, client, _ctx())  # must not raise


def test_edit_endpoint_notifies_user_when_views_open_fails(monkeypatch):
    monkeypatch.setattr(
        "agent.byok_store.get_endpoint_decrypted",
        lambda user_id, ep_id: {"name": "n", "base_url": "https://x", "model": "m"},
    )
    client = Mock()
    client.views_open.side_effect = RuntimeError("trigger_id expired")
    ack = Mock()

    handle_byok_edit(ack, {"trigger_id": "t1", "actions": [{"action_id": "byok_edit_ep1"}]}, client, _ctx())

    client.chat_postEphemeral.assert_called_once()
