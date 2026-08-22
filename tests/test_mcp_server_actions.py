from unittest.mock import Mock

from listeners.actions.mcp_server_actions import handle_mcp_server_add, handle_mcp_server_delete


def _ctx(user_id="U1"):
    context = Mock()
    context.user_id = user_id
    return context


def test_add_notifies_user_when_views_open_fails():
    client = Mock()
    client.views_open.side_effect = RuntimeError("trigger_id expired")
    ack = Mock()

    handle_mcp_server_add(ack, {"trigger_id": "t1"}, client, _ctx())

    ack.assert_called_once()
    client.chat_postEphemeral.assert_called_once()
    kwargs = client.chat_postEphemeral.call_args.kwargs
    assert kwargs["channel"] == "U1"
    assert "try again" in kwargs["text"]


def test_add_open_failure_never_raises_even_if_ephemeral_also_fails():
    client = Mock()
    client.views_open.side_effect = RuntimeError("boom")
    client.chat_postEphemeral.side_effect = RuntimeError("also boom")
    ack = Mock()

    handle_mcp_server_add(ack, {"trigger_id": "t1"}, client, _ctx())  # must not raise


def test_delete_removes_server_and_confirms(monkeypatch):
    delete_mock = Mock()
    monkeypatch.setattr("agent.mcp_server_store.delete_server", delete_mock)
    client = Mock()
    ack = Mock()

    handle_mcp_server_delete(
        ack, {"actions": [{"action_id": "mcp_server_delete_mcp_abc123"}]}, client, _ctx()
    )

    ack.assert_called_once()
    delete_mock.assert_called_once_with("U1", "mcp_abc123")
    client.chat_postEphemeral.assert_called_once()


def test_delete_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(
        "agent.mcp_server_store.delete_server", Mock(side_effect=RuntimeError("boom"))
    )
    client = Mock()
    ack = Mock()

    handle_mcp_server_delete(
        ack, {"actions": [{"action_id": "mcp_server_delete_mcp_abc123"}]}, client, _ctx()
    )  # must not raise
