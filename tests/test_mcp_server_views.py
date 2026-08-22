from unittest.mock import Mock

from listeners.views.mcp_server_views import build_add_mcp_server_modal, handle_mcp_server_add_submit


def _inputs(modal):
    return {b["block_id"]: b for b in modal["blocks"] if b["type"] == "input"}


def test_add_mcp_server_modal_structure():
    modal = build_add_mcp_server_modal()
    assert modal["type"] == "modal"
    assert modal["callback_id"] == "mcp_server_add_submit"
    assert modal["submit"]["text"] == "Add"

    inputs = _inputs(modal)
    assert set(inputs) == {"server_name", "server_url", "server_token"}
    assert inputs["server_token"]["optional"] is True


def _submit_body(name="My Server", url="https://mcp.example.com/mcp", token=""):
    return {
        "view": {
            "state": {
                "values": {
                    "server_name": {"value": {"value": name}},
                    "server_url": {"value": {"value": url}},
                    "server_token": {"value": {"value": token or None}},
                }
            }
        }
    }


def _ctx(user_id="U1"):
    context = Mock()
    context.user_id = user_id
    return context


def test_submit_saves_server_when_probe_succeeds(monkeypatch):
    monkeypatch.setattr("agent.mcp_server_store.probe_server", lambda url, token: (True, "connected, 3 tool(s) available"))
    add_mock = Mock(return_value="mcp_new")
    monkeypatch.setattr("agent.mcp_server_store.add_server", add_mock)
    client = Mock()
    ack = Mock()
    logger = Mock()

    handle_mcp_server_add_submit(ack, _submit_body(), client, _ctx(), logger)

    ack.assert_called_once()
    add_mock.assert_called_once_with("U1", "My Server", "https://mcp.example.com/mcp", "")
    client.chat_postEphemeral.assert_called_once()
    assert "Added" in client.chat_postEphemeral.call_args.kwargs["text"]


def test_submit_rejects_and_does_not_save_when_probe_fails(monkeypatch):
    monkeypatch.setattr("agent.mcp_server_store.probe_server", lambda url, token: (False, "connection refused"))
    add_mock = Mock()
    monkeypatch.setattr("agent.mcp_server_store.add_server", add_mock)
    client = Mock()
    ack = Mock()
    logger = Mock()

    handle_mcp_server_add_submit(ack, _submit_body(), client, _ctx(), logger)

    add_mock.assert_not_called()
    client.chat_postEphemeral.assert_called_once()
    text = client.chat_postEphemeral.call_args.kwargs["text"]
    assert "Couldn't connect" in text
    assert "connection refused" in text


def test_submit_handles_store_error_gracefully(monkeypatch):
    monkeypatch.setattr("agent.mcp_server_store.probe_server", lambda url, token: (True, "ok"))
    monkeypatch.setattr(
        "agent.mcp_server_store.add_server", Mock(side_effect=ValueError("You already have 5 MCP servers registered"))
    )
    client = Mock()
    ack = Mock()
    logger = Mock()

    handle_mcp_server_add_submit(ack, _submit_body(), client, _ctx(), logger)  # must not raise

    text = client.chat_postEphemeral.call_args.kwargs["text"]
    assert "already have 5" in text
